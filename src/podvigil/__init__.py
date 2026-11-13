"""Unattended batch jobs on Runpod Secure Cloud Pods: a hard deadline, an independent watchdog, verified
results and verified clean-up.

PodVigil's command, environment variables and per-user folders derive from NAME below.
"""

NAME = __name__  # the package's import name, defined by its folder: src/<NAME>
__version__ = "0.1.0"  # the one version source; pyproject.toml reads it

# The layout of job.json and of the Pod spec. Raise it when a reader must tell old
# records from new ones. Ledgers written before 0.1.0 have no number.
LEDGER_SCHEMA = 1
