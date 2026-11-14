"""Output bundle and cutoff regressions. The Linux run of the POSIX cases is tests/container/test_output_bundle_linux.py."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import copy
import errno
import hashlib
import io
import json
import os
import stat
import tarfile
import time
import unittest

from podvigil.podrt import bundle
from podvigil.storage import Storage, verify_local_backup
from tests.support.fakes import FakeS3

JOB = {"job_id": "c" * 32, "inputs": [], "config": {"required_artifacts": ["metrics.json"]}}
PREFIX = "jobs/" + JOB["job_id"] + "/"
BUCKET = "testvolume"


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def attributes(mode=0o600):
    return {"mode": mode, "uid": 61000, "gid": 61000, "mtime_ns": 1000000000}


def archive_fixture():
    """Create a POSIX-name bundle without asking Windows to create those names."""
    snapshot = "a" * 32
    bodies = {
        "result.json": json.dumps({"job_id": JOB["job_id"], "outcome": "success"}).encode(),
        "train.log": b"dummy training complete",
        "metrics.json": b'{"loss":0.1}',
        "run/epoch:20": b"only checkpoint copy",
    }
    entries = [{"path": "run", "kind": "directory", **attributes(0o750)}]
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for index, (name, data) in enumerate(bodies.items()):
            member = f"objects/{index:08d}"
            header = tarfile.TarInfo(member)
            header.size = len(data)
            tar.addfile(header, io.BytesIO(data))
            entries.append(
                {
                    "path": name,
                    "kind": "file",
                    **attributes(),
                    "member": member,
                    "size": len(data),
                    "sha256": checksum(data),
                }
            )
    original = entries[-1]
    entries.append(
        {key: value for key, value in original.items() if key not in ("kind", "member", "path")}
        | {"path": "copy", "kind": "hardlink", "target": original["path"]}
    )
    entries.append(
        {"path": "external", "kind": "symlink", **attributes(0o777), "target": "/dummy/credential"}
    )
    entries.append(
        {"path": "link", "kind": "symlink", **attributes(0o777), "target": "run/epoch:20"}
    )
    metadata = {
        "version": 1,
        "job_id": JOB["job_id"],
        "snapshot_id": snapshot,
        "root": attributes(0o700),
        "entries": entries,
    }
    paths = {
        "archive": f"output-bundle/{snapshot}/archive.tar",
        "metadata": f"output-bundle/{snapshot}/metadata.json",
    }
    files = {
        paths["archive"]: raw.getvalue(),
        paths["metadata"]: json.dumps(metadata).encode(),
        "artifacts/result.json": bodies["result.json"],
        "artifacts/train.log": bodies["train.log"],
    }
    manifest = {
        "version": 2,
        "job_id": JOB["job_id"],
        "complete": True,
        "bundle": paths,
        "files": [
            {"path": name, "size": len(data), "sha256": checksum(data)}
            for name, data in files.items()
        ],
    }
    originals = {"artifacts/" + name: data for name, data in bodies.items()}
    originals["artifacts/copy"] = bodies["run/epoch:20"]
    return manifest, metadata, files | originals


class BundleConsumerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "backup"
        self.manifest, self.metadata, self.objects = archive_fixture()
        self.s3 = FakeS3(page_size=2)
        self.storage = Storage("EU-RO-1", self.s3)
        self.sync()

    def sync(self):
        self.objects[self.manifest["bundle"]["metadata"]] = json.dumps(self.metadata).encode()
        for item in self.manifest["files"]:
            data = self.objects[item["path"]]
            item.update(size=len(data), sha256=checksum(data))
        self.objects[bundle.MANIFEST] = json.dumps(self.manifest).encode()
        self.s3.objects = {(BUCKET, PREFIX + name): data for name, data in self.objects.items()}

    def collect(self):
        return self.storage.collect(JOB, BUCKET, self.destination)

    def test_bundle_collect_audit_and_revalidate_without_remote_source(self):
        self.collect()
        before = dict(self.s3.objects)
        audit = self.storage.audit_for_delete(JOB, BUCKET, self.destination)
        self.assertTrue(audit["bundle_verified"])
        self.assertEqual(audit["object_count"], len(before))
        self.assertTrue(any(item["kind"] == "archived_original" for item in audit["objects"]))
        self.assertEqual(self.s3.objects, before)
        self.assertTrue(any(prefix == "" and token for _, prefix, token in self.s3.list_calls))
        self.s3.objects.clear()
        self.assertEqual(verify_local_backup(JOB, BUCKET, self.destination), self.manifest)

    def test_default_extraction_is_opaque_and_never_creates_stored_links(self):
        self.collect()
        result = bundle.extract_bundle(
            self.destination / self.manifest["bundle"]["archive"],
            self.destination / self.manifest["bundle"]["metadata"],
            self.root / "extracted",
        )
        extracted = Path(result["directory"])
        self.assertFalse(result["restored_tree"])
        self.assertEqual(json.loads((extracted / "mapping.json").read_text()), self.metadata)
        self.assertEqual((extracted / "objects" / "00000003").read_bytes(), b"only checkpoint copy")
        self.assertFalse((extracted / "external").exists())

    def test_link_targets_check_components_before_normalizing_parent_steps(self):
        entries = bundle.validate_metadata(self.metadata)
        entries.update({name: {"kind": "directory"} for name in ("deep", "deep/nest", "shallow")})
        entries["deep/nest/alias"] = {"kind": "symlink", "target": "../../shallow"}
        entries["outside.txt"] = {"kind": "file"}
        for target in (
            "deep/nest/alias/../../../outside.txt",
            "deep/nest/alias/../outside.txt",
            "link",
            "link/../outside.txt",
            "outside.txt/../outside.txt",
            "outside.txt/.",
            "outside.txt/",
            "missing/../outside.txt",
            "../outside.txt",
            "/outside.txt",
        ):
            with self.subTest(target=target):
                self.assertFalse(bundle._contained_link("escape", target, entries))
        for name, target in (
            ("safe", "deep/nest/../../outside.txt"),
            ("safe", "run/epoch:20"),
            ("safe", "copy"),
            ("safe", "run/"),
            ("deep/nest/safe", "../../outside.txt"),
        ):
            with self.subTest(name=name, target=target):
                self.assertTrue(bundle._contained_link(name, target, entries))

    def test_metadata_paths_traversal_missing_parents_link_escape_and_duplicates(self):
        for change in (
            "traversal",
            "absolute",
            "missingparent",
            "linkparent",
            "duplicate",
            "memberalias",
        ):
            with self.subTest(change=change):
                original = copy.deepcopy(self.metadata)
                if change == "traversal":
                    self.metadata["entries"][1]["path"] = "../escape"
                elif change == "absolute":
                    self.metadata["entries"][1]["path"] = "/escape"
                elif change == "missingparent":
                    self.metadata["entries"][1]["path"] = "missing/a"
                elif change == "linkparent":
                    self.metadata["entries"][1]["path"] = "external/a"
                elif change == "duplicate":
                    self.metadata["entries"].append(self.metadata["entries"][1])
                else:
                    self.metadata["entries"][2]["member"] = self.metadata["entries"][1]["member"]
                self.sync()
                with self.assertRaises((ValueError, RuntimeError)):
                    self.collect()
                self.assertFalse((self.destination / "backup-verified.json").exists())
                self.metadata = original

    def test_missing_extra_and_changed_archive_members_rejected(self):
        archive_path = self.manifest["bundle"]["archive"]
        original = self.objects[archive_path]
        for case in ("missing", "extra", "changed", "symlink", "duplicate"):
            with self.subTest(case=case):
                raw = io.BytesIO()
                with tarfile.open(fileobj=raw, mode="w") as tar:
                    if case != "missing":
                        with tarfile.open(fileobj=io.BytesIO(original)) as source:
                            for member in source:
                                data = source.extractfile(member).read()
                                if case == "changed":
                                    data = b"x" * len(data)
                                tar.addfile(member, io.BytesIO(data))
                    if case in ("extra", "symlink", "duplicate"):
                        header = tarfile.TarInfo(
                            "objects/00000000" if case == "duplicate" else "../../outside"
                        )
                        if case == "symlink":
                            header.type, header.linkname = tarfile.SYMTYPE, "/dummy/credential"
                        tar.addfile(header)
                self.objects[archive_path] = raw.getvalue()
                self.sync()
                with self.assertRaises((ValueError, RuntimeError)):
                    self.collect()

    def test_same_size_original_mutation_after_snapshot_retains_source(self):
        self.collect()
        key = (BUCKET, PREFIX + "artifacts/run/epoch:20")
        self.s3.objects[key] = b"X" * len(self.s3.objects[key])
        with self.assertRaisesRegex(RuntimeError, "original checksum"):
            self.storage.audit_for_delete(JOB, BUCKET, self.destination)
        self.assertIn(key, self.s3.objects)

    def test_unknown_or_missing_original_and_other_snapshot_retains_source(self):
        self.collect()
        for name in (
            "artifacts/late",
            "outside/model",
            "output-bundle/" + "b" * 32 + "/archive.tar",
        ):
            self.s3.objects[BUCKET, PREFIX + name] = b"late"
            with self.assertRaisesRegex(RuntimeError, "Unexpected volume objects"):
                self.storage.audit_for_delete(JOB, BUCKET, self.destination)
            del self.s3.objects[BUCKET, PREFIX + name]
        del self.s3.objects[BUCKET, PREFIX + "artifacts/copy"]
        with self.assertRaisesRegex(RuntimeError, "missing manifest objects"):
            self.storage.audit_for_delete(JOB, BUCKET, self.destination)

    def test_visible_symlink_objects_never_read_by_collect_audit_or_partial_export(self):
        self.collect()
        symlink = PREFIX + "artifacts/external"
        self.s3.objects[BUCKET, symlink] = b"never read this dummy target"
        self.s3.objects[BUCKET, PREFIX + "control/output-links.json"] = json.dumps(
            {
                "version": 1,
                "job_id": JOB["job_id"],
                "links": ["artifacts/external", "artifacts/link"],
            }
        ).encode()
        original = self.s3.get_object

        def guarded(Bucket, Key):
            self.assertNotEqual(Key, symlink, "A link target was read!")
            return original(Bucket, Key)

        with patch.object(self.s3, "get_object", side_effect=guarded):
            with self.assertRaisesRegex(RuntimeError, "symlink object"):
                self.collect()
            with self.assertRaisesRegex(RuntimeError, "symlink object"):
                self.storage.audit_for_delete(JOB, BUCKET, self.destination)
            result = self.storage.export_partial(JOB, BUCKET, self.root / "partial")
        self.assertFalse(result["complete"])
        self.assertEqual(result["failed_count"], 1)
        self.assertIn("skipped_symlink", Path(result["mapping_file"]).read_text())

    def test_cutoff_partial_archive_and_raw_objects_export_without_receipt(self):
        del self.s3.objects[BUCKET, PREFIX + bundle.MANIFEST]
        archive = self.manifest["bundle"]["archive"]
        self.s3.objects[BUCKET, PREFIX + archive] = b"incomplete tar"
        with self.assertRaises(RuntimeError):
            self.collect()
        result = self.storage.export_partial(JOB, BUCKET, self.root / "partial")
        self.assertEqual(result["file_count"], len(self.s3.objects))
        self.assertFalse(result["complete"])
        self.assertFalse(result["volume_deleted"])
        self.assertFalse((self.destination / "backup-verified.json").exists())

    def test_local_archive_corruption_and_wrong_snapshot_or_job_rejected(self):
        self.collect()
        path = self.destination / self.manifest["bundle"]["archive"]
        path.write_bytes(b"X" * path.stat().st_size)
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            verify_local_backup(JOB, BUCKET, self.destination)
        self.metadata["job_id"] = "wrong"
        self.sync()
        with self.assertRaises(ValueError):
            self.collect()
        self.metadata["job_id"] = JOB["job_id"]
        self.metadata["snapshot_id"] = "b" * 32
        self.sync()
        with self.assertRaisesRegex(ValueError, "snapshot identity"):
            self.collect()

    def test_portable_manifest_still_collects_and_audits(self):
        self.s3.objects.clear()
        self.s3.finished(JOB["job_id"])
        self.storage.collect(JOB, BUCKET, self.destination)
        self.assertNotIn("version", verify_local_backup(JOB, BUCKET, self.destination))
        self.assertFalse(
            self.storage.audit_for_delete(JOB, BUCKET, self.destination)["bundle_verified"]
        )

    def test_duplicate_or_malformed_manifest_and_uncovered_required_artifact_rejected(self):
        for case in ("version", "extra", "duplicate", "required"):
            with self.subTest(case=case):
                saved = copy.deepcopy(self.manifest)
                if case == "version":
                    self.manifest["version"] = True
                elif case == "extra":
                    self.manifest["bundle"]["unexpected"] = "anything"
                elif case == "duplicate":
                    self.manifest["files"].append(self.manifest["files"][0].copy())
                self.sync()
                job = (
                    {**JOB, "config": {"required_artifacts": ["missing.bin"]}}
                    if case == "required"
                    else JOB
                )
                with self.assertRaises((ValueError, RuntimeError)):
                    self.storage.collect(job, BUCKET, self.destination)
                self.manifest = saved


@unittest.skipUnless(os.name == "posix", "Requires actual POSIX filesystem semantics")
class BundlePosixTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "job"
        self.folder.mkdir(mode=0o700)
        self.output = self.folder / "artifacts"
        self.output.mkdir(mode=0o700)
        (self.output / "result.json").write_text(
            json.dumps({"job_id": JOB["job_id"], "outcome": "success"})
        )
        (self.output / "train.log").write_bytes(b"dummy log")
        (self.output / "metrics.json").write_text("{}")

    def roundtrip_names(self, paths):
        external = self.root / "dummy-credential"
        external.write_bytes(b"must never be opened")
        (self.output / "external").symlink_to(external)
        folder = self.output / "dir:odd"
        folder.mkdir(mode=0o750)
        for name in paths:
            (folder / name).write_bytes(os.fsencode(name))
        os.link(folder / "Case", self.output / "hardcopy")
        (self.output / "internal").symlink_to("dir:odd/Case")
        manifest = bundle.finalize(JOB, self.folder, quiesced=True)
        self.assertEqual(manifest["version"], 2)
        archive, metadata = [
            self.folder / manifest["bundle"][key] for key in ("archive", "metadata")
        ]
        records = bundle.verify_bundle(archive, metadata)
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o400)
        entries = {item["path"]: item for item in records["entries"]}
        self.assertEqual(entries["external"]["target"], str(external))
        self.assertNotIn(external.read_bytes(), archive.read_bytes())
        result = bundle.extract_bundle(
            archive, metadata, self.root / "restored", restore_links=True
        )
        tree = Path(result["directory"]) / "tree"
        self.assertFalse((tree / "external").exists())
        self.assertEqual(os.readlink(tree / "internal"), "dir:odd/Case")
        self.assertEqual((tree / "hardcopy").stat().st_ino, (tree / "dir:odd/Case").stat().st_ino)
        self.assertEqual(stat.S_IMODE((tree / "dir:odd").stat().st_mode), 0o750)
        for name in paths:
            self.assertEqual((tree / "dir:odd" / name).read_bytes(), os.fsencode(name))
        self.assertEqual(external.read_bytes(), b"must never be opened")

    def test_actual_odd_names_symlinks_hardlinks_and_directory_modes_roundtrip(self):
        self.roundtrip_names(["Case", "trailing.", "NUL.txt", "a\\b", "new\nline"])

    def test_case_collisions_roundtrip_on_a_case_sensitive_filesystem(self):
        (self.root / "case-probe").write_bytes(b"probe")
        if (self.root / "CASE-PROBE").exists():
            self.skipTest("Requires a case-sensitive filesystem")
        self.roundtrip_names(["Case", "case"])

    def test_non_utf8_names_roundtrip_where_the_filesystem_accepts_them(self):
        probe = self.root / "bad-\udcff"
        try:
            probe.write_bytes(b"probe")
        except OSError as exc:
            if exc.errno not in (errno.EILSEQ, errno.EINVAL):
                raise
            self.skipTest("Filesystem rejects non-UTF-8 names")
        except UnicodeEncodeError:
            self.skipTest("Filesystem encoding rejects this name")
        probe.unlink()
        self.roundtrip_names(["Case", "bad-\udcff"])

    def test_external_hardlinks_and_special_files_rejected_before_content_open(self):
        outside = self.root / "dummy-outside"
        outside.write_bytes(b"do not read")
        os.link(outside, self.output / "external-hardlink")
        with patch.object(bundle, "_open_file", side_effect=AssertionError("read forbidden")):
            with self.assertRaisesRegex(ValueError, "Hardlink leaves"):
                bundle.finalize(JOB, self.folder, quiesced=True)
        (self.output / "external-hardlink").unlink()
        os.mkfifo(self.output / "pipe")
        with patch.object(bundle, "_open_file", side_effect=AssertionError("read forbidden")):
            with self.assertRaisesRegex(ValueError, "Special output"):
                bundle.finalize(JOB, self.folder, quiesced=True)
        self.assertFalse((self.folder / bundle.MANIFEST).exists())

    def test_restored_link_cannot_escape_through_an_intermediate_symlink(self):
        outside = self.root / "outside.txt"
        outside.write_bytes(b"dummy outside extraction directory")
        (self.output / "deep/nest").mkdir(parents=True)
        (self.output / "shallow").mkdir()
        (self.output / "outside.txt").write_bytes(b"in-tree decoy")
        (self.output / "deep/nest/alias").symlink_to("../../shallow", target_is_directory=True)
        target = "deep/nest/alias/../../../outside.txt"
        (self.output / "escape").symlink_to(target)
        (self.output / "safe").symlink_to("deep/nest/../../outside.txt")
        manifest = bundle.finalize(JOB, self.folder, quiesced=True)
        archive, metadata = [
            self.folder / manifest["bundle"][key] for key in ("archive", "metadata")
        ]
        destination = self.root / "restored"
        bundle.extract_bundle(archive, metadata, destination, restore_links=True)
        tree = destination / "tree"
        self.assertFalse(os.path.lexists(tree / "escape"))
        self.assertEqual(os.readlink(tree / "deep/nest/alias"), "../../shallow")
        self.assertEqual((tree / "safe").read_bytes(), b"in-tree decoy")
        self.assertEqual(outside.read_bytes(), b"dummy outside extraction directory")
        mapping = json.loads((destination / "mapping.json").read_text())
        self.assertEqual(
            next(item["target"] for item in mapping["entries"] if item["path"] == "escape"), target
        )

    def test_cutoff_removes_old_manifest_preserves_raw_files_and_partial_bundle(self):
        bundle.finalize(JOB, self.folder, quiesced=True)
        (self.output / "odd:name").write_bytes(b"only raw copy" * 10000)
        real = bundle._HashReader.read
        calls = 0

        def interrupt(reader, size=-1):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError("simulated finalization cutoff")
            return real(reader, size)

        with patch.object(bundle._HashReader, "read", interrupt):
            with self.assertRaises(TimeoutError):
                bundle.finalize(JOB, self.folder, quiesced=True)
        self.assertFalse((self.folder / bundle.MANIFEST).exists())
        self.assertEqual((self.output / "odd:name").read_bytes(), b"only raw copy" * 10000)
        self.assertTrue(list((self.folder / "output-bundle").glob("*/archive.tar")))
        with self.assertRaises(TimeoutError):
            bundle.finalize(JOB, self.folder, quiesced=True, deadline=time.monotonic() - 1)

    def test_generated_bundle_is_accepted_by_full_volume_consumer(self):
        (self.output / "empty").mkdir(mode=0o711)
        (self.output / "checkpoint:20").write_bytes(b"actual producer bytes")
        (self.output / "outside").symlink_to("/unreadable/dummy-target")
        os.link(self.output / "checkpoint:20", self.output / "copy")
        manifest = bundle.finalize(JOB, self.folder, quiesced=True)
        fake = FakeS3(page_size=2)
        # Model a bridge that exposes regular files and omits symbolic links.
        # Symlink-representation assumptions remain a live-provider check.
        for directory, _dirs, names in os.walk(self.folder, followlinks=False):
            for name in names:
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                fake.objects[BUCKET, PREFIX + path.relative_to(self.folder).as_posix()] = (
                    path.read_bytes()
                )
        storage = Storage("EU-RO-1", fake)
        destination = self.root / "backup"
        storage.collect(JOB, BUCKET, destination)
        evidence = storage.audit_for_delete(JOB, BUCKET, destination)
        self.assertTrue(evidence["bundle_verified"])
        metadata = bundle.verify_bundle(
            destination / manifest["bundle"]["archive"],
            destination / manifest["bundle"]["metadata"],
        )
        self.assertIn("empty", {item["path"] for item in metadata["entries"]})

    def test_failed_metadata_publication_never_commits_manifest(self):
        (self.output / "checkpoint:20").write_bytes(b"retained")
        real = bundle.save

        def fail_metadata(path, value):
            if Path(path).name == "metadata.json":
                raise OSError("simulated full disk")
            return real(path, value)

        with patch.object(bundle, "save", side_effect=fail_metadata):
            with self.assertRaisesRegex(OSError, "full disk"):
                bundle.finalize(JOB, self.folder, quiesced=True)
        self.assertFalse((self.folder / bundle.MANIFEST).exists())
        self.assertEqual((self.output / "checkpoint:20").read_bytes(), b"retained")

    def test_real_256mib_checkpoint_cache_timing_and_same_size_rewrite(self):
        path = self.output / "checkpoint.bin"
        chunk = b"a" * bundle.BLOCK
        started = time.monotonic()
        with path.open("wb") as stream:
            for _ in range(64):
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        cache = bundle.HashCache(self.output)
        cache.scan_once()
        warm_seconds = time.monotonic() - started
        before = cache.bytes_hashed
        started = time.monotonic()
        manifest = bundle.finalize(
            JOB, self.folder, cache=cache, quiesced=True, deadline=time.monotonic() + 10
        )
        finalize_seconds = time.monotonic() - started
        self.assertNotIn("version", manifest)
        self.assertGreaterEqual(before, 256 * 1024 * 1024)
        self.assertEqual(
            cache.bytes_hashed, before, "Finalization reread a stable 256MiB checkpoint"
        )
        self.assertGreaterEqual(cache.cache_hits, 4)
        old = next(item for item in manifest["files"] if item["path"].endswith("checkpoint.bin"))
        stamp = path.stat()
        with path.open("r+b") as stream:
            stream.seek(-1, os.SEEK_END)
            stream.write(b"b")
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        manifest = bundle.finalize(JOB, self.folder, cache=cache, quiesced=True)
        changed = next(
            item for item in manifest["files"] if item["path"].endswith("checkpoint.bin")
        )
        self.assertNotEqual(old["sha256"], changed["sha256"])
        self.assertEqual(cache.bytes_hashed - before, 256 * 1024 * 1024)
        print(
            json.dumps(
                {
                    "checkpoint_bytes": path.stat().st_size,
                    "write_and_prime_seconds": round(warm_seconds, 3),
                    "cached_finalize_seconds": round(finalize_seconds, 3),
                    "stable_finalize_bytes_hashed": 0,
                }
            ),
            flush=True,
        )

    def test_background_cache_and_immutable_checkpoint_journal(self):
        source = self.output / "source.bin"
        source.write_bytes(b"checkpoint")
        journal = bundle.CheckpointJournal(self.output / "checkpoints")
        first, second = journal.publish(source), journal.publish(source)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b"checkpoint")
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o400)
        cache = bundle.HashCache(self.output).start(interval=0.01)
        until = time.monotonic() + 2
        while cache.bytes_hashed < 10 and time.monotonic() < until:
            time.sleep(0.01)
        cache.stop()
        self.assertGreater(cache.bytes_hashed, 0)
        bundle.finalize(JOB, self.folder, cache=cache, quiesced=True)
        with self.assertRaises(TimeoutError):
            journal.publish(source, deadline=time.monotonic() - 1)
        self.assertEqual(first.read_bytes(), b"checkpoint")

    def test_mutation_or_inode_replacement_during_hash_has_no_complete_manifest(self):
        path = self.output / "checkpoint"
        path.write_bytes(b"original")
        cache = bundle.HashCache(self.output)
        real = cache.digest

        def mutate(name, *args, **kwargs):
            digest = real(name, *args, **kwargs)
            if name == "checkpoint":
                path.unlink()
                path.write_bytes(b"replaced")
            return digest

        with patch.object(cache, "digest", side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError, "changed during snapshot"):
                bundle.finalize(JOB, self.folder, cache=cache, quiesced=True)
        self.assertFalse((self.folder / bundle.MANIFEST).exists())
        with self.assertRaisesRegex(ValueError, "Quiesce"):
            bundle.finalize(JOB, self.folder)

    def test_extraction_refuses_existing_symlink_destination_and_link_parent(self):
        manifest, metadata, objects = archive_fixture()
        archive = self.root / "archive.tar"
        archive.write_bytes(objects[manifest["bundle"]["archive"]])
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        for target in (self.root / "linked", self.root / "linked/child"):
            with self.assertRaises((ValueError, FileExistsError)):
                bundle.extract_bundle(archive, metadata, target)
        self.assertEqual(list(outside.iterdir()), [])
