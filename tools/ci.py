"""Credential-free local checks shared by GitHub Actions and developers. Never launches cloud jobs."""

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath
import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SECRET = re.compile(
    rb"(?:rpa|rps)_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|"
    rb"github_pat_[A-Za-z0-9_]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def tracked():
    return (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        .decode()
        .strip("\0")
        .split("\0")
    )


def forbidden(name):
    path = PurePosixPath(name)
    return (
        any(
            part in {".git", ".venv", ".venv-ci", "output", "downloads", "__pycache__"}
            for part in path.parts
        )
        or (path.name.startswith(".env") and name != ".env.example")
        or path.name == "credentials.env"
        or path.suffix in {".pem", ".key"}
    )


def version():
    for node in ast.parse((ROOT / "src/podvigil/__init__.py").read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            if re.fullmatch(r"\d+\.\d+\.\d+", value):
                return value
    raise RuntimeError("Missing or invalid package version")


def audit():
    problems = []
    for name in tracked():
        if forbidden(name):
            problems.append(name + ": private/generated file is tracked")
        if SECRET.search((ROOT / name).read_bytes()):
            problems.append(name + ": possible credential (value withheld)")
    if problems:
        raise RuntimeError("\n".join(problems))
    subprocess.run(
        [
            "git",
            "diff",
            "--check",
            subprocess.check_output(
                ["git", "hash-object", "-t", "tree", "--stdin"], input=b"", cwd=ROOT
            )
            .decode()
            .strip(),
            "--",
            ".",
        ],
        cwd=ROOT,
        check=True,
    )
    print("Tracked-file credential scan and maintained-file whitespace check passed.")


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def tests(lane):
    sys.path.insert(0, str(ROOT))
    os.environ.pop("PAID_SMOKE_JOB", None)
    os.environ.pop("PAID_SMOKE_ACCEPT_CHARGES", None)
    from tests.run_validation import RecordedResult, git_state, source_hashes

    start = ROOT / "tests" if lane == "all" else ROOT / "tests" / lane
    suite = unittest.defaultTestLoader.discover(str(start), top_level_dir=str(ROOT))
    ids = [test.id() for test in flatten(suite)]
    if not ids or any(name.startswith("tests.live.") for name in ids):
        raise RuntimeError("Empty suite or live cloud test collected")
    before = source_hashes()
    before_git = git_state()
    output = ROOT / "output/ci"
    output.mkdir(parents=True, exist_ok=True)
    with (output / (lane + "-tests.log")).open("w", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            result = unittest.TextTestRunner(
                stream=log, verbosity=2, resultclass=RecordedResult
            ).run(suite)
    after = source_hashes()
    after_git = git_state()
    report = {
        "lane": lane,
        "python": sys.version,
        "tests_run": result.testsRun,
        "passed": len(result.passed_ids),
        "skipped": [{"test": t.id(), "reason": why} for t, why in result.skipped],
        "failures": [{"test": t.id(), "detail": detail} for t, detail in result.failures],
        "errors": [{"test": t.id(), "detail": detail} for t, detail in result.errors],
        "source_sha256": after,
        "source_changed": before != after,
        "git_before_run": before_git,
        "git": after_git,
        "git_changed_during_run": before_git != after_git,
        "successful": result.wasSuccessful() and before == after and before_git == after_git,
        "paid_resources_created": False,
    }
    (output / (lane + "-tests.json")).write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: report[k] for k in ("lane", "tests_run", "passed", "successful", "source_changed")}
            | {
                "skipped": len(result.skipped),
                "failures": len(result.failures),
                "errors": len(result.errors),
            }
        )
    )
    if not report["successful"]:
        for test, detail in result.failures + result.errors:
            print(test.id() + "\n" + detail)
        raise RuntimeError("Local test suite failed; see output/ci")


def package_members(path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return [
                (name, archive.read(name)) for name in archive.namelist() if not name.endswith("/")
            ]
    with tarfile.open(path, "r:gz") as archive:
        return [
            (member.name, archive.extractfile(member).read())
            for member in archive.getmembers()
            if member.isfile()
        ]


def inspect_distribution(path):
    for name, data in package_members(path):
        parts = PurePosixPath(name).parts
        normalized = "/".join(parts[1:]) if path.name.endswith(".tar.gz") else name
        if (
            name.startswith("/")
            or ".." in parts
            or forbidden(normalized)
            or "podwarden" in parts
            or SECRET.search(data)
        ):
            raise RuntimeError("Unexpected or private distribution member: " + name)


def smoke():
    expected = version()
    wheel = ROOT / "dist" / ("podvigil-" + expected + "-py3-none-any.whl")
    sdist = ROOT / "dist" / ("podvigil-" + expected + ".tar.gz")
    for path in (wheel, sdist):
        inspect_distribution(path)
    with tempfile.TemporaryDirectory(prefix="podvigil-ci-wheel-") as tmp:
        folder = Path(tmp).resolve()
        assert folder.parent == Path(tempfile.gettempdir()).resolve()
        subprocess.run([sys.executable, "-m", "venv", str(folder / "env")], check=True)
        bin_dir = folder / "env" / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        command = bin_dir / ("podvigil.exe" if os.name == "nt" else "podvigil")
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--disable-pip-version-check",
                str(wheel),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "PYTHONPATH"
            and not k.startswith(("RUNPOD_", "PODVIGIL_", "PODWARDEN_", "WONDOMON_"))
        }
        env.update(
            PYTHONDONTWRITEBYTECODE="1",
            PODVIGIL_STATE_DIR=str(folder / "state"),
            PODVIGIL_ENV_FILE=str(folder / "missing-credentials.env"),
        )

        def run(args):
            return subprocess.check_output(
                [str(a) for a in args], cwd=folder, env=env, text=True
            ).strip()

        package = Path(
            run(
                [
                    python,
                    "-c",
                    "import pathlib,podvigil;print(pathlib.Path(podvigil.__file__).parent)",
                ]
            )
        )

        def snapshot():
            return {
                str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in package.rglob("*")
                if p.is_file()
            }

        before = snapshot()
        for args in ([command, "--version"], [python, "-m", "podvigil", "--version"]):
            if run(args) != "podvigil " + expected:
                raise RuntimeError("Installed command reports the wrong name/version")
        examples = sorted([*ROOT.glob("examples/**/job.json"), *ROOT.glob("jobs/*.example.json")])
        if not examples:
            raise RuntimeError("No example jobs found")
        for example in examples:
            run([command, "plan", example])
        subprocess.run(
            [str(python), "-I", "-B", str(ROOT / "tests/integration/wheel_smoke.py"), "-v"],
            cwd=folder,
            env=env,
            check=True,
        )
        if before != snapshot() or (folder / "state").exists():
            raise RuntimeError("Offline commands modified the installed package or created state")
    (ROOT / "dist/SHA256SUMS").write_text(
        "".join(
            hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n"
            for p in (wheel, sdist)
        ),
        encoding="utf-8",
    )
    print(
        "Wheel installed outside the checkout; entry points, example plans and local workload lifecycle passed."
    )


def container_suite(image):
    with tempfile.TemporaryDirectory(prefix="podvigil-ci-source-") as tmp:
        source = Path(tmp).resolve()
        assert source.parent == Path(tempfile.gettempdir()).resolve()
        for name in tracked():
            if forbidden(name):
                raise RuntimeError("Refusing to stage private file: " + name)
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        output = ROOT / "output/ci"
        output.mkdir(parents=True, exist_ok=True)
        for init in (False, True):
            args = ["docker", "run", "--rm", "--pull=never", "--network", "none"]
            if init:
                args.append("--init")
            args += [
                "--mount",
                "type=bind,source=" + str(source) + ",target=/src,readonly",
                "--entrypoint",
                "sh",
                image,
                "-c",
                "cp -r /src /work && cd /work && python3 -B -m unittest discover -s tests -v",
            ]
            log = output / ("linux-root-init-" + str(init).lower() + ".log")
            with log.open("w", encoding="utf-8") as stream:
                result = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT, timeout=900)
            print("Linux root suite (init=" + str(init) + "): " + str(result.returncode))
            if result.returncode:
                print("\n".join(log.read_text(encoding="utf-8").splitlines()[-65:]))
                raise RuntimeError("Linux container suite failed")


def release_check(tag):
    if tag != "v" + version():
        raise RuntimeError("Release tag must match the package version exactly")
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], cwd=ROOT, check=True
    )
    for path in (ROOT / "dist").glob("*"):
        if path.suffix == ".whl" or path.name.endswith(".tar.gz"):
            inspect_distribution(path)
    print("Version tag matches; the release commit is on main.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("audit", "test", "smoke", "container-suite", "release-check", "version")
    )
    parser.add_argument(
        "--lane", choices=("all", "unit", "fakecloud", "integration", "container"), default="all"
    )
    parser.add_argument("--image")
    parser.add_argument("--tag")
    args = parser.parse_args()
    if args.command == "audit":
        audit()
    elif args.command == "test":
        tests(args.lane)
    elif args.command == "smoke":
        smoke()
    elif args.command == "container-suite":
        if not args.image:
            parser.error("--image is required")
        container_suite(args.image)
    elif args.command == "release-check":
        release_check(args.tag)
    else:
        print(version())


if __name__ == "__main__":
    main()
