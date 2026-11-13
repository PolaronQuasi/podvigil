"""What a job has logged so far: the watchdog log, the failure records and, once the results are downloaded,
the workload log. Reads only: each file is read in one go and closed at once, never held open, so a running
watchdog can keep replacing its records (Windows refuses to replace a file another process holds open).
"""

from pathlib import Path
import json
import os

from .controller.ledger import ledger_folder, status_read
from .events import read_events

READ_LIMIT = 1 << 20  # at most this many bytes from the end of a log
POD_RECORDS = ("control/guard-failure.json", "control/finalization-incomplete.json")
WORKLOAD_LOG = "artifacts/train.log"


def tail(path, lines=100, limit=READ_LIMIT):
    """The last `lines` lines (all when 0) of a text file, from at most `limit` bytes at its end; None if absent."""
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            start = max(0, size - limit)
            stream.seek(start)
            data = stream.read(limit)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None
    text = data.decode("utf-8", errors="replace")
    if start:
        text = text.partition("\n")[2]  # the first line read is cut
    rows = text.splitlines()
    shown = rows[-lines:] if lines else rows
    return {
        "path": str(path),
        "bytes": size,
        "lines": shown,
        "truncated": bool(start) or len(shown) < len(rows),
    }


def _inside(folder, path):
    try:
        return Path(path).resolve().is_relative_to(folder)
    except (OSError, ValueError):
        return False


def _copies(folder):
    """(volume key, local copy) for every volume object the ledger holds a local copy of: the delete audit's
    copies under downloads/ or partial-downloads/volume-audit-*, and partial exports. Only paths inside the
    ledger are used."""
    found = []
    indexes = [
        index
        for parent in ("downloads", "partial-downloads")
        for index in folder.glob(parent + "/volume-audit-*/objects.json")
    ]
    for index in sorted(indexes):
        record = status_read(index, {})
        for item in record.get("objects", []) if isinstance(record, dict) else []:
            if (
                isinstance(item, dict)
                and isinstance(item.get("key"), str)
                and isinstance(item.get("local_path"), str)
            ):
                local = Path(item["local_path"])
                local = local if _inside(folder, local) else index.parent / local.name
                found.append((item["key"], local))
    for mapping in sorted(folder.glob("*/partial-export-*/objects.jsonl")):
        try:
            rows = mapping.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for row in rows:
            try:
                item = json.loads(row)
            except ValueError:
                continue
            if (
                isinstance(item, dict)
                and item.get("status") == "saved"
                and isinstance(item.get("key"), str)
            ):
                found.append((item["key"], mapping.parent / str(item.get("local_path"))))
    return [(key, local) for key, local in found if _inside(folder, local)]


def logs(folder, lines=100):
    """The `logs` command: everything the ledger holds now that tells what happened."""
    folder = ledger_folder(folder)
    job = status_read(folder / "job.json", {})
    job_id = job.get("job_id") if isinstance(job, dict) else None
    prefix = "jobs/" + str(job_id) + "/"
    copies = _copies(folder)
    pod_records = {}
    for key, local in copies:
        for name in POD_RECORDS:
            if key == prefix + name and name not in pod_records:
                pod_records[name] = status_read(local)
    workload = tail(folder / "downloads" / WORKLOAD_LOG, lines)
    if workload is None:
        saved = [local for key, local in copies if key == prefix + WORKLOAD_LOG]
        workload = tail(saved[0], lines) if saved else None
    result = {
        "job_id": job_id,
        "ledger": str(folder),
        "events": read_events(folder, lines),
        "watchdog_log": tail(folder / "watchdog.log", lines),
        "launch_failure": status_read(folder / "launch-failure.json"),
        "recovery": status_read(folder / "recovery-handoff.json"),
        "volume_kept": status_read(folder / "volume-kept.json"),
        "pod_records": pod_records or None,
        "workload_log": workload,
        "not_yet": [],
    }
    if result["watchdog_log"] is None:
        result["not_yet"].append("watchdog.log: the watchdog has written nothing yet.")
    if workload is None:
        result["not_yet"].append(
            "The workload log stays on the Pod while the job runs; it is downloaded with the "
            "results when the Pod has ended (recover, or the watchdog's recovery)."
        )
    return result


def logs_lines(result):
    """The logs as text sections for people."""
    lines = []

    def section(title, log):
        lines.append(
            f"== {title} ({log['path']}" + (", end only" if log["truncated"] else "") + ") =="
        )
        lines.extend(log["lines"] or ["(empty)"])

    if result["events"]:
        lines.append("== events (events.jsonl) ==")
        for item in result["events"]:
            details = " ".join(
                f"{key}={json.dumps(value)}"
                for key, value in item.items()
                if key not in ("utc", "event", "pid", "command")
            )
            lines.append(
                (
                    f"{item.get('utc')} {item.get('command')}[{item.get('pid')}] "
                    f"{item.get('event')} {details}"
                ).rstrip()
            )
    if result["watchdog_log"]:
        section("watchdog log", result["watchdog_log"])
    for title, record in (
        ("launch failure", result["launch_failure"]),
        ("watchdog recovery", result["recovery"]),
        ("why the volume was kept", result["volume_kept"]),
    ):
        if isinstance(record, dict):
            lines.append(f"== {title} ==")
            lines.append(json.dumps(record, indent=2))
    for name, record in (result["pod_records"] or {}).items():
        lines.append(f"== Pod record {name} (copied from the volume) ==")
        lines.append(json.dumps(record, indent=2))
    if result["workload_log"]:
        section("workload log", result["workload_log"])
    for note in result["not_yet"]:
        lines.append("Not available yet: " + note)
    return lines
