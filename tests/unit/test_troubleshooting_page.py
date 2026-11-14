"""docs/OPERATIONS.md keeps up with the code: the states and outcomes the code writes, and the messages it
raises or prints, appear on the page.

Two heuristic scans of src/ (adopted from the documentation work): state-like values written under keys such as
"state", "phase", "outcome" or "mode", and the longest literal piece of each raised or printed message. A value
or message that is not on the page fails the test, unless PENDING lists it: what the code has and the page does
not yet explain. An entry of PENDING that the page now covers, or that the code no longer has, fails too, so the
list only shrinks. Skipped when the page is not there (a source tree without the documentation).
"""

import ast
import re
import unittest
from textwrap import dedent

from tests.support.paths import ROOT

PAGE_PATH = ROOT / "docs" / "OPERATIONS.md"
STATE_KEYS = frozenset(
    {
        "state",
        "phase",
        "outcome",
        "status",
        "mode",
        "kind",
        "source",
        "established_by",
        "cleanup_state",
        "stage",
        "absence_basis",
        "request_outcome",
        "actor",
        "training_outcome",
    }
)
STATE_NAME = re.compile(r"[a-z][a-z_]+")
STATE_WORD = re.compile(r"[a-z_]+")
STATE_TEXT = re.compile(r"[a-z][a-z_ ]+")
REASON_TEXT = re.compile(r"[a-z][a-z_ /-]+")
CALL_ARGUMENTS = {
    "record": (0, STATE_WORD),
    "agent_status": (2, STATE_WORD),
    "record_stop": (1, None),
}


# Written by the code, not yet on the page. Remove an entry once the page explains it.
PENDING = {
    "states": set(),
    "messages": set(),
}


def sources():
    return sorted((ROOT / "src").rglob("*.py"))


def literal_choices(node, pattern):
    if isinstance(node, ast.IfExp):
        yield from literal_choices(node.body, pattern)
        yield from literal_choices(node.orelse, pattern)
    elif (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value
        and (pattern is None or pattern.fullmatch(node.value))
    ):
        yield node.value


def assigned_states(node):
    for target in node.targets:
        if isinstance(target, ast.Name):
            if target.id in STATE_KEYS:
                yield from literal_choices(node.value, STATE_NAME)
            elif target.id == "reason":
                value = node.value
                if (
                    isinstance(value, ast.BoolOp)
                    and isinstance(value.op, ast.Or)
                    and isinstance(value.values[0], ast.Name)
                    and value.values[0].id == "reason"
                ):
                    value = value.values[1]
                yield from literal_choices(value, REASON_TEXT)
        elif (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id == "phase"
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "value"
        ):
            yield from literal_choices(node.value, STATE_WORD)


def state_values(source):
    """Find literal states and reasons in code regardless of line breaks or quote style."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value in STATE_KEYS:
                    yield from literal_choices(value, STATE_TEXT)
        elif isinstance(node, ast.keyword) and node.arg in STATE_KEYS:
            yield from literal_choices(node.value, STATE_NAME)
        elif isinstance(node, ast.Assign):
            yield from assigned_states(node)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            argument = CALL_ARGUMENTS.get(node.func.id)
            if argument is not None:
                index, pattern = argument
                if len(node.args) > index:
                    yield from literal_choices(node.args[index], pattern)
        # Outside a known state field, keep the broad check for literal choices
        # with no strings in the test. This excludes choices between field names.
        elif isinstance(node, ast.IfExp) and not any(
            isinstance(part, ast.Constant) and isinstance(part.value, str)
            for part in ast.walk(node.test)
        ):
            body = list(literal_choices(node.body, STATE_WORD))
            otherwise = list(literal_choices(node.orelse, STATE_WORD))
            if body and otherwise:
                yield from body
                yield from otherwise


def states():
    found = {}
    for path in sources():
        for value in state_values(path.read_text(encoding="utf-8")):
            found.setdefault(value, set()).add(path.relative_to(ROOT).as_posix())
    return found


def pieces(node):
    """Literal text pieces of a message expression, in order; None marks a variable part."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        out = []
        for value in node.values:
            out.extend(pieces(value) if isinstance(value, ast.Constant) else [None])
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return pieces(node.left) + pieces(node.right)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return [
            p
            for part in pieces(node.func.value)
            for p in ([part] if part is None else re.split(r"(\{[^}]*\})", part))
        ]
    return [None]


def messages():
    """The longest literal piece of every raised or printed message of 12 or more characters, by where it is."""
    found = {}
    for path in sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            message = None
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
                message = node.exc.args[0]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("notice", "print")
                and node.args
            ):
                message = node.args[0]
            if message is None:
                continue
            parts = [
                p
                for p in pieces(message)
                if p is not None and not re.fullmatch(r"\{[^}]*\}", p or "")
            ]
            literal = max((" ".join(p.split()) for p in parts), key=len, default="").strip(
                " :;.,()"
            )
            if len(literal) >= 12:
                found.setdefault(literal, set()).add(
                    f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                )
    return found


class StateScannerTests(unittest.TestCase):
    def test_finds_wrapped_states_and_reasons(self):
        source = dedent(
            """
            record = {
                "state": "operator_resolution_needed"
                if overdue >= 300
                else "creation_unresolved",
            }
            state = (
                ("monitor_armed" if spawned else "existing_monitor_active")
                if occupied
                else "monitor_acknowledged_and_exited"
            )
            reason = reason or (
                "self-termination probe timeout" if probe else "job finished"
            )
            agent_status(
                job,
                pod_id,
                'guard_ready',
            )
            record_stop(
                folder,
                "recover/finish cleanup",
            )
            """
        )
        self.assertEqual(
            set(state_values(source)),
            {
                "operator_resolution_needed",
                "creation_unresolved",
                "monitor_armed",
                "existing_monitor_active",
                "monitor_acknowledged_and_exited",
                "self-termination probe timeout",
                "job finished",
                "guard_ready",
                "recover/finish cleanup",
            },
        )


@unittest.skipUnless(PAGE_PATH.is_file(), "docs/OPERATIONS.md is not in this source tree")
class TroubleshootingPageTests(unittest.TestCase):
    def setUp(self):
        self.page = PAGE_PATH.read_text(encoding="utf-8")
        self.flat = " ".join(self.page.split()).replace("\\|", "|")

    def check(self, kind, found, on_page):
        missing = {value for value in found if not on_page(value)}
        new = sorted(missing - PENDING[kind])
        if new:
            self.fail(
                "Not on docs/OPERATIONS.md; explain them there (or list them in PENDING): "
                + "; ".join(f"{value!r} ({', '.join(sorted(found[value]))})" for value in new)
            )
        stale = sorted(PENDING[kind] - missing)
        if stale:
            self.fail(
                f"PENDING lists {kind} that the page now covers or the code no longer has; remove them: "
                f"{stale}"
            )

    def test_every_state_the_code_writes_is_on_the_page(self):
        self.check("states", states(), lambda value: value in self.page)

    def test_wait_outcome_and_terminal_phases_are_documented(self):
        from podvigil.outcomes import EXIT_CODES
        from podvigil.status import TERMINAL_PHASES

        self.assertIn(f"`wait_timeout` ({EXIT_CODES['wait_timeout']})", self.page)
        for phase in TERMINAL_PHASES:
            self.assertIn("`" + phase + "`", self.page)
        self.assertIn("Waiting interrupted; the job is unaffected.", self.page)
        self.assertIn("watchdog is no longer heard", self.page)

    def test_capacity_rounds_and_refusals_are_documented(self):
        for text in (
            "--wait-for-capacity",
            "capacity_rounds",
            "attempt_ledgers",
            "Capacity retry refused",
            "Capacity waiting allowance expired.",
            "last attempt's ledger does not verify absence",
        ):
            self.assertIn(text, self.flat)

    def test_every_message_the_code_raises_or_prints_is_on_the_page(self):
        self.check("messages", messages(), lambda literal: literal in self.flat)


if __name__ == "__main__":
    unittest.main()
