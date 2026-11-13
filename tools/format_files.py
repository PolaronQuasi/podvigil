"""Format or check tracked source and documentation without reading private files.

Run with the Python environment containing requirements-ci.txt. Full checks also
need the pinned Node development dependencies installed by npm ci --ignore-scripts.
Stage new files before running so Git includes them in the formatting inventory.
"""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PRETTIER_EXTENSIONS = {".json", ".md", ".mjs", ".svg", ".yaml", ".yml"}


def tracked_files():
    result = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [name for name in result.decode("utf-8").split("\0") if name]


def run(arguments):
    result = subprocess.run(arguments, cwd=ROOT, check=False)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="apply the configured formatting")
    mode.add_argument(
        "--check", action="store_true", help="report formatting differences (default)"
    )
    parser.add_argument(
        "--python-only", action="store_true", help="run Ruff without Node dependencies"
    )
    args = parser.parse_args()

    files = tracked_files()
    python_files = [name for name in files if name.endswith(".py")]
    ruff = [sys.executable, "-m", "ruff", "format"]
    if not args.write:
        ruff.append("--check")
    status = run([*ruff, "--", *python_files])

    if not args.python_only:
        prettier = ROOT / "node_modules/prettier/bin/prettier.cjs"
        node = shutil.which("node")
        if node is None or not prettier.is_file():
            parser.exit(2, "Install Node and run npm ci --ignore-scripts before full formatting.\n")
        text_files = [
            name
            for name in files
            if Path(name).suffix in PRETTIER_EXTENSIONS and name != "package-lock.json"
        ]
        text_status = run(
            [node, str(prettier), "--write" if args.write else "--check", "--", *text_files]
        )
        if text_status:
            status = text_status

    return status


if __name__ == "__main__":
    raise SystemExit(main())
