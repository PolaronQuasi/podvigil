"""Compatibility entry point: `python managed_run.py <command> ...` runs the package's command line.

The runner is the package in src/. Installed (`pip install .`) it is the `podvigil` command and
`python -m podvigil`. This file stays so that existing callers and wrappers keep working unchanged;
it always runs the code of this checkout.
"""

from pathlib import Path
import importlib
import sys

PACKAGE = "podvigil"  # the package's import name: the folder src/<PACKAGE>

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    raise SystemExit(importlib.import_module(PACKAGE + ".cli").run_cli(program="managed_run.py"))
