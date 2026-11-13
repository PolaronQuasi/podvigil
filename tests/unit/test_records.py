"""Record reads recover from brief Windows sharing violations and otherwise fail closed."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import unittest

from podvigil.podrt import records


class RecordReadTests(unittest.TestCase):
    def test_windows_sharing_violation_recovers_the_complete_record(self):
        with (
            patch.object(records, "os", SimpleNamespace(name="nt")),
            patch.object(
                Path, "read_text", side_effect=[PermissionError(13, "busy"), '{"epoch": 42}']
            ) as read_text,
            patch.object(records.time, "sleep") as sleep,
        ):
            self.assertEqual(records.read("heartbeat.json"), {"epoch": 42})
        self.assertEqual(read_text.call_count, 2)
        self.assertGreater(sum(call.args[0] for call in sleep.call_args_list), 0)

    def test_persistent_windows_denial_is_not_a_missing_record(self):
        error = PermissionError(13, "denied")
        with (
            patch.object(records, "os", SimpleNamespace(name="nt")),
            patch.object(Path, "read_text", side_effect=error),
            patch.object(records.time, "sleep") as sleep,
            self.assertRaises(PermissionError) as caught,
        ):
            records.read("job.json", {"unsafe": "fallback"})
        self.assertIs(caught.exception, error)
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 1.0)

    def test_other_read_errors_are_not_retried(self):
        for error in (OSError(5, "I/O error"), IsADirectoryError(21, "directory")):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(records, "os", SimpleNamespace(name="nt")),
                patch.object(Path, "read_text", side_effect=error) as read_text,
                patch.object(records.time, "sleep") as sleep,
                self.assertRaises(type(error)) as caught,
            ):
                records.read("job.json")
            self.assertIs(caught.exception, error)
            read_text.assert_called_once()
            sleep.assert_not_called()

    def test_non_windows_denial_is_not_retried(self):
        with (
            patch.object(records, "os", SimpleNamespace(name="posix")),
            patch.object(Path, "read_text", side_effect=PermissionError(13, "denied")),
            patch.object(records.time, "sleep") as sleep,
            self.assertRaises(PermissionError),
        ):
            records.read("job.json")
        sleep.assert_not_called()

    def test_missing_record_returns_the_callers_default(self):
        missing = object()
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            self.assertIs(records.read("missing.json", missing), missing)

    def test_invalid_json_is_not_retried_or_treated_as_missing(self):
        with (
            patch.object(records, "os", SimpleNamespace(name="nt")),
            patch.object(Path, "read_text", return_value="{"),
            patch.object(records.time, "sleep") as sleep,
            self.assertRaises(json.JSONDecodeError),
        ):
            records.read("job.json", {})
        sleep.assert_not_called()
