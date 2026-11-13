"""Verified deletion: DELETE only an owned resource, then prove its absence with separated reads."""

import time
import urllib.parse

from .api import ApiError


def delete_verified(
    api, path, matches, attempts=8, sleep=time.sleep, confirm_absent=None, *, actor=None
):
    """Return observations, not attribution of which guard caused deletion.

    confirm_absent, if provided, must return a bool after a complete inventory walk.
    A Pod tombstone always requires that proof (using api.listing by default).
    Ordinary absence keeps the existing optional inventory-callback contract.
    """
    evidence = {
        "verified": False,
        "path": path.split("?", 1)[0],
        "actor": actor,
        "delete_attempts": [],
        "observations": [],
        "proof": None,
    }
    started = time.monotonic()
    resource_id = urllib.parse.unquote(evidence["path"].rsplit("/", 1)[-1])
    is_pod = evidence["path"].startswith("/pods/")
    last = "resource is still present"

    def metadata(method):
        value = getattr(api, "last_response", None)
        return (
            value
            if (
                isinstance(value, dict)
                and value.get("method") == method
                and value.get("path") == evidence["path"]
            )
            else {}
        )

    def observe():
        current = api.get_or_none(path)
        info = metadata("GET")
        observation = {
            "type": "point_read",
            "status": info.get("status"),
            "response_date": info.get("response_date"),
            "elapsed_seconds": time.monotonic() - started,
        }
        if current is None:
            observation["state"] = "absent"
        else:
            if not matches(current):
                raise PermissionError("Ownership mismatch; refusing to delete this resource.")
            observation.update(
                state="terminated"
                if is_pod and current.get("status") == "TERMINATED"
                else "present",
                ownership_checked=True,
            )
        evidence["observations"].append(observation)
        return observation["state"]

    def confirm(first):
        first_seen = evidence["observations"][-1]["elapsed_seconds"]
        sleep(1)
        second = observe()
        separation = evidence["observations"][-1]["elapsed_seconds"] - first_seen
        if second == "present":
            return False
        tombstone = "terminated" in (first, second)
        inventory = None
        if confirm_absent is not None:
            absent = confirm_absent()
            if type(absent) is not bool:
                raise RuntimeError("Inventory confirmation must return a boolean.")
            inventory = {
                "type": "inventory",
                "source": "confirm_absent",
                "absent": absent,
                "complete": True,
            }
        elif tombstone:
            values = api.listing("/pods", "pods")
            if not isinstance(values, list) or any(
                not isinstance(value, dict)
                or not isinstance(value.get("id"), str)
                or not value["id"]
                for value in values
            ):
                raise RuntimeError("Malformed inventory; tombstone is not verified.")
            inventory = {
                "type": "inventory",
                "source": "api.listing",
                "path": "/pods",
                "absent": all(value["id"] != resource_id for value in values),
                "complete": True,
                "resource_count": len(values),
            }
        if inventory is not None:
            evidence["observations"].append(inventory)
            if not inventory["absent"]:
                return False
        evidence["verified"] = True
        evidence["proof"] = {
            "point_states": [first, second],
            "separation_seconds": separation,
            "requested_separation_seconds": 1,
            "observed_absence": "absent" in (first, second),
            "observed_tombstone": tombstone,
            "inventory": inventory,
        }
        return True

    for attempt in range(attempts):
        delay = min(2**attempt, 30)
        try:
            state = observe()
            if state == "present":
                record = {
                    "attempt": len(evidence["delete_attempts"]) + 1,
                    "status": None,
                    "outcome": "uncertain",
                    "actor": actor,
                }
                evidence["delete_attempts"].append(record)
                try:
                    api.request("DELETE", path)
                    info = metadata("DELETE")
                    record.update(
                        status=info.get("status"),
                        outcome="accepted",
                        response_date=info.get("response_date"),
                        retry_after=info.get("retry_after"),
                    )
                    if info.get("retry_after") is not None:
                        sleep(info["retry_after"])
                except ApiError as exc:
                    record.update(
                        status=exc.status,
                        outcome="not_found" if exc.status == 404 else "error",
                        not_sent=getattr(exc, "not_sent", False),
                        retry_after=getattr(exc, "retry_after", None),
                        title=getattr(exc, "title", None),
                        detail=getattr(exc, "detail", None),
                        response_date=getattr(exc, "response_date", None),
                    )
                    if exc.status != 404:
                        raise
                # An accepted DELETE still requires proof of absence.
                state = observe()
            if state != "present" and confirm(state):
                return evidence
            last = "resource presence or inventory still prevents confirmation"
        except ApiError as exc:
            last = str(exc)
            evidence["observations"].append(
                {
                    "type": "api_error",
                    "status": exc.status,
                    "method": getattr(exc, "method", None),
                    "retry_after": getattr(exc, "retry_after", None),
                }
            )
            if exc.status in (400, 401, 403, 422):
                break
            delay = max(delay, getattr(exc, "retry_after", None) or 0)
        if attempt + 1 < attempts:
            sleep(delay)
    error = RuntimeError("Cleanup NOT verified: " + last)
    error.evidence = evidence
    raise error
