"""The job ledger folder: finding it without creating anything, reading records, the watchdog log."""

from pathlib import Path
import datetime
import math
import os
import re

from ..podrt.records import read, utc
from ..settings import jobs_dir

# The start of a job ID that names a job: at least four of its hex digits, like a short commit ID.
JOB_PREFIX = re.compile(r"[0-9a-f]{4,32}")


def ledger_folder(path):
    """The job folder, or ValueError. Creates nothing: no folder, no lock file."""
    folder = Path(path).resolve()
    if not (folder / "job.json").is_file():
        raise ValueError(
            "No job ledger at "
            + str(folder)
            + " (job.json is missing). Nothing was created or changed."
        )
    return folder


def utc_epoch(text):
    """Seconds since the epoch of an ISO 8601 time with a zone, else None."""
    try:
        stamp = datetime.datetime.fromisoformat(text)
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def epoch_utc(value):
    """An epoch time as ISO 8601 UTC, else None."""
    if type(value) in (int, float) and math.isfinite(value):
        return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat()
    return None


def job_folders(jobs=None):
    """The ledger folders (with a job.json) in the state folder's jobs folder, newest first. Reads only."""
    try:
        entries = list(Path(jobs_dir() if jobs is None else jobs).iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    folders = [entry for entry in entries if (entry / "job.json").is_file()]

    def created(folder):
        job = status_read(folder / "job.json", {})
        stamp = utc_epoch(job.get("created_utc")) if isinstance(job, dict) else None
        if stamp is None:
            try:
                stamp = (folder / "job.json").stat().st_mtime
            except OSError:
                stamp = 0.0
        return stamp, folder.name

    return sorted(folders, key=created, reverse=True)


def find_job(name):
    """The job ledger a command names: a ledger folder's path; `latest`, the newest ledger in the state folder;
    or the start of a job ID there (at least four hex digits, matching one job). Creates nothing."""
    text = str(name)
    if (Path(text) / "job.json").is_file() or not (
        text == "latest" or JOB_PREFIX.fullmatch(text.lower())
    ):
        return ledger_folder(text)
    jobs = jobs_dir()
    folders = job_folders(jobs)
    if text == "latest":
        if not folders:
            raise ValueError(
                "No job ledgers in "
                + str(jobs)
                + ", so there is no latest job. Nothing was created "
                "or changed."
            )
        return ledger_folder(folders[0])
    matches = [folder for folder in folders if folder.name.startswith(text.lower())]
    if len(matches) == 1:
        return ledger_folder(matches[0])
    if matches:
        raise ValueError(
            f"{len(matches)} jobs in {jobs} start with {text}: "
            + ", ".join(folder.name for folder in matches[:5])
            + ". Give more of the ID."
        )
    raise ValueError(
        f"No job in {jobs} has an ID that starts with {text}, and there is no job ledger at "
        f"{Path(text).resolve()}. Nothing was created or changed."
    )


def watch_log(folder, text):
    """Append one line to watchdog.log. Best effort; callers pass only redacted text."""
    try:
        with (Path(folder) / "watchdog.log").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                utc() + " pid " + str(os.getpid()) + ": " + " ".join(str(text).split()) + "\n"
            )
    except Exception:
        pass


def status_read(path, default=None):
    try:
        return read(path, default)
    except (OSError, ValueError):
        return default
