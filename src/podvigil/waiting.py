"""Read a job until status says it ended; never take over its supervision."""

import time

from .outcomes import of_ended_job, remains
from .status import status, watchdog_heard

POLL_SECONDS = 5


def wait_for_job(folder, timeout_minutes=None, *, progress):
    """Return (the status document, outcome). All job access is through read-only views."""
    end = None if timeout_minutes is None else time.monotonic() + timeout_minutes * 60
    phase = None
    while True:
        result = status(folder)
        if result["phase"] != phase:
            phase = result["phase"]
            progress("Job phase: " + phase + ".")
        if result["ended"]:
            return result, of_ended_job(folder)
        if not watchdog_heard(folder):
            # Recovery can finish between the status read and the heartbeat read.
            result = status(folder)
            if result["ended"]:
                if result["phase"] != phase:
                    progress("Job phase: " + result["phase"] + ".")
                return result, of_ended_job(folder)
            progress(
                "Stopped waiting: the job has not ended and its watchdog is no longer heard. "
                "The job is unaffected; the closing lines show what its ledger says exists."
            )
            return result, remains(folder) or "error"
        remaining = None if end is None else end - time.monotonic()
        if remaining is not None and remaining <= 0:
            progress("Wait timed out; the job is unaffected.")
            return result, "wait_timeout"
        time.sleep(POLL_SECONDS if remaining is None else min(POLL_SECONDS, remaining))
