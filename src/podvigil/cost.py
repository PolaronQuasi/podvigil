"""What a job's compute is estimated to cost, from the price quote its ledger keeps. Reads only.

The estimate is the quoted Secure Cloud rate times the time from the Pod's creation receipt to its verified
deletion, or until now while the Pod exists. It is not a bill: Runpod bills by its own clock and rates, and
the network volume's storage and taxes are not in it.
"""

import math
import time

from .controller.ledger import epoch_utc, ledger_folder, status_read, utc_epoch

NOTE = (
    "An estimate, not a bill: the quoted rate times the time from the Pod's creation "
    "receipt to its verified deletion, or until now while it exists. Runpod bills by "
    "its own clock and rates; the network volume's storage and taxes are not included."
)


def _field(folder, name, key):
    record = status_read(folder / name, {})
    return record.get(key) if isinstance(record, dict) else None


def compute_estimate(folder, job, pod, now=None):
    """The estimated compute cost so far, or None when the ledger cannot support one.

    `pod` is what status.resource_state says about the job's Pod.
    """
    now = time.time() if now is None else now
    quote = job.get("quote") if isinstance(job.get("quote"), dict) else {}
    rate = quote.get("compute_usd_per_hour")
    started = utc_epoch(_field(folder, "pod-receipt.json", "recorded_utc"))
    if type(rate) not in (int, float) or not math.isfinite(rate) or started is None:
        return None
    deleted = pod.get("state") == "deleted"
    ended = utc_epoch(_field(folder, "pod-deleted.json", "verified_utc")) if deleted else now
    if ended is None:
        return None
    seconds = max(0.0, ended - started)
    return {
        "usd": round(rate * seconds / 3600, 4),
        "seconds": round(seconds, 1),
        "rate_usd_per_hour": rate,
        "from_utc": epoch_utc(started),
        "to_utc": epoch_utc(ended) if deleted else None,
        "running": not deleted,
        "basis": "quoted rate times the time from the Pod's creation to its verified deletion "
        "(or now); storage and taxes are not included",
    }


def cost(folder):
    """The `cost` command: the stored price quote, the Pod's run time and the estimated compute cost."""
    from .status import resource_state

    folder = ledger_folder(folder)
    job = status_read(folder / "job.json", {})
    job = job if isinstance(job, dict) else {}
    cfg = job.get("config") if isinstance(job.get("config"), dict) else {}
    pod = resource_state(folder, job, "pod")
    estimate = compute_estimate(folder, job, pod)
    result = {
        "job_id": job.get("job_id"),
        "ledger": str(folder),
        "estimate": True,
        "pod": pod,
        "estimated_compute_usd": None,
        "run_seconds": None,
        "running": None,
        "from_utc": None,
        "to_utc": None,
        "price_quote": job.get("quote") if isinstance(job.get("quote"), dict) else None,
        "compute_ceiling_usd_per_hour": cfg.get("max_compute_usd_per_hour"),
        "compute_budget_usd": cfg.get("max_compute_usd"),
        "explanation": None,
        "note": NOTE,
    }
    if pod["state"] in ("not_created", "rejected"):
        result.update(
            estimated_compute_usd=0.0,
            run_seconds=0.0,
            running=False,
            explanation="No Pod was created, so no compute was billed.",
        )
    elif estimate is not None:
        result.update(
            estimated_compute_usd=estimate["usd"],
            run_seconds=estimate["seconds"],
            running=estimate["running"],
            from_utc=estimate["from_utc"],
            to_utc=estimate["to_utc"],
        )
    elif pod["state"] == "unknown":
        result["explanation"] = (
            "The Pod's create request has no known outcome, so a Pod may exist and bill; "
            "there is nothing to estimate from. See status."
        )
    else:
        result["explanation"] = (
            "This ledger has no price quote or no Pod creation time to estimate from."
        )
    return result


def _duration(seconds):
    seconds = int(round(seconds or 0))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def cost_lines(result):
    """The cost as a few lines for people."""

    def usd(value):
        return "?" if value is None else f"{value:.4f} USD"

    quote = result.get("price_quote") or {}
    if result["estimated_compute_usd"] is None:
        lines = [f"Job {result['job_id']}: no compute cost estimate. {result['explanation']}"]
    else:
        lines = [
            (
                f"Job {result['job_id']}: estimated compute cost "
                f"{usd(result['estimated_compute_usd'])} (an estimate, not a bill)."
            )
        ]
        if result["explanation"]:
            lines.append("  " + result["explanation"])
        else:
            span = (
                f"from {result['from_utc']} to {result['to_utc']}"
                if result["to_utc"]
                else f"since {result['from_utc']}, and it still runs"
            )
            lines.append(
                f"  The Pod ran {_duration(result['run_seconds'])} ({span}) at the quoted "
                f"{quote.get('compute_usd_per_hour')} USD per hour "
                f"(checked {quote.get('checked_utc')})."
            )
    lines.append(
        f"  Ceiling {result['compute_ceiling_usd_per_hour']} USD per hour; compute budget "
        f"{result['compute_budget_usd']} USD."
    )
    lines.append("  " + result["note"])
    return lines
