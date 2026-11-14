"""Secure Cloud jobs with independent cleanup, durable results and verified backup.

`plan` is offline. Only `launch` creates resources. No retries of ambiguous POSTs.
"""

from pathlib import Path
import argparse
import json
import math
import sys

from . import NAME, __version__, events, settings
from .controller.capacity import CapacityWaitInterrupted, launch_with_capacity_wait
from .controller.launch import definite_no_capacity, launch_candidates, plan_job
from .controller.ledger import find_job, status_read
from .controller.operator_commands import (
    cancel,
    handoff,
    reattach,
    resolve_creation,
    resume_monitoring,
)
from .controller.recovery import accept_partial, export_partial, export_unresolved, recover
from .controller.watchdog import watchdog
from .controller.watchdog_spawn import report_unseen_ledger
from .cost import cost, cost_lines
from .credentials import load_key
from .errors import public_message
from .jobspec import read_job
from .localrun import run_local
from .logs import logs, logs_lines
from .outcomes import EXIT_CODES, MEANINGS, of_failure, of_result
from .provider.catalog import survey, survey_lines
from .provider.client import configured_api
from .status import NOTHING_CREATED, job_list, list_lines, resource_state, state_lines, status
from .waiting import wait_for_job

# The codes by name are for callers that used them before the table in outcomes.OUTCOMES.
EXIT_WORKLOAD_FAILED = EXIT_CODES["workload_failed"]
EXIT_NO_CAPACITY = EXIT_CODES["no_capacity"]
EXIT_STORAGE_RETAINED = EXIT_CODES["storage_retained"]
EXIT_DETACHED = EXIT_CODES["detached"]
EXIT_CANCEL_PENDING = EXIT_CODES["pending"]

JOB_HELP = (
    "the job: its ledger folder, `latest` (the newest job in the state folder), "
    "or the start of its ID (4 or more characters)"
)


def say(text):
    """Progress, warnings and errors for people go to standard error; standard output carries the result."""
    print(text, file=sys.stderr, flush=True)


def print_text(lines):
    """Print text that holds other programs' output (logs) even where the console cannot show every character."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="backslashreplace")
        except (TypeError, ValueError):
            pass
    print("\n".join(lines), flush=True)


class VersionAction(argparse.Action):
    """Print the version without argparse's terminal-width-dependent help formatting."""

    def __call__(self, parser, namespace, values, option_string=None):
        print(NAME + " " + __version__)
        parser.exit()


def minutes(value):
    """A finite, nonnegative allowance; fractions of a minute are supported."""
    try:
        number = float(value)
    except ValueError:
        number = -1
    if not math.isfinite(number) or number < 0 or not math.isfinite(number * 60):
        raise argparse.ArgumentTypeError("MINUTES must be a finite, nonnegative number.")
    return number


def build_parser():
    # Options every command takes, before or after its name.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--state-dir",
        default=argparse.SUPPRESS,
        metavar="FOLDER",
        help="folder for new job ledgers and the API pacing file (default: "
        + settings.STATE_DIR_VARIABLE
        + ", else a per-user folder)",
    )
    common.add_argument(
        "--env-file",
        default=argparse.SUPPRESS,
        metavar="FILE",
        help="the credentials file (default: the file "
        + settings.ENV_FILE_VARIABLE
        + " names, else the per-user file, else the repository's .env)",
    )
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    parser.add_argument(
        "--version", action=VersionAction, nargs=0, help="show program's version number and exit"
    )
    commands = parser.add_subparsers(dest="action", required=True)

    for name in ("plan", "launch"):
        p = commands.add_parser(name, parents=[common])
        p.add_argument("config")
        if name == "launch":
            p.add_argument(
                "--wait-for-capacity",
                type=minutes,
                metavar="MINUTES",
                help="after proven no capacity and cleanup, poll the catalog for up to MINUTES "
                "before another attempt; waits in the foreground even with --detach",
            )
            p.add_argument(
                "--detach",
                action="store_true",
                help="return once the workload runs: the watchdog alone enforces the same deadline "
                "and recovers the results",
            )

    for name in (
        "watchdog",
        "recover",
        "export-partial",
        "export-unresolved",
        "reattach",
        "resume-monitoring",
        "status",
        "wait",
    ):
        p = commands.add_parser(name, parents=[common])
        p.add_argument("job_directory", metavar="JOB", help=JOB_HELP)
        if name == "wait":
            p.add_argument(
                "--timeout",
                type=minutes,
                metavar="MINUTES",
                help="stop waiting after MINUTES; default: no limit; the job is unaffected",
            )
        if name == "recover":
            p.add_argument(
                "--accept-partial",
                action="store_true",
                help="when the workload's outputs are incomplete: copy every object on the volume "
                "to the ledger with its bytes checked, then delete the volume",
            )

    p = commands.add_parser(
        "list", help="read-only: the jobs in the state folder, newest first", parents=[common]
    )
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")

    p = commands.add_parser(
        "logs", help="read-only: what the job has logged so far", parents=[common]
    )
    p.add_argument("job_directory", metavar="JOB", help=JOB_HELP)
    p.add_argument(
        "--tail",
        type=int,
        default=100,
        metavar="LINES",
        help="show the last LINES lines of each log (0: all of the last MiB; default 100)",
    )
    p.add_argument("--json", action="store_true", help="print JSON instead of text")

    p = commands.add_parser(
        "cost", help="read-only: the job's estimated compute cost", parents=[common]
    )
    p.add_argument("job_directory", metavar="JOB", help=JOB_HELP)
    p.add_argument("--json", action="store_true", help="print JSON instead of text")

    p = commands.add_parser(
        "run-local",
        parents=[common],
        help="run the job's workload on this computer under the workload contract: no cloud "
        "call, no isolation",
    )
    p.add_argument("config")
    p.add_argument(
        "--workdir",
        metavar="FOLDER",
        help="a new or empty folder for the run (default: a new temporary folder, kept afterwards)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        metavar="SECONDS",
        help="stop the workload after SECONDS (default: the workload's share of the job's time)",
    )
    p.add_argument(
        "--python", metavar="PATH", help="run a python3 or python command with this Python"
    )

    p = commands.add_parser(
        "cancel",
        parents=[common],
        help="stop the job early: a running Pod is asked to save its outputs first, then the "
        "watchdog cleans up as at the deadline",
    )
    p.add_argument("job_directory", metavar="JOB", help=JOB_HELP)
    p.add_argument(
        "--wait",
        type=float,
        metavar="SECONDS",
        help="wait up to SECONDS for the end (default: the job's finalize grace plus 150 seconds; "
        "0 returns at once)",
    )

    p = commands.add_parser(
        "survey",
        help="read-only: GPU types with Secure Cloud capacity now, and where",
        parents=[common],
    )
    p.add_argument("--gpu-count", type=int, default=1)
    p.add_argument(
        "--min-cuda-version",
        metavar="MAJOR.MINOR",
        help="the lowest host CUDA version (default: the runner's CUDA floor, "
        + settings.CUDA_FLOOR_VARIABLE
        + ", else 12.8)",
    )
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")

    p = commands.add_parser("resolve-creation", parents=[common])
    p.add_argument("job_directory", metavar="JOB", help=JOB_HELP)
    identity = p.add_mutually_exclusive_group(required=True)
    identity.add_argument("--pod-id")
    identity.add_argument("--volume-id")
    identity.add_argument("--audit", action="store_true")

    p = commands.add_parser("hand-off", parents=[common])
    p.add_argument("job_directory", metavar="JOB", help=JOB_HELP)
    p.add_argument("--note", required=True)
    p.add_argument("--acknowledge-unverified-billing", action="store_true", required=True)

    return parser


def main(argv=None, context=None):
    """Run one command. Its result goes to context["result"] (printed as JSON) and, for a view asked for as text,
    context["text"] (printed as it is); context["ledgers"] collects the job folders it touched."""
    context = {} if context is None else context
    ledgers = context.setdefault("ledgers", [])
    args = build_parser().parse_args(argv)
    settings.configure(
        state_dir=getattr(args, "state_dir", None),
        env_file=getattr(args, "env_file", None),
        program=context.get("program"),
    )
    context["action"] = args.action
    events.configure(args.action)
    folder = None
    if getattr(args, "job_directory", None) is not None:
        try:
            folder = find_job(args.job_directory)  # fails before creating anything
        except ValueError:
            if args.action == "watchdog":
                # An arming controller learns where this watchdog looked.
                report_unseen_ledger(args.job_directory)
            raise
        ledgers.append(folder)
        if not (Path(args.job_directory) / "job.json").is_file():
            # The job was named by the start of its ID or as latest.
            say("Job " + folder.name + ": " + str(folder))

    result = text = None
    if args.action == "watchdog":
        watchdog(folder)
    elif args.action == "status":
        result = status(folder)
    elif args.action == "wait":
        result, context["outcome"] = wait_for_job(folder, args.timeout, progress=say)
    elif args.action == "list":
        rows = job_list()
        # The folder by its real location, as every ledger line prints it (settings.real_path).
        jobs_folder = settings.real_path(settings.jobs_dir())
        result = {"jobs_dir": str(jobs_folder), "jobs": rows}
        text = None if args.json else list_lines(rows, jobs_folder)
    elif args.action == "logs":
        if args.tail < 0:
            raise ValueError("--tail takes 0 (everything) or a positive number of lines.")
        result = logs(folder, args.tail)
        text = None if args.json else logs_lines(result)
    elif args.action == "cost":
        result = cost(folder)
        text = None if args.json else cost_lines(result)
    elif args.action == "run-local":
        # Candidates share the workload; the first one's limits apply.
        result = run_local(read_job(args.config)[0], args.workdir, args.timeout, args.python)
    elif args.action == "cancel":
        result = cancel(folder, args.wait)
    elif args.action == "survey":
        result = survey(configured_api(load_key()), args.gpu_count, args.min_cuda_version)
        text = None if args.json else survey_lines(result)
    elif args.action == "recover":
        result = accept_partial(folder) if args.accept_partial else recover(folder)
    elif args.action == "export-partial":
        result = export_partial(folder)
    elif args.action == "export-unresolved":
        result = export_unresolved(folder)
    elif args.action == "hand-off":
        result = handoff(folder, args.note, args.acknowledge_unverified_billing)
    elif args.action == "resume-monitoring":
        result = resume_monitoring(folder)
    elif args.action == "reattach":
        result = reattach(folder)
    elif args.action == "resolve-creation":
        result = resolve_creation(folder, args.pod_id, volume_id=args.volume_id, audit=args.audit)
    else:
        context["configs"] = configs = read_job(args.config)
        if args.action == "plan":
            result = plan_job(configs)
        elif args.wait_for_capacity is None:
            result = launch_candidates(configs, ledgers, detach=args.detach)
        else:
            context["capacity"] = {}
            result = launch_with_capacity_wait(
                configs,
                ledgers,
                args.wait_for_capacity,
                detach=args.detach,
                details=context["capacity"],
                progress=say,
            )

    context["result"], context["text"] = result, text


def document(context, outcome, code, error):
    """The JSON for standard output: the command's result with its outcome, or, on failure, the outcome, the
    error and what the job's ledger shows."""
    if error is None and isinstance(context.get("result"), dict):
        return {
            **context["result"],
            **context.get("capacity", {}),
            "outcome": outcome,
            "exit_code": code,
        }
    record = {**context.get("capacity", {}), "outcome": outcome, "exit_code": code, "error": error}
    if context["ledgers"]:
        folder = context["ledgers"][-1]
        job = status_read(folder / "job.json", {})
        job = job if isinstance(job, dict) else {}
        record.update(
            ledger=str(folder),
            job_id=job.get("job_id"),
            pod=resource_state(folder, job, "pod"),
            volume=resource_state(folder, job, "volume"),
        )
    return record


def run_cli(argv=None, program=None):
    """The command line.

    Standard output carries the result: one JSON document with its named outcome and exit code
    (outcomes.OUTCOMES), or the text of a view asked for as text (list, logs, cost and survey without --json).
    Standard error carries progress, warnings and errors, and, last, two lines on what exists for the job the
    command touched: its Pod, then its volume. The process exits with the outcome's code.
    `program` is the command the user typed, which the advice in the messages names (settings.program).
    """
    context = {
        "ledgers": [],
        "action": None,
        "result": None,
        "text": None,
        "configs": None,
        "program": program,
    }
    error = None
    try:
        main(argv, context)
        outcome = context.get("outcome") or of_result(
            context["action"], context["result"] or {}, context["configs"]
        )
    except KeyboardInterrupt as exc:
        if context["action"] == "wait":
            say("Waiting interrupted; the job is unaffected.")
            if context["ledgers"]:
                context["result"] = status(context["ledgers"][-1])
        else:
            error = (
                str(exc)
                if isinstance(exc, CapacityWaitInterrupted)
                else (
                    "Interrupted; cleanup requested. Check the job ledger "
                    "before assuming billing has ended."
                )
            )
            say(error)
        outcome = "interrupted"
    except SystemExit as exc:
        if context["action"] is not None or exc.code in (0, None):
            raise  # --help and --version
        outcome, error = "usage_error", "The command line is wrong; its usage is on standard error."
    except Exception as exc:
        # Avoid reflecting third-party SDK tracebacks which may contain credentials.
        error = public_message(exc)
        say("RUN FAILED: " + error)
        outcome = context.get("capacity", {}).get("outcome") or of_failure(
            context["action"],
            exc,
            context["ledgers"][-1] if context["ledgers"] else None,
            definite_no_capacity(exc),
        )

    code = EXIT_CODES[outcome]
    if outcome == "outputs_lost":
        say("OUTPUTS LOST: " + MEANINGS[outcome] + ".")
    try:
        if context["text"] is not None and error is None:
            print_text(context["text"])
        elif not (context["action"] == "watchdog" and error is None):
            # The watchdog's output is its log.
            print(json.dumps(document(context, outcome, code, error), indent=2), flush=True)
    except Exception:
        pass

    try:
        if context["ledgers"]:
            lines = state_lines(context["ledgers"][-1])
        elif context["action"] == "launch":
            lines = NOTHING_CREATED  # launch only reads the provider before its ledger exists
        else:
            lines = []
        for line in lines:
            say(line)
    except Exception:
        pass
    settings.configure()  # the options apply to this command only
    events.configure()
    return code


if __name__ == "__main__":
    raise SystemExit(run_cli())
