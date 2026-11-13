"""`python -m podvigil`: the same command line as the console command."""

from . import NAME
from .cli import run_cli

if __name__ == "__main__":
    raise SystemExit(run_cli(program="python -m " + NAME))
