"""A job file may name a folder or a pattern; plan expands it into the explicit, hashed file list.

The first user's wrapper (`make_job.py`) expanded 30 patterns by hand. Expanded files pass the same
safety checks as explicit ones; a link, a credential file or an empty match fails the whole job file.
"""

from pathlib import Path
import hashlib
import json
import os
import subprocess
import tempfile
import unittest

from podvigil.storage import Storage
from tests.support import runner as run
from tests.support.runner import patch_run
from podvigil.jobspec import read_config, read_job
from tests.support.fakes import FakeApi, FakeS3
from tests.support.paths import ROOT


def write(path, content=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def can_symlink(folder):
    try:
        os.symlink(folder / "missing-target", folder / "probe-link")
    except (OSError, NotImplementedError):
        return False
    (folder / "probe-link").unlink()
    return True


class InputExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wondomon-input-expansion-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        write(self.project / "data" / "a.bin", b"alpha")
        write(self.project / "data" / "deep" / "b.bin", b"beta")
        write(self.project / "configs" / "one.json", b"{}")
        write(self.project / "configs" / "two.json", b"[]")
        write(self.project / "configs" / "notes.txt", b"not matched")
        # A folder the pattern matches is skipped.
        (self.project / "configs" / "folder.json").mkdir()
        self.jobs = self.project / "cloud" / "jobs"
        self.jobs.mkdir(parents=True)

    def job(self, inputs, name="job.json"):
        config = json.loads((ROOT / "jobs" / "smoke.example.json").read_text())
        config["inputs"] = inputs
        path = self.jobs / name
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_directory_and_pattern_expand_to_explicit_sorted_files(self):
        cfg = read_config(
            self.job(
                [
                    {"directory": "../../data", "remote": "data"},
                    {"pattern": "configs/*.json", "base": "../..", "remote": "project"},
                    {"pattern": "*.txt", "base": "../../configs"},
                    {"local": "../../data/a.bin", "remote": "explicit/a.bin"},
                ]
            )
        )
        self.assertEqual(
            [item["remote"] for item in cfg["inputs"]],
            [
                "data/a.bin",
                "data/deep/b.bin",
                "project/configs/one.json",
                "project/configs/two.json",
                "notes.txt",
                "explicit/a.bin",
            ],
        )
        for item in cfg["inputs"]:
            self.assertEqual(set(item), {"local", "remote"})
            self.assertTrue(Path(item["local"]).is_absolute())

    def test_plan_shows_the_hashed_list(self):
        result = run.plan(read_config(self.job([{"directory": "../../data", "remote": "data"}])))
        listed = {item["remote"]: item for item in result["inputs"]["list"]}
        self.assertEqual(result["inputs"]["files"], 2)
        self.assertEqual(result["inputs"]["bytes"], len(b"alpha") + len(b"beta"))
        self.assertEqual(listed["data/deep/b.bin"]["sha256"], hashlib.sha256(b"beta").hexdigest())
        self.assertEqual(result["pod_request"]["input_files"], 2)

    def test_launch_uploads_exactly_the_expanded_files(self):
        configs = read_job(self.job([{"directory": "../../data", "remote": "data"}]))
        api, s3 = FakeApi(), FakeS3()
        api.pod_rejection = (
            400,
            "There are no longer any instances available with the requested specifications.",
        )
        jobs = self.root / "ledgers"

        class Alive:
            def poll(self):
                return None

        uploaded = []
        original = s3.upload_file

        def upload(source, bucket, key):
            uploaded.append(key)
            original(source, bucket, key)

        s3.upload_file = upload
        with (
            patch_run("Api", return_value=api),
            patch_run("Storage", return_value=Storage("EU-RO-1", s3)),
            patch_run("load_key", return_value="dummy"),
            patch_run("spawn_watchdog", side_effect=lambda folder, **_: Alive()),
            patch_run("jobs_dir", return_value=jobs),
        ):
            with self.assertRaises(Exception):
                run.launch_candidates(configs)
        self.assertEqual(sorted(uploaded), ["inputs/data/a.bin", "inputs/data/deep/b.bin"])
        job = json.loads(next(jobs.iterdir()).joinpath("job.json").read_text())
        self.assertEqual(
            {item["remote"]: item["sha256"] for item in job["inputs"]},
            {
                "data/a.bin": hashlib.sha256(b"alpha").hexdigest(),
                "data/deep/b.bin": hashlib.sha256(b"beta").hexdigest(),
            },
        )

    def test_unsafe_or_empty_entries_reject_the_job_file(self):
        write(self.project / "secrets" / ".env", b"RUNPOD_API_KEY=dummy")
        (self.project / "empty").mkdir()
        cases = {
            "credential file": [{"directory": "../../secrets", "remote": "s"}],
            "holds no files": [{"directory": "../../empty", "remote": "e"}],
            "does not exist": [{"directory": "../../missing", "remote": "m"}],
            "not a plain folder": [{"directory": "../../data/a.bin", "remote": "f"}],
            "matched no files": [{"pattern": "configs/*.yaml", "base": "../.."}],
            "without '.', '..' or '**'": [{"pattern": "../../configs/*.json"}],
            "use a directory input": [{"pattern": "**/*.json", "base": "../.."}],
            "must be relative": [{"pattern": "/etc/*"}],
            'exactly "directory" and "remote"': [{"directory": "../../data"}],
            'optionally "remote" and "base"': [{"pattern": "*.json", "local": "x"}],
            "Duplicate input": [
                {"directory": "../../data", "remote": "data"},
                {"local": "../../data/a.bin", "remote": "data/a.bin"},
            ],
            "Unsafe artifact path": [{"directory": "../../data", "remote": "../escape"}],
        }
        for expected, inputs in cases.items():
            with self.subTest(expected=expected):
                with self.assertRaises(ValueError) as caught:
                    read_config(self.job(inputs))
                self.assertIn(expected, str(caught.exception))

    def test_a_pattern_can_match_the_credential_file_and_is_refused(self):
        write(self.project / "configs" / ".env.local", b"RUNPOD_S3_SECRET_KEY=dummy")
        with self.assertRaisesRegex(ValueError, "credential file"):
            read_config(self.job([{"pattern": "configs/*", "base": "../.."}]))

    def test_remote_file_directory_conflicts_are_refused_in_either_order(self):
        for names in (("data", "data/file.txt"), ("data/file.txt", "data")):
            with (
                self.subTest(names=names),
                self.assertRaisesRegex(ValueError, "Input paths collide"),
            ):
                read_config(
                    self.job([{"local": "../../data/a.bin", "remote": name} for name in names])
                )

    def test_nul_in_an_input_destination_is_refused_before_planning(self):
        with self.assertRaisesRegex(ValueError, "Unsafe artifact path"):
            read_config(self.job([{"local": "../../data/a.bin", "remote": "data\0.bin"}]))

    def test_cloud_inputs_keep_distinct_linux_case_sensitive_names(self):
        config = read_config(
            self.job(
                [{"local": "../../data/a.bin", "remote": name} for name in ("Data.txt", "data.txt")]
            )
        )
        self.assertEqual([item["remote"] for item in config["inputs"]], ["Data.txt", "data.txt"])

    def test_links_inside_a_folder_are_rejected_not_followed(self):
        if not can_symlink(self.root):
            self.skipTest("This account cannot create symbolic links")
        outside = write(self.root / "outside" / "private.key", b"private")
        os.symlink(outside, self.project / "data" / "link.key")
        with self.assertRaisesRegex(ValueError, "link or reparse point"):
            read_config(self.job([{"directory": "../../data", "remote": "data"}]))
        (self.project / "data" / "link.key").unlink()
        os.symlink(
            outside.parent, self.project / "data" / "linked-folder", target_is_directory=True
        )
        with self.assertRaisesRegex(ValueError, "link or reparse point"):
            read_config(self.job([{"directory": "../../data", "remote": "data"}]))
        with self.assertRaisesRegex(ValueError, "symlink or reparse point|link or reparse point"):
            read_config(self.job([{"pattern": "data/linked-folder/*", "base": "../.."}]))

    @unittest.skipUnless(os.name == "nt", "Windows directory junction")
    def test_a_windows_junction_is_rejected_not_followed(self):
        outside = write(self.root / "outside" / "private.key", b"private").parent
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(self.project / "data" / "junction"), str(outside)],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        with self.assertRaisesRegex(ValueError, "link or reparse point"):
            read_config(self.job([{"directory": "../../data", "remote": "data"}]))
        with self.assertRaisesRegex(ValueError, "symlink or reparse point|link or reparse point"):
            read_config(self.job([{"pattern": "data/junction/*", "base": "../.."}]))

    def test_a_large_folder_no_longer_meets_a_request_limit(self):
        # Before the input manifest (item 7), 700 files overflowed the Pod request.
        # Now the list is on the volume.
        for index in range(700):
            write(self.project / "many" / f"file-{index:04d}-with-a-longer-name.py")
        result = run.plan(read_config(self.job([{"directory": "../../many", "remote": "many"}])))
        self.assertEqual(
            (result["inputs"]["files"], result["pod_request"]["input_files"]), (700, 700)
        )
        empty = run.plan(read_config(self.job([])))["pod_request"]["bytes"]
        self.assertEqual(result["pod_request"]["bytes"], empty)


if __name__ == "__main__":
    unittest.main()
