"""events.jsonl: one append-only line per thing that happened to a job, written by every process that acts on it.

Each line is a JSON object: utc, event, pid, the command of the process (launch, watchdog, cancel, ...) and the
event's own fields. Every value passes the one redaction point, clean(): IDs and names the code sets are kept as
they are while they look like identifiers and are no known secret; every other string goes through the same
redaction as the API errors, with this process's own credential values removed first. Writing is best effort and never stops cleanup. Appends from the controller and
the watchdog are serialized by events.lock, because Windows does not append atomically across processes.
"""

from pathlib import Path
import json
import math
import os
import re
import time

from .podrt.api import _redact
from .podrt.records import exclusive_lock, utc

FILE = "events.jsonl"

# The code supplies these IDs and names. Keep a value when it looks like an identifier
# and is not a known secret. Redact every other string, masking long tokens.
NAMED_FIELDS = frozenset(
    {
        "event",
        "command",
        "job_id",
        "pod_id",
        "volume_id",
        "kind",
        "state",
        "audit",
        "source",
        "phase",
        "training_outcome",
        "error_type",
        "provider_code",
        "method",
        "runner_version",
    }
)
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
TEXT_LIMIT = 1000
_process = {"command": None, "secrets": None}


def configure(command=None):
    """Name the command this process runs, for the events it writes."""
    _process["command"] = command


def _secrets():
    """The credential values this process resolved, once; never written, only removed from text."""
    if _process["secrets"] is None:
        try:
            from .credentials import resolve_credentials

            _process["secrets"] = [value for value in resolve_credentials().values() if value]
        except Exception:
            _process["secrets"] = []
    return _process["secrets"]


def clean(name, value, secrets):
    """The one redaction point: what may be written for field `name`."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if name in NAMED_FIELDS and IDENTIFIER.fullmatch(value) and value not in secrets:
            return value
        return _redact(value, secrets, TEXT_LIMIT)
    if isinstance(value, dict):
        return {str(key): clean(str(key), item, secrets) for key, item in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [clean(name, item, secrets) for item in list(value)[:50]]
    return type(value).__name__


def record(folder, event, **fields):
    """Append one event to the job's events.jsonl. Best effort: returns False instead of raising."""
    try:
        secrets = _secrets()
        line = {
            "utc": utc(),
            "event": clean("event", event, secrets),
            "pid": os.getpid(),
            "command": clean("command", _process["command"], secrets),
        }
        line.update(
            {key: clean(key, value, secrets) for key, value in fields.items() if key not in line}
        )

        data = (json.dumps(line, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
        folder = Path(folder)
        if not (folder / "job.json").is_file():
            return False  # never create a ledger folder
        for attempt in range(4):
            written = False
            try:
                try:
                    with exclusive_lock(folder / "events.lock", timeout=2):
                        _append(folder / FILE, data)
                        written = True
                except TimeoutError:
                    if not written:
                        _append(folder / FILE, data)  # better a rare interleaving than a lost event
                        written = True
                return True
            except OSError:
                if written:
                    return True  # the line is there; only releasing the lock failed
                time.sleep(0.05 * (attempt + 1))  # a passing sharing violation (Windows): try again
        return False
    except Exception:
        return False


def _append(path, data):
    """One write of the whole line. Raises only before anything was written (or if the write itself fails)."""
    descriptor = os.open(
        path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600
    )
    try:
        os.write(descriptor, data)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def read_events(folder, limit=50):
    """The last `limit` events (all when 0), read in one go; unreadable lines are skipped. Reads only."""
    try:
        with open(Path(folder) / FILE, "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - (1 << 20)))
            data = stream.read()
    except FileNotFoundError:
        return []
    events = []
    for row in data.decode("utf-8", errors="replace").splitlines():
        try:
            item = json.loads(row)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events[-limit:] if limit else events
