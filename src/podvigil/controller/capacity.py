"""Opt-in capacity rounds: proven absence before another attempt; catalog reads between attempts."""

from contextlib import nullcontext
import time

from ..credentials import load_key
from ..outcomes import of_failure, of_result, remains
from ..provider.catalog import NoCapacity, check_catalog
from ..provider.client import configured_api
from .launch import attempt_left_nothing, definite_no_capacity, launch_candidates, runtime_limit
from .ledger import epoch_utc, status_read

CAPACITY_POLL_SECONDS = 75


class CapacityWaitInterrupted(KeyboardInterrupt):
    """Interrupted between attempts, after inspecting the last attempt's ledger."""


def verified_empty(folder, wait_seconds):
    """A missing or malformed attempt ledger is not evidence of an empty attempt."""
    job = status_read(folder / "job.json")
    if not isinstance(job, dict) or not isinstance(job.get("job_id"), str) or not job["job_id"]:
        return False
    return attempt_left_nothing(folder, wait_seconds=wait_seconds)


def interrupted(ledgers):
    if not ledgers:
        message = "Capacity wait interrupted; no resource attempt created a ledger or resources."
    else:
        try:
            empty = verified_empty(ledgers[-1], wait_seconds=0)
        except Exception:
            empty = False
        message = (
            "Capacity wait interrupted; the last attempt's ledger verifies that nothing remains."
            if empty
            else "Capacity wait interrupted; the last attempt's ledger does not verify absence. "
            "Check the closing resource lines before assuming billing has ended."
        )
    raise CapacityWaitInterrupted(message) from None


def available_candidates(api, configs, end):
    """The same Secure Cloud catalog criteria as survey, narrowed to the requested candidates.

    Keep the configured client's account-wide pacing and read retry budget. One deadline covers
    the catalog round; neither a rate-limit refusal nor a pricing error means no capacity.
    """
    available = []
    context = api.pacing(deadline=end) if hasattr(api, "pacing") else nullcontext()
    with context:
        for cfg in configs:
            if time.monotonic() >= end:
                raise NoCapacity("Capacity waiting allowance expired.")
            runtime_limit(cfg)
            try:
                check_catalog(api, cfg)
            except NoCapacity:
                continue
            available.append(cfg)
    return available


def launch_with_capacity_wait(configs, ledgers, minutes, *, detach=False, details, progress):
    """Run the initial candidate round normally, then only attempt catalog-positive candidates.

    `details` is also retained by the CLI on failures and interrupts. Catalog-only rounds create
    no ledger; every actual attempt keeps its own ordinary ledger. The allowance gates new
    rounds, not an already-started job's supervision or cleanup.
    """
    end = time.monotonic() + minutes * 60
    end_utc = epoch_utc(time.time() + minutes * 60)
    api = None
    details["capacity_rounds"] = 0
    while True:
        details["capacity_rounds"] += 1
        number = details["capacity_rounds"]
        before = len(ledgers)
        answer, next_at = "error", None
        try:
            candidates = configs
            if number > 1:
                try:
                    api = api or configured_api(load_key())
                    candidates = available_candidates(api, configs, end)
                except KeyboardInterrupt:
                    interrupted(ledgers)
                if not candidates:
                    raise NoCapacity("No candidate reports Secure Cloud capacity in this round.")
            result = launch_candidates(
                candidates,
                ledgers,
                detach=detach,
                capacity_deadline=end if number > 1 else None,
            )
            answer = of_result("launch", result, configs)
            return result
        except Exception as exc:
            answer = of_failure(
                "launch",
                exc,
                ledgers[-1] if len(ledgers) > before else None,
                definite_no_capacity(exc),
            )
            if not definite_no_capacity(exc):
                raise
            # The original candidate loop only needs to verify cleanup before its next
            # candidate. A new round must also verify the final candidate, and any others.
            for folder in ledgers[before:]:
                # An unreadable ledger is a verification failure, never a no-capacity exit.
                answer = "error"
                empty = verified_empty(folder, min(60, max(0, end - time.monotonic())))
                if not empty:
                    answer = remains(folder) or "error"
                    raise RuntimeError(
                        "Capacity retry refused: the attempt's ledger does not prove that no Pod "
                        "was created and no volume remains. Ledger: " + str(folder)
                    ) from exc
            answer = "no_capacity"
            if time.monotonic() >= end:
                raise
            next_at = min(end, time.monotonic() + CAPACITY_POLL_SECONDS)
        except KeyboardInterrupt:
            answer = "interrupted"
            raise
        finally:
            details["outcome"] = answer
            details["attempt_ledgers"] = [str(folder) for folder in ledgers]
            next_utc = (
                epoch_utc(time.time() + max(0, next_at - time.monotonic()))
                if next_at is not None and next_at < end
                else "none"
            )
            progress(
                f"Capacity round {number}: {answer}; next round: {next_utc}; "
                f"allowance ends: {end_utc}."
            )
        try:
            while time.monotonic() < next_at:
                time.sleep(max(0, min(60, next_at - time.monotonic())))
        except KeyboardInterrupt:
            interrupted(ledgers)
        if time.monotonic() >= end:
            raise NoCapacity("Capacity waiting allowance expired.")
