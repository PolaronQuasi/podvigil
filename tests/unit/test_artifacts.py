from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import copy
import hashlib
import json
import os
import stat
import threading

from podvigil import storage as artifacts
from podvigil.storage import MANIFEST_FILE, RECEIPT_FILE, Storage, verify_local_backup
from podvigil.podrt.records import exclusive_lock, save
from podvigil.jobspec import read_config, validate
from podvigil.pathsafe import validate_input_source
from tests.support.fakes import FakeS3
from tests.support.paths import ROOT


BUCKET = "testvolume"
JOB_ID = "a" * 32
PREFIX = f"jobs/{JOB_ID}/"
CONFIG = ROOT / "jobs" / "smoke.example.json"


class ArtifactReviewTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "downloads"
        self.job = {"job_id": JOB_ID, "config": {"required_artifacts": ["metrics.json"]}}
        self.s3 = FakeS3(page_size=2)
        self.s3.finished(JOB_ID)
        self.storage = Storage("EU-RO-1", self.s3)
        self.manifest_key = (BUCKET, PREFIX + MANIFEST_FILE)
        self.manifest = json.loads(self.s3.objects[self.manifest_key])

    def collect(self):
        return self.storage.collect(self.job, BUCKET, self.destination)

    def local_verify(self, bucket=BUCKET):
        return verify_local_backup(self.job, bucket, self.destination)

    def publish_manifest(self, manifest):
        self.s3.objects[self.manifest_key] = json.dumps(manifest).encode()

    def entry(self, name, content):
        return {
            "path": "artifacts/" + name,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def test_paginated_inventory_and_bound_receipt(self):
        self.s3.objects[BUCKET, PREFIX + "artifacts/empty/"] = b""
        self.s3.objects[BUCKET, PREFIX + "agent.json"] = b"not an artifact"
        self.s3.objects["other", PREFIX + "artifacts/unrelated.bin"] = b"another volume"
        self.assertEqual(self.collect(), self.manifest)
        self.assertEqual(self.local_verify(), self.manifest)
        self.assertEqual([call[2] for call in self.s3.list_calls], [None, "page:2", "page:4"])
        receipt = json.loads((self.destination / RECEIPT_FILE).read_text())
        raw = (self.destination / MANIFEST_FILE).read_bytes()
        self.assertEqual(
            receipt,
            {
                "job_id": JOB_ID,
                "volume_id": BUCKET,
                "file_count": 4,
                "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            },
        )
        staged = [path for _, _, path in self.s3.downloads]
        self.assertEqual(len(staged), len(set(staged)))
        self.assertTrue(all(path.parent.parent == self.destination for path in staged))
        self.assertTrue(all(not path.exists() for path in staged))

    def test_rejects_missing_and_malformed_manifests_before_downloading(self):
        del self.s3.objects[self.manifest_key]
        with self.assertRaisesRegex(RuntimeError, "manifest"):
            self.collect()
        cases = [
            None,
            [],
            {**self.manifest, "complete": 1},
            {**self.manifest, "job_id": "other"},
            {**self.manifest, "extra": True},
            {**self.manifest, "created_utc": "2026-11-10T00:00:00"},
            {**self.manifest, "files": []},
        ]
        for update in (
            {"size": True},
            {"size": -1},
            {"sha256": "bad"},
            {"path": None},
            {"unknown": True},
        ):
            cases.append({**self.manifest, "files": [{**self.manifest["files"][0], **update}]})
        for manifest in cases:
            with self.subTest(manifest=manifest):
                self.publish_manifest(manifest)
                with self.assertRaisesRegex(RuntimeError, "manifest"):
                    self.collect()
        self.s3.objects[self.manifest_key] = b'{"job_id":"old","job_id":"new"}'
        with self.assertRaisesRegex(RuntimeError, "metadata"):
            self.collect()
        self.assertFalse(self.s3.downloads)
        self.assertFalse((self.destination / RECEIPT_FILE).exists())

    def test_rejects_missing_object_even_when_a_local_copy_exists(self):
        self.collect()
        (self.destination / RECEIPT_FILE).unlink()
        del self.s3.objects[BUCKET, PREFIX + "artifacts/model.bin"]
        with self.assertRaisesRegex(RuntimeError, "inventory"):
            self.collect()
        self.assertFalse((self.destination / RECEIPT_FILE).exists())

    def test_rejects_extra_checkpoint_on_a_later_page(self):
        download = self.s3.download_file

        def publish_late(bucket, key, destination):
            download(bucket, key, destination)
            self.s3.objects[BUCKET, PREFIX + "artifacts/zzz-only-checkpoint.bin"] = b"remote only"

        with patch.object(self.s3, "download_file", side_effect=publish_late):
            with self.assertRaisesRegex(RuntimeError, "inventory"):
                self.collect()
        self.assertEqual(self.s3.list_calls[-1][2], "page:4")
        self.assertFalse((self.destination / RECEIPT_FILE).exists())

    def test_rejects_malformed_inventory_and_bad_pagination(self):
        key = PREFIX + "artifacts/train.log"
        item = {"Key": key, "Size": 10}
        page = {"IsTruncated": True, "NextContinuationToken": "again", "Contents": []}
        cases = [
            [{}],
            [{"IsTruncated": 1}],
            [{"IsTruncated": True}],
            [page, page],
            [{"IsTruncated": False, "Contents": [item, item]}],
            [{"IsTruncated": False, "Contents": [{**item, "Size": True}]}],
            [{"IsTruncated": False, "Contents": [{**item, "Key": PREFIX + "agent.json"}]}],
            [{"IsTruncated": False, "Contents": [item], "KeyCount": 0}],
            [{"IsTruncated": False, "Contents": [item], "CommonPrefixes": {}}],
        ]
        for pages in cases:
            with (
                self.subTest(pages=pages),
                patch.object(self.s3, "list_objects_v2", side_effect=pages),
            ):
                with self.assertRaisesRegex(RuntimeError, "inventory"):
                    self.collect()
                self.assertFalse((self.destination / RECEIPT_FILE).exists())

    def test_rejects_windows_aliases_and_file_directory_collisions_before_writing(self):
        names = [
            ("MODEL.BIN",),
            ("model.bin",),
            ("model.bin/child",),
            ("foo.",),
            ("foo ",),
            ("nested/NUL.txt",),
            ("LPT¹.txt",),
            ("LONGFI~1.bin",),
            ("invalid?.bin",),
            ("folder/a", "Folder/b"),
            ("leaf/child", "leaf"),
            ("K.bin", "K.bin"),
        ]
        for paths in names:
            with self.subTest(paths=paths):
                manifest = copy.deepcopy(self.manifest)
                manifest["files"].extend(self.entry(name, b"distinct data") for name in paths)
                self.publish_manifest(manifest)
                with self.assertRaises(ValueError):
                    self.collect()
        self.assertFalse(self.s3.downloads)
        self.assertFalse((self.destination / "artifacts").exists())

    def test_distinct_artifact_with_partial_suffix_survives(self):
        content = b"separate checkpoint"
        entry = self.entry("model.bin.partial", content)
        self.manifest["files"].insert(0, entry)
        self.s3.objects[BUCKET, PREFIX + entry["path"]] = content
        self.publish_manifest(self.manifest)
        self.collect()
        self.assertEqual((self.destination / entry["path"]).read_bytes(), content)
        self.assertEqual(
            (self.destination / "artifacts/model.bin").read_bytes(), b"example weights"
        )
        self.local_verify()

    def test_existing_hardlink_is_rejected_before_downloads(self):
        outside = self.root / "outside.bin"
        outside.write_bytes(b"untouched")
        target = self.destination / "artifacts/model.bin"
        target.parent.mkdir(parents=True)
        os.link(outside, target)
        with self.assertRaisesRegex(RuntimeError, "Unsafe existing path"):
            self.collect()
        self.assertFalse(self.s3.downloads)
        self.assertEqual(outside.read_bytes(), b"untouched")

    def test_success_requires_metrics_failed_outcome_does_not(self):
        del self.s3.objects[BUCKET, PREFIX + "artifacts/metrics.json"]
        self.manifest["files"] = [
            entry for entry in self.manifest["files"] if entry["path"] != "artifacts/metrics.json"
        ]
        self.publish_manifest(self.manifest)
        with self.assertRaisesRegex(RuntimeError, "missing required artifacts"):
            self.collect()
        self.assertFalse((self.destination / RECEIPT_FILE).exists())
        self.s3.finished(JOB_ID, outcome="training_failed")
        self.collect()
        self.local_verify()

    def test_rejects_missing_or_wrong_job_result(self):
        content = b'{"job_id":"other","outcome":"success"}'
        result = self.entry("result.json", content)
        self.manifest["files"] = [
            result if entry["path"] == result["path"] else entry for entry in self.manifest["files"]
        ]
        self.s3.objects[BUCKET, PREFIX + result["path"]] = content
        self.publish_manifest(self.manifest)
        with self.assertRaisesRegex(RuntimeError, "Invalid job result"):
            self.collect()
        self.manifest["files"].remove(result)
        self.publish_manifest(self.manifest)
        with self.assertRaisesRegex(RuntimeError, "Missing job result"):
            self.collect()

    def test_local_reverification_rejects_wrong_volume_and_modified_data(self):
        self.collect()
        with self.assertRaisesRegex(RuntimeError, "receipt"):
            self.local_verify("other")
        manifest = self.destination / MANIFEST_FILE
        original = manifest.read_bytes()
        manifest.write_bytes(original + b"\n")
        with self.assertRaisesRegex(RuntimeError, "receipt"):
            self.local_verify()
        manifest.write_bytes(original)
        checkpoint = self.destination / "artifacts/model.bin"
        checkpoint.write_bytes(b"X" * len(b"example weights"))
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            self.local_verify()
        checkpoint.unlink()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            self.local_verify()

    def test_failed_durable_publication_creates_no_receipt(self):
        with patch.object(artifacts, "durable_replace", side_effect=OSError("dummy flush error")):
            with self.assertRaisesRegex(OSError, "dummy flush error"):
                self.collect()
        self.assertFalse((self.destination / RECEIPT_FILE).exists())

    def test_concurrent_direct_collects_share_a_lock_and_reuse_verified_bytes(self):
        downloading, release = threading.Event(), threading.Event()
        download = self.s3.download_file

        def slow_download(bucket, key, destination):
            with self.assertRaises(TimeoutError):
                with exclusive_lock(self.destination / "collect.lock", timeout=0):
                    self.fail("download was not protected by the collection lock")
            downloading.set()
            if not release.wait(5):
                raise TimeoutError("test failed to release the downloader")
            download(bucket, key, destination)

        with patch.object(self.s3, "download_file", side_effect=slow_download):
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(self.collect)
                try:
                    self.assertTrue(downloading.wait(5))
                    second = pool.submit(self.collect)
                finally:
                    release.set()
                self.assertEqual(first.result(10), self.manifest)
                self.assertEqual(second.result(10), self.manifest)
        self.assertEqual(len(self.s3.downloads), len(self.manifest["files"]))
        self.local_verify()

    def test_partial_export_is_separate_and_produces_no_verification_receipt(self):
        del self.s3.objects[self.manifest_key]
        before = dict(self.s3.objects)
        exported = self.storage.export_partial(self.job, BUCKET, self.root / "partial-downloads")
        self.assertFalse(exported["complete"])
        self.assertEqual(exported["file_count"], len(before))
        root = Path(exported["download_directory"])
        records = [
            json.loads(line) for line in Path(exported["mapping_file"]).read_text().splitlines()
        ]
        copied = next(
            item
            for item in records
            if item.get("status") == "saved" and item["key"].endswith("artifacts/model.bin")
        )
        self.assertEqual((root / copied["local_path"]).read_bytes(), b"example weights")
        self.assertFalse((root / RECEIPT_FILE).exists())
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.s3.objects, before)


class InputPolicyTests(TestCase):
    def config(self):
        cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
        cfg.pop("required_artifacts", None)
        cfg.pop("preparation_timeout_seconds", None)
        return cfg

    def test_preparation_timeout_and_required_artifacts(self):
        self.assertEqual(validate(self.config())["preparation_timeout_seconds"], 1800)
        self.assertEqual(validate(self.config())["required_artifacts"], [])
        for invalid in (True, 59, 86401, 60.0):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate({**self.config(), "preparation_timeout_seconds": invalid})
        for invalid in (
            "metrics.json",
            ["../metrics.json"],
            ["result.JSON"],
            ["checkpoints", "checkpoints/model.bin"],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate({**self.config(), "required_artifacts": invalid})
        cfg = validate(
            {
                **self.config(),
                "required_artifacts": ["metrics.json", "nested/model.bin"],
                "preparation_timeout_seconds": 60,
            }
        )
        self.assertEqual(cfg["required_artifacts"], ["metrics.json", "nested/model.bin"])

    def test_casefolds_credential_source_and_remote_name(self):
        for local, remote in ((".ENV", "input.txt"), ("data.txt", "dir/.Env.backup")):
            with (
                self.subTest(local=local, remote=remote),
                self.assertRaisesRegex(ValueError, "env|credential"),
            ):
                validate({**self.config(), "inputs": [{"local": local, "remote": remote}]})

    def test_same_file_check_detects_hardlink_at_upload_time(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            dummy = root / "dummy-secret"
            source = root / "input.txt"
            dummy.write_bytes(b"dummy only")
            source.write_bytes(b"original input")
            config_path = root / "job.json"
            save(
                config_path,
                {**self.config(), "inputs": [{"local": source.name, "remote": "input.txt"}]},
            )
            with patch("podvigil.pathsafe.credential_files", return_value=[dummy]):
                configured = read_config(config_path)["inputs"][0]["local"]
                self.assertEqual(Path(configured), source)
                source.unlink()
                os.link(dummy, source)
                with self.assertRaisesRegex(ValueError, "alias"):
                    validate_input_source(configured)
                s3 = FakeS3()
                with self.assertRaisesRegex(ValueError, "alias"):
                    Storage("EU-RO-1", s3).upload(BUCKET, "inputs/input.txt", configured)
                self.assertFalse(s3.objects)

    def test_rejects_symlink_before_resolution(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "input.txt"
            original_lstat = Path.lstat
            link_info = os.stat_result((stat.S_IFLNK, 0, 0, 1, 0, 0, 0, 0, 0, 0))

            def lstat(path):
                return link_info if path == source else original_lstat(path)

            with patch.object(Path, "lstat", lstat), patch.object(Path, "resolve") as resolve:
                with self.assertRaisesRegex(ValueError, "symlink"):
                    validate_input_source(source)
                resolve.assert_not_called()
