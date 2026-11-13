"""Record the local suite and exact source hashes; never creates cloud resources."""

from datetime import datetime, timezone
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
import unittest

from tests.container import review_image

ROOT = Path(__file__).resolve().parents[1]

INPUT_DIRECTORIES = (
    "src",
    "tests",
    "examples",
    "jobs",
    "docs",
    ".gitea",
    ".github",
    "tools",
    "assets",
)
ROOT_INPUTS = {
    "pyproject.toml",
    "LICENSE",
    "NOTICE",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    "package.json",
    "package-lock.json",
}
EXCLUDED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "site-packages",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
    "output",
    "downloads",
    "htmlcov",
}


def excluded(path):
    """Do not follow links or fingerprint credentials, machine-local jobs, caches or dependencies."""
    name = path.name.lower()
    return (
        name in EXCLUDED_DIRECTORIES
        or name.endswith(".egg-info")
        or name.startswith(".env")
        or name == "credentials.env"
        or name.endswith(".local.json")
        or path.suffix.lower()
        in {".env", ".pem", ".key", ".p12", ".pfx", ".pyc", ".pyo", ".tmp", ".swp"}
        or path.is_symlink()
        or bool(
            getattr(path.lstat(), "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    )


def source_hashes(root=None):
    """Hash validation inputs, including docs and example data, also in archives without Git."""
    root = Path(root) if root is not None else ROOT
    paths = [
        path
        for path in root.iterdir()
        if not excluded(path)
        and path.is_file()
        and (
            path.name in ROOT_INPUTS
            or path.suffix in {".py", ".md"}
            or (path.name.startswith("requirements") and path.suffix == ".txt")
        )
    ]
    for name in INPUT_DIRECTORIES:
        directory = root / name
        if not directory.exists() or excluded(directory):
            continue
        for current, directories, files in os.walk(directory, followlinks=False):
            directories[:] = sorted(
                name for name in directories if not excluded(Path(current) / name)
            )
            paths.extend(
                path
                for name in files
                if not excluded(path := Path(current) / name) and path.is_file()
            )
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }


def git_state(root=None):
    """Record identity and dirtiness without storing file names, remotes or Git diagnostics.

    A source archive, or a machine without Git, has unknown (null) Git state, not a clean checkout.
    Git's optional index refresh is disabled so this evidence read does not change the checkout.
    """
    root = Path(root) if root is not None else ROOT
    unknown = {"commit": None, "dirty": None}
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS="0")

    def git(*arguments):
        return subprocess.run(
            ["git", "-C", str(root), "-c", "core.fsmonitor=false", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=10,
        )

    try:
        top = git("rev-parse", "--show-toplevel")
        if top.returncode or Path(top.stdout.strip()).resolve() != root.resolve():
            return unknown
        head = git("rev-parse", "--verify", "HEAD")
        status = git("status", "--porcelain=v1", "--untracked-files=all")
    except (OSError, subprocess.TimeoutExpired):
        return unknown
    return {
        "commit": head.stdout.strip() if head.returncode == 0 else None,
        "dirty": bool(status.stdout) if status.returncode == 0 else None,
    }


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed_ids = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed_ids.append(test.id())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output/validation")
    output = parser.parse_args().output_dir
    output.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    tick = time.monotonic()
    initial_hashes = source_hashes()
    initial_git = git_state()
    docker_image = review_image()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    with (output / "tests.log").open("w", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            result = unittest.TextTestRunner(
                stream=log, verbosity=2, resultclass=RecordedResult
            ).run(suite)
    hashes = source_hashes()
    final_git = git_state()
    changed = sorted(
        name
        for name in initial_hashes.keys() | hashes.keys()
        if initial_hashes.get(name) != hashes.get(name)
    )
    evidence = {
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - tick, 3),
        "tests_run": result.testsRun,
        "passed": len(result.passed_ids),
        "skipped": [{"test": t.id(), "reason": reason} for t, reason in result.skipped],
        "failures": [{"test": t.id(), "detail": detail} for t, detail in result.failures],
        "errors": [{"test": t.id(), "detail": detail} for t, detail in result.errors],
        "passed_tests": result.passed_ids,
        "successful": result.wasSuccessful() and not changed and initial_git == final_git,
        "source_changed_during_run": changed,
        "git": final_git,
        "git_before_run": initial_git,
        "git_changed_during_run": initial_git != final_git,
        "python": sys.version,
        "interpreter": sys.executable,
        "docker_image": docker_image,
        "source_sha256": hashes,
        "scope": "Local unit, loopback service, Windows process and optional network-disabled Linux container tests.",
        "provider_smoke_run": False,
        "unattended_cloud_readiness_proven": False,
    }
    (output / "validation.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {k: evidence[k] for k in ("tests_run", "passed", "duration_seconds", "successful")}
            | {
                "skipped": len(result.skipped),
                "failures": len(result.failures),
                "errors": len(result.errors),
            }
        )
    )
    return 0 if evidence["successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
