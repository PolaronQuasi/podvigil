"""Download from Runpod storage after GPU deletion; verify before deleting storage."""

from datetime import datetime, timedelta
from pathlib import Path
from contextlib import nullcontext
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile

from .credentials import local_secrets
from .jobspec import required_artifacts
from .pathsafe import is_link_or_reparse, portable_paths, safe_relative, validate_input_source
from .podrt.bundle import validate_metadata, verify_bundle
from .podrt.records import digest, durable_replace, exclusive_lock, save, utc
from .policy import S3_DATACENTERS, STANDARD_ARTIFACTS

MANIFEST_FILE = "artifact-manifest.json"
RECEIPT_FILE = "backup-verified.json"
METADATA_LIMIT = 16 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
OUTCOMES = {
    "success",
    "training_failed",
    "time_limit",
    "interrupted",
    "agent_failed",
    "agent_preflight_failed",
    "cancelled",
}

CONTROL_FILES = frozenset(
    {
        "agent.json",
        "start.json",
        "cancel.json",
        "started-once.json",
        "start.lock",
        MANIFEST_FILE,
        "termination.json",
        "control/identity.json",
        "control/self-delete.json",
        "control/finalization-incomplete.json",
        "control/output-links.json",
        "control/guard-failure.json",
    }
)

# What a Pod guard may leave before the controller releases the workload. No start marker,
# manifest or artifact is in this set: any of those keeps the volume.
UNRELEASED_CONTROL_FILES = frozenset(
    {
        "agent.json",
        "termination.json",
        "control/identity.json",
        "control/self-delete.json",
        "control/guard-failure.json",
    }
)

# A released job that published nothing leaves the release marker, the claim,
# the Pod's status and its control records. _released_record checks each exact name.
# Any artifact, bundle part, manifest, link index, salvaged output or other object
# keeps the volume.
RELEASED_RECORDS = frozenset(
    {
        "start.json",
        "cancel.json",
        "started-once.json",
        "start.lock",
        "agent.json",
        "control/identity.json",
        "control/self-delete.json",
        "control/guard-failure.json",
        "control/finalization-incomplete.json",
    }
)

POD_ACTORS = ("pod_guard", "pod_supervisor")
OWNER_FILE = "managed-owner.json"
# The bundle's link index names objects that must never be read.
LINK_INDEX = "control/output-links.json"


def _unique_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member.")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("Non-finite JSON number.")


def _metadata(stream):
    raw = stream.read(METADATA_LIMIT + 1)
    if len(raw) > METADATA_LIMIT:
        raise ValueError("Artifact metadata exceeds 16 MiB.")
    return (
        json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_members,
            parse_constant=_nonfinite,
        ),
        raw,
    )


def _job_prefix(job):
    job_id = safe_relative(job["job_id"])
    if "/" in job_id:
        raise ValueError("Invalid job ID for artifact storage.")
    return "jobs/" + job_id + "/"


def _manifest_entries(job, manifest):
    mandatory = {"job_id", "complete", "files"}
    v2 = (
        isinstance(manifest, dict)
        and type(manifest.get("version")) is int
        and manifest["version"] == 2
    )
    fields = mandatory | ({"version", "bundle"} if v2 else set())
    if (
        not isinstance(manifest, dict)
        or not fields <= manifest.keys() <= fields | {"created_utc"}
        or manifest["job_id"] != job["job_id"]
        or manifest["complete"] is not True
    ):
        raise RuntimeError("No valid complete artifact manifest. Keep the volume for recovery.")
    if v2:
        bundle = manifest["bundle"]
        if (
            not isinstance(bundle, dict)
            or set(bundle) != {"archive", "metadata"}
            or any(not isinstance(value, str) for value in bundle.values())
            or not re.fullmatch(r"output-bundle/[0-9a-f]{32}/archive\.tar", bundle["archive"])
            or bundle["metadata"] != bundle["archive"].removesuffix("archive.tar") + "metadata.json"
        ):
            raise ValueError("Invalid output bundle descriptor. Keep the volume.")

    if "created_utc" in manifest:
        try:
            offset = datetime.fromisoformat(manifest["created_utc"]).utcoffset()
        except (TypeError, ValueError, OverflowError):
            offset = None
        if offset != timedelta(0):
            raise RuntimeError("Invalid artifact manifest timestamp. Keep the volume.")

    entries = manifest["files"]
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("Artifact manifest files must be a nonempty list. Keep the volume.")
    for item in entries:
        if (
            not isinstance(item, dict)
            or item.keys() != {"path", "size", "sha256"}
            or not isinstance(item["path"], str)
            or type(item["size"]) is not int
            or item["size"] < 0
            or not isinstance(item["sha256"], str)
            or not SHA256.fullmatch(item["sha256"])
        ):
            raise RuntimeError("Invalid artifact manifest file entry. Keep the volume.")

    if v2 and {item["path"] for item in entries} != {
        "artifacts/result.json",
        "artifacts/train.log",
        *manifest["bundle"].values(),
    }:
        raise ValueError(
            "Bundle manifest must contain exactly its archive, metadata, result and log."
        )
    return entries


def _checked_path(path, *, directory=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        is_link_or_reparse(info)
        or not expected
        or (not directory and info.st_nlink != 1)
        or (os.name == "nt" and path.resolve(strict=True).name != path.name)
    ):
        raise RuntimeError("Unsafe existing path in the artifact destination.")
    return True


def _destination(path, *, create=False):
    destination = Path(path).absolute()
    for parent in reversed(destination.parents):
        _checked_path(parent, directory=True)
    if create:
        destination.mkdir(parents=True, exist_ok=True)
    if not _checked_path(destination, directory=True):
        raise RuntimeError("Local artifact destination is missing.")
    return destination


def _targets(destination, names, manifest=None):
    paths = portable_paths(names)
    targets, checked = {}, set()
    for name, parts in paths.items():
        bundle_paths = (
            set(manifest["bundle"].values()) if manifest and manifest.get("version") == 2 else set()
        )
        if (len(parts) < 2 or parts[0] != "artifacts") and name not in bundle_paths:
            raise ValueError("Artifact path must be beneath artifacts/.")
        target = destination
        for index, part in enumerate(parts, 1):
            target = target / part
            if target not in checked:
                _checked_path(target, directory=index < len(parts))
                checked.add(target)
        targets[name] = target
    return targets


def _read_local(path):
    if not _checked_path(path):
        raise RuntimeError("Local backup metadata or job result is missing.")
    try:
        with path.open("rb") as stream:
            return _metadata(stream)
    except ValueError:
        raise RuntimeError("Local backup metadata or job result is malformed.") from None


def _matches(path, item):
    return path.stat().st_size == item["size"] and (
        "sha256" not in item or digest(path) == item["sha256"]
    )


def _bundle_metadata(job, manifest, targets):
    if manifest.get("version") != 2:
        return None
    bundle = manifest["bundle"]
    metadata, _ = _read_local(targets[bundle["metadata"]])
    verify_bundle(targets[bundle["archive"]], metadata, job["job_id"])
    if metadata["snapshot_id"] != bundle["archive"].split("/")[1]:
        raise ValueError("Bundle snapshot identity mismatch.")
    records = validate_metadata(metadata, job["job_id"])
    for name in STANDARD_ARTIFACTS:
        original = records.get(name, {})
        if original.get("kind") not in ("file", "hardlink") or not _matches(
            targets["artifacts/" + name], original
        ):
            raise RuntimeError("Bundle result/log differs from manifest copies. Keep the volume.")
    return records


def _verify_files(job, entries, targets, manifest=None):
    mandatory = {"artifacts/" + name for name in STANDARD_ARTIFACTS}
    if not mandatory <= targets.keys():
        raise RuntimeError("Missing job result or log. Keep the volume.")
    for item in entries:
        target = targets[item["path"]]
        if not _checked_path(target) or not _matches(target, item):
            raise RuntimeError("Local artifact missing or checksum mismatch. Keep the volume.")

    result, _ = _read_local(targets["artifacts/result.json"])
    if (
        not isinstance(result, dict)
        or result.get("job_id") != job["job_id"]
        or not isinstance(result.get("outcome"), str)
        or result["outcome"] not in OUTCOMES
    ):
        raise RuntimeError("Invalid job result. Keep the volume.")
    records = _bundle_metadata(job, manifest, targets) if manifest is not None else None
    if result["outcome"] == "success":
        required = {"artifacts/" + name for name in required_artifacts(job.get("config", {}))}
        available = (
            targets.keys()
            if records is None
            else {
                "artifacts/" + name
                for name, record in records.items()
                if record["kind"] in ("file", "hardlink")
            }
        )
        if not required <= available:
            raise RuntimeError("Successful job is missing required artifacts. Keep the volume.")


def _receipt(job, bucket, manifest_bytes, file_count):
    return {
        "job_id": job["job_id"],
        "volume_id": bucket,
        "file_count": file_count,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }


def verify_local_backup(job, bucket, destination):
    """Return the verified manifest or raise. Caller holds the whole-job recovery lock."""
    destination = _destination(destination)
    manifest, raw = _read_local(destination / MANIFEST_FILE)
    entries = _manifest_entries(job, manifest)
    receipt, _ = _read_local(destination / RECEIPT_FILE)
    if (
        not isinstance(receipt, dict)
        or type(receipt.get("file_count")) is not int
        or receipt != _receipt(job, bucket, raw, len(entries))
    ):
        raise RuntimeError("Local backup receipt does not match this job, volume, or manifest.")
    targets = _targets(destination, (item["path"] for item in entries), manifest)
    _verify_files(job, entries, targets, manifest)
    return manifest


def _sync_directories(destination, targets):
    if os.name == "nt":
        # durable_replace publishes each file through Windows write-through rename.
        return
    parents = {destination, destination.parent}
    for target in targets.values():
        for parent in target.parents:
            if parent == destination:
                break
            parents.add(parent)
    # durable_replace syncs the leaf directory. Commit nested directory entries too.
    for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _input_records(job):
    entries = job.get("inputs", [])
    if not isinstance(entries, list):
        raise ValueError("Original input ledger is malformed.")
    records = {}
    for item in entries:
        fields = {"remote", "size", "sha256"}
        if (
            not isinstance(item, dict)
            or not fields <= item.keys() <= fields | {"local"}
            or type(item["size"]) is not int
            or item["size"] < 0
            or not isinstance(item["sha256"], str)
            or not SHA256.fullmatch(item["sha256"])
        ):
            raise ValueError("Original input ledger entry is malformed.")
        key = "inputs/" + safe_relative(item["remote"])
        if key in records:
            raise ValueError("Duplicate original input ledger entry.")
        records[key] = item
    return records


def _manifest_records(job):
    """The input manifest launch wrote to the volume, by key, with the size and SHA-256 of its exact bytes;
    empty for ledgers from before the manifest."""
    record = job.get("input_manifest")
    if record is None:
        return {}
    key = _job_prefix(job) + "control/inputs.json"
    if (
        not isinstance(record, dict)
        or record.get("key") != key
        or type(record.get("size")) is not int
        or record["size"] < 0
        or not isinstance(record.get("sha256"), str)
        or not SHA256.fullmatch(record["sha256"])
    ):
        raise ValueError("Input manifest ledger entry is malformed.")
    return {key: {"size": record["size"], "sha256": record["sha256"]}}


def _released_record(job, name, path, pod_id):
    """Raise unless `path` holds the record `name` exactly as the release or this job's Pod writes it."""
    data = Path(path).read_bytes()
    if name == "start.lock":
        valid = data in (b"", b"0")  # the claim's lock file: one byte, never fenced
    else:
        try:
            value = json.loads(
                data.decode("utf-8"), object_pairs_hook=_unique_members, parse_constant=_nonfinite
            )
        except (ValueError, UnicodeDecodeError):
            value = None
        valid = isinstance(value, dict)
        if valid and name == "started-once.json":
            valid = set(value) == {"started_utc"} and isinstance(value["started_utc"], str)
        elif valid and name == "start.json":
            valid = value == {"job_id": job["job_id"], "pod_id": pod_id}
        elif valid and name == "cancel.json":
            # The watchdog's request to finish (watchdog.finishing).
            valid = (
                set(value) == {"job_id", "pod_id", "requested_utc", "grace_seconds"}
                and value["job_id"] == job["job_id"]
                and value["pod_id"] == pod_id
            )
        elif valid:
            valid = (
                value.get("job_id") == job["job_id"]
                and value.get("pod_id") == pod_id
                and value.get("actor") in POD_ACTORS
            )
            if valid and name == "control/finalization-incomplete.json":
                valid = value.get("complete") is False and not value.get("salvaged_files")
    if not valid:
        raise RuntimeError(
            "Volume record " + json.dumps(name) + " is not one this job's Pod leaves. "
            "Keep the volume."
        )


def _recreatable_input(job, item):
    configured = job.get("config", {}).get("inputs", [])
    if not isinstance(configured, list):
        raise ValueError("Configured input sources are malformed.")
    sources = [
        entry.get("local")
        for entry in configured
        if isinstance(entry, dict) and entry.get("remote") == item["remote"]
    ]
    if "local" in item:
        sources.append(item["local"])
    for source in sources:
        if not isinstance(source, str):
            continue
        try:
            path = validate_input_source(source)
            if _matches(path, item):
                return path
        except (OSError, ValueError, RuntimeError):
            continue
    return None


def _directory_keys(keys):
    directories = set()
    for key in keys:
        parts = key.split("/")
        directories.update("/".join(parts[:index]) + "/" for index in range(1, len(parts)))
    return directories


def _record_export(stream, record):
    stream.write(json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def verify_local_audit(job, volume_id, folder, audit, pod_id):
    """Recheck a special recovery's saved audit and every copy, without relying on S3.

    Only the audit's own snapshot may supply copies. An original input outside the
    ledger is allowed only at its configured source, with its original size/hash.
    This is evidence for finishing an interrupted recovery, never DELETE authority.
    """
    try:
        root = Path(folder).resolve()
        mode = audit["mode"]
        parent = "partial-downloads" if mode == "partial_outputs_copied" else "downloads"
        if (
            mode not in ("unreleased_pod", "released_unpublished", "partial_outputs_copied")
            or audit["job_id"] != job["job_id"]
            or audit["volume_id"] != volume_id
        ):
            raise ValueError()

        snapshot = Path(audit["control_backup_directory"])
        if (
            not snapshot.is_absolute()
            or snapshot.parent != root / parent
            or not re.fullmatch(r"volume-audit-[A-Za-z0-9_-]+", snapshot.name)
        ):
            raise ValueError()
        _destination(snapshot)
        index, _ = _read_local(snapshot / "objects.json")
        records = audit["objects"]
        if (
            not isinstance(records, list)
            or type(audit["object_count"]) is not int
            or len(records) != audit["object_count"]
            or index != {"job_id": job["job_id"], "volume_id": volume_id, "objects": records}
        ):
            raise ValueError()

        remote = [
            {key: value for key, value in record.items() if key != "local_path"}
            for record in records
        ]
        raw = json.dumps(remote, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
        if hashlib.sha256(raw).hexdigest() != audit["inventory_sha256"]:
            raise ValueError()

        inputs, manifests, seen = _input_records(job), _manifest_records(job), set()
        prefix = _job_prefix(job)
        controls = {OWNER_FILE} | {
            prefix + name
            for name in (
                UNRELEASED_CONTROL_FILES
                if mode == "unreleased_pod"
                else RELEASED_RECORDS
                if mode == "released_unpublished"
                else CONTROL_FILES
            )
        }

        for record in records:
            key, kind = record["key"], record["kind"]
            if not isinstance(key, str) or not key or key in seen:
                raise ValueError()
            seen.add(key)
            if kind == "directory_marker":
                if (
                    record != {"key": key, "kind": kind, "size": 0}
                    or not key.endswith("/")
                    or type(record["size"]) is not int
                ):
                    raise ValueError()
                continue

            expected_kind = (
                "input"
                if key in inputs
                else "input_manifest"
                if key in manifests
                else "control"
                if key in controls
                else "partial_output"
            )
            if (
                kind != expected_kind
                or (kind == "partial_output" and mode != "partial_outputs_copied")
                or set(record) != {"key", "kind", "size", "sha256", "local_path"}
                or type(record["size"]) is not int
                or record["size"] < 0
                or not isinstance(record["sha256"], str)
                or not SHA256.fullmatch(record["sha256"])
            ):
                raise ValueError()

            source = Path(record["local_path"])
            if source.parent != snapshot or not re.fullmatch(r"[0-9]{8,}\.bin", source.name):
                original = _recreatable_input(job, inputs[key]) if kind == "input" else None
                if original is None or source != original:
                    raise ValueError()
            if not _checked_path(source) or not _matches(source, record):
                raise ValueError()
            original = inputs.get(key) or manifests.get(key)
            if original and any(record[name] != original[name] for name in ("size", "sha256")):
                raise ValueError()
            if key == OWNER_FILE and _read_local(source)[0] != {"job_id": job["job_id"]}:
                raise ValueError()
            if mode == "released_unpublished" and kind == "control" and key != OWNER_FILE:
                _released_record(job, key[len(prefix) :], source, pod_id)
            if mode == "unreleased_pod" and key == prefix + "agent.json":
                agent, _ = _read_local(source)
                if (
                    not isinstance(agent, dict)
                    or agent.get("job_id") != job["job_id"]
                    or agent.get("pod_id") != pod_id
                    or agent.get("actor") != "pod_guard"
                ):
                    raise ValueError()

        if mode != "partial_outputs_copied" and prefix + "agent.json" not in seen:
            raise ValueError()
        missing = audit["inputs_absent_from_volume"]
        if (
            not isinstance(missing, list)
            or len(missing) != len(inputs.keys() - seen)
            or {item["key"] for item in missing} != inputs.keys() - seen
        ):
            raise ValueError()
        for item in missing:
            original = inputs[item["key"]]
            source = _recreatable_input(job, original)
            if (
                source is None
                or str(source) != item["local_path"]
                or any(item[name] != original[name] for name in ("size", "sha256"))
            ):
                raise ValueError()
        return snapshot / "objects.json"
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        raise RuntimeError(
            "Saved volume audit is missing, changed or invalid; completion is not verified."
        ) from None


class Storage:
    def __init__(self, data_center, client=None):
        if client is not None:
            self.client = client
            return
        if data_center not in S3_DATACENTERS:
            raise ValueError("Unknown Runpod S3 data center.")
        keys = local_secrets()
        if not all(keys.values()):
            raise RuntimeError(
                "Set Runpod's separate S3 access/secret keys in .env before launch; "
                "no resources created."
            )
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise RuntimeError(
                "Install requirements.txt in this folder's own venv before launch."
            ) from None
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://s3api-{data_center.lower()}.runpod.io",
            region_name=data_center,
            aws_access_key_id=keys["RUNPOD_S3_ACCESS_KEY"],
            aws_secret_access_key=keys["RUNPOD_S3_SECRET_KEY"],
            config=Config(
                signature_version="s3v4",
                connect_timeout=15,
                read_timeout=30,
                retries={"max_attempts": 4, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        )

    def put_json(self, bucket, key, value):
        self.client.put_object(Bucket=bucket, Key=key, Body=json.dumps(value).encode())

    def put_bytes(self, bucket, key, data):
        self.client.put_object(Bucket=bucket, Key=key, Body=bytes(data))

    def object_digest(self, bucket, key, size):
        """Size and SHA-256 of the object's actual bytes; raises unless it holds exactly `size` bytes."""
        return self._read_object(bucket, key, size)

    def get_json(self, bucket, key):
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
            with response["Body"] as stream:
                value, _ = _metadata(stream)
            return value
        except ValueError:
            raise RuntimeError("Storage metadata is malformed or exceeds 16 MiB.") from None
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if str(code) in ("NoSuchKey", "404", "NotFound"):
                return None
            # Do not surface SDK request objects or credential-bearing diagnostics.
            raise RuntimeError(
                "Storage read failed; check connectivity and S3 credentials."
            ) from None

    def upload(self, bucket, key, source):
        self.client.upload_file(str(validate_input_source(source)), bucket, key)

    def _objects(self, bucket, prefix=""):
        """Walk every page and reject incomplete or ambiguous listing responses."""
        request = {"Bucket": bucket, "Prefix": prefix}
        seen_keys, seen_tokens = set(), set()
        while True:
            try:
                page = self.client.list_objects_v2(**request)
            except Exception:
                raise RuntimeError("S3 artifact inventory failed. Keep the volume.") from None
            if not isinstance(page, dict) or type(page.get("IsTruncated")) is not bool:
                raise RuntimeError("Malformed S3 artifact inventory. Keep the volume.")

            contents = page.get("Contents", [])
            if not isinstance(contents, list) or page.get("CommonPrefixes", []) != []:
                raise RuntimeError("Malformed S3 artifact inventory entries. Keep the volume.")
            if "KeyCount" in page and (
                type(page["KeyCount"]) is not int or page["KeyCount"] != len(contents)
            ):
                raise RuntimeError("Inconsistent S3 artifact inventory count. Keep the volume.")

            for item in contents:
                key = item.get("Key") if isinstance(item, dict) else None
                size = item.get("Size") if isinstance(item, dict) else None
                if (
                    not isinstance(key, str)
                    or not key
                    or not key.startswith(prefix)
                    or key in seen_keys
                    or type(size) is not int
                    or size < 0
                ):
                    raise RuntimeError(
                        "Malformed or duplicate S3 artifact inventory object. Keep the volume."
                    )
                seen_keys.add(key)
                yield key, size

            token = page.get("NextContinuationToken")
            if not page["IsTruncated"] and token in (None, ""):
                return
            if (
                not page["IsTruncated"]
                or not isinstance(token, str)
                or not token
                or token in seen_tokens
            ):
                raise RuntimeError("Invalid S3 artifact inventory pagination. Keep the volume.")
            seen_tokens.add(token)
            request["ContinuationToken"] = token

    def _inventory(self, bucket, prefix):
        inventory = {}
        for key, size in self._objects(bucket, prefix + "artifacts/"):
            if key.endswith("/") and size == 0:
                safe_relative(key[len(prefix) : -1])
                continue
            inventory[key[len(prefix) :]] = size
        return inventory

    def _read_object(self, bucket, key, expected_size, target=None):
        """Hash the actual remote bytes, optionally retaining them in a staging file."""
        checksum, size = hashlib.sha256(), 0
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
            with (
                response["Body"] as stream,
                target.open("xb") if target else nullcontext() as output,
            ):
                for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    size += len(chunk)
                    if size > expected_size:
                        raise ValueError("Object exceeds its listed size.")
                    checksum.update(chunk)
                    if output is not None:
                        output.write(chunk)
        except Exception:
            raise RuntimeError(
                "Cannot read volume object " + json.dumps(key) + ". Keep the volume."
            ) from None
        if size != expected_size:
            raise RuntimeError(
                "Volume object size changed " + json.dumps(key) + ". Keep the volume."
            )
        return {"size": size, "sha256": checksum.hexdigest()}

    def _download(self, bucket, prefix, destination, entries, targets):
        with tempfile.TemporaryDirectory(prefix=".artifact-stage-", dir=destination) as staging:
            for index, item in enumerate(entries):
                name = item["path"]
                target = targets[name]
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = Path(staging) / str(index)
                reused = False
                if (
                    "sha256" in item
                    and _checked_path(target)
                    and target.stat().st_size == item["size"]
                ):
                    # Republish reused bytes too, including files from an interrupted recovery.
                    shutil.copyfile(target, partial)
                    reused = _matches(partial, item)
                if not reused:
                    partial.unlink(missing_ok=True)
                    try:
                        self.client.download_file(bucket, prefix + name, str(partial))
                    except Exception:
                        raise RuntimeError("Artifact download failed. Keep the volume.") from None
                    if not _checked_path(partial) or not _matches(partial, item):
                        raise RuntimeError("Artifact size or checksum mismatch. Keep the volume.")
                durable_replace(partial, target)
        _sync_directories(destination, targets)

    def collect(self, job, bucket, destination):
        """Commit artifact copies. Volume deletion additionally requires audit_for_delete."""
        prefix = _job_prefix(job)
        destination = _destination(destination, create=True)
        lock = destination / "collect.lock"
        _checked_path(lock)
        with exclusive_lock(lock):
            manifest = self.get_json(bucket, prefix + MANIFEST_FILE)
            entries = _manifest_entries(job, manifest)
            targets = _targets(destination, (item["path"] for item in entries), manifest)
            for name in (MANIFEST_FILE, RECEIPT_FILE):
                _checked_path(destination / name)

            self._download(bucket, prefix, destination, entries, targets)
            _verify_files(job, entries, targets, manifest)
            if manifest.get("version") == 2:
                records = _bundle_metadata(job, manifest, targets)
                expected = {
                    "artifacts/" + name: record["size"]
                    for name, record in records.items()
                    if record["kind"] in ("file", "hardlink")
                }
                actual = dict(self._objects(bucket, prefix + "artifacts/"))
                # A symlink object may expose its target via the provider bridge.
                # Never GET it to investigate. Retain rather than risk copying a secret.
                for name, record in records.items():
                    if record["kind"] == "symlink" and prefix + "artifacts/" + name in actual:
                        raise RuntimeError(
                            "S3 exposes a symlink object; retain volume without reading its target."
                        )
                markers = {prefix + "artifacts/"} | {
                    prefix + "artifacts/" + name + "/"
                    for name, record in records.items()
                    if record["kind"] == "directory"
                }
                actual = {
                    key[len(prefix) :]: size
                    for key, size in actual.items()
                    if not (key in markers and size == 0)
                }
                if actual != expected:
                    raise RuntimeError(
                        "S3 original inventory does not match bundle coverage. Keep the volume."
                    )
            elif self._inventory(bucket, prefix) != {
                item["path"]: item["size"] for item in entries
            }:
                raise RuntimeError(
                    "S3 artifact inventory does not match the manifest. Keep the volume."
                )

            save(destination / MANIFEST_FILE, manifest)
            _, raw = _read_local(destination / MANIFEST_FILE)
            save(destination / RECEIPT_FILE, _receipt(job, bucket, raw, len(entries)))
            return manifest

    def audit_for_delete(
        self,
        job,
        volume_id,
        backup_dir=None,
        allow_pristine_inputs=False,
        allow_unreleased=False,
        released_pod_id=None,
        copy_all=False,
    ):
        """Audit every volume key while the caller holds its recovery lock.

        The caller must verify Pod termination and fence all writers. Passing
        allow_pristine_inputs=True asserts a positive never-dispatched record
        checked under the closed creation fence. Missing ledger files cannot
        establish that assertion. Adding allow_unreleased=True asserts instead
        that a Pod existed but the controller never released the workload: the
        guard's own pre-release records are then accepted beside the pristine
        inputs and copied to backup_dir; a start marker, manifest or artifact
        is still an unexpected object and keeps the volume. Passing
        released_pod_id (with pristine inputs) asserts instead that the
        workload was released on that Pod, whose deletion is verified, and that
        no manifest exists: RELEASED_RECORDS are then accepted, each checked
        to be this job's and this Pod's, and copied to backup_dir; the Pod's
        agent.json must be among them. Any other object keeps the volume.
        Passing copy_all=True (with pristine inputs) asserts instead that the
        operator asked for it (recover --accept-partial), that the workload
        was released on a Pod whose deletion is verified, and that no
        complete manifest exists: every other object is then accepted, read
        once, copied to backup_dir and the copy checked, and the listing
        must not change. The inputs and the input manifest are still checked
        against their originals, and the owner record against the job. An
        object the link index names keeps the volume: no link target is read.
        """
        if not isinstance(job, dict) or not isinstance(job.get("job_id"), str):
            raise ValueError("Volume audit requires a valid job identity.")
        if not isinstance(job.get("config", {}), dict):
            raise ValueError("Volume audit job configuration is malformed.")
        try:
            return self._audit_for_delete(
                job,
                volume_id,
                backup_dir,
                allow_pristine_inputs,
                allow_unreleased,
                released_pod_id,
                copy_all,
            )
        except OSError:
            raise RuntimeError(
                "Volume audit could not durably verify local data. Keep the volume."
            ) from None

    def _audit_for_delete(
        self,
        job,
        volume_id,
        backup_dir,
        allow_pristine_inputs,
        allow_unreleased=False,
        released_pod_id=None,
        copy_all=False,
    ):
        prefix = _job_prefix(job)
        if (
            not isinstance(volume_id, str)
            or not volume_id
            or "/" in safe_relative(volume_id)
            or job.get("volume_id", volume_id) != volume_id
        ):
            raise ValueError("Volume audit identity does not match the job.")
        if type(allow_pristine_inputs) is not bool:
            raise ValueError("Pristine input authorization must be a boolean.")
        if type(allow_unreleased) is not bool or (allow_unreleased and not allow_pristine_inputs):
            raise ValueError(
                "Unreleased-Pod authorization must be a boolean used with pristine inputs."
            )
        if backup_dir is None and not allow_pristine_inputs:
            raise ValueError("Completed-job volume deletion requires a verified backup directory.")
        if backup_dir is None and allow_unreleased:
            raise ValueError(
                "Unreleased-Pod volume deletion requires a backup directory "
                "for the guard's records."
            )
        if released_pod_id is not None and (
            not isinstance(released_pod_id, str)
            or not released_pod_id
            or not allow_pristine_inputs
            or allow_unreleased
            or backup_dir is None
        ):
            raise ValueError(
                "Released-Pod volume deletion needs the Pod's ID, pristine inputs and a backup "
                "directory for the Pod's records."
            )
        if type(copy_all) is not bool or (
            copy_all
            and (
                not allow_pristine_inputs
                or allow_unreleased
                or released_pod_id is not None
                or backup_dir is None
            )
        ):
            raise ValueError(
                "Copying every object needs pristine inputs and a backup directory, and no other "
                "mode."
            )

        destination = _destination(backup_dir, create=True) if backup_dir is not None else None
        lock = nullcontext()
        if destination is not None:
            _checked_path(destination / "collect.lock")
            lock = exclusive_lock(destination / "collect.lock")
        with lock:
            manifest = None
            artifacts = {}
            archived = {}
            archived_dirs = set()
            symlink_keys = set()
            if not allow_pristine_inputs:
                manifest = verify_local_backup(job, volume_id, destination)
                artifacts = {prefix + item["path"]: item for item in manifest["files"]}
                if manifest.get("version") == 2:
                    targets = _targets(
                        destination, (item["path"] for item in manifest["files"]), manifest
                    )
                    metadata = _bundle_metadata(job, manifest, targets)
                    archived = {
                        prefix + "artifacts/" + name: record
                        for name, record in metadata.items()
                        if record["kind"] in ("file", "hardlink")
                    }
                    archived_dirs = {
                        prefix + "artifacts/" + name + "/"
                        for name, record in metadata.items()
                        if record["kind"] == "directory"
                    }
                    symlink_keys = {
                        prefix + "artifacts/" + name
                        for name, record in metadata.items()
                        if record["kind"] == "symlink"
                    }

            inputs = _input_records(job)
            manifests = _manifest_records(job)  # checked byte for byte, in every mode
            controls = {OWNER_FILE}
            if not allow_pristine_inputs:
                controls.update(prefix + name for name in CONTROL_FILES)
            elif allow_unreleased:
                controls.update(prefix + name for name in UNRELEASED_CONTROL_FILES)
            elif released_pod_id is not None:
                controls.update(prefix + name for name in RELEASED_RECORDS)
            elif copy_all:
                # These are named "control". The other objects are copied too.
                controls.update(prefix + name for name in CONTROL_FILES)
            known = controls | inputs.keys() | artifacts.keys() | archived.keys() | manifests.keys()
            directories = _directory_keys(known) | archived_dirs

            inventory = dict(self._objects(volume_id))
            if symlink_keys & inventory.keys():
                raise RuntimeError(
                    "S3 exposes a symlink object; retain volume without reading its target."
                )
            if copy_all and self._link_keys(volume_id, prefix, job) & inventory.keys():
                raise RuntimeError(
                    "S3 exposes an object the link index names; retain volume without reading "
                    "its target."
                )
            unexpected = (
                []
                if copy_all
                else [
                    key
                    for key, size in inventory.items()
                    if key not in known
                    and not (key in directories and key.endswith("/") and size == 0)
                ]
            )
            if unexpected:
                raise RuntimeError(
                    "Unexpected volume objects. Keep the volume. Keys "
                    + json.dumps(sorted(unexpected))
                )
            if released_pod_id is not None and prefix + "agent.json" not in inventory:
                raise RuntimeError(
                    "The Pod's own status record is not on the volume. Keep the volume."
                )
            if (
                manifest is not None
                and not (artifacts.keys() | archived.keys() | {prefix + MANIFEST_FILE})
                <= inventory.keys()
            ):
                raise RuntimeError("Volume is missing manifest objects. Keep the volume.")

            missing_inputs = []
            for key in inputs.keys() - inventory.keys():
                source = _recreatable_input(job, inputs[key])
                if source is None:
                    raise RuntimeError(
                        "Original input is absent from both the volume and a verified local "
                        "source. Keep the volume. Key " + json.dumps(key)
                    )
                missing_inputs.append(
                    {
                        "key": key,
                        "local_path": str(source),
                        **{name: inputs[key][name] for name in ("size", "sha256")},
                    }
                )

            snapshot = None
            if destination is not None:
                snapshot = Path(tempfile.mkdtemp(prefix="volume-audit-", dir=destination))
            records, copies = [], {}
            with tempfile.TemporaryDirectory(
                prefix=".volume-audit-stage-", dir=destination
            ) as staging:
                for index, (key, size) in enumerate(sorted(inventory.items())):
                    if key.endswith("/") and size == 0:
                        records.append({"key": key, "size": 0, "kind": "directory_marker"})
                        continue
                    original = (
                        artifacts.get(key)
                        or inputs.get(key)
                        or archived.get(key)
                        or manifests.get(key)
                    )
                    source = None
                    if key in artifacts:
                        source = destination / artifacts[key]["path"]
                        kind = "artifact"
                    elif key in inputs:
                        source = _recreatable_input(job, inputs[key])
                        kind = "input"
                    elif key in archived:
                        kind = "archived_original"
                    elif key in manifests:
                        kind = "input_manifest"
                    else:
                        kind = "partial_output" if copy_all and key not in controls else "control"
                    needs_copy = source is None and kind != "archived_original"
                    if kind == "input" and needs_copy and snapshot is None:
                        raise RuntimeError(
                            "Input has no verified local source or backup. Keep the volume. Key "
                            + json.dumps(key)
                        )
                    staged = Path(staging) / str(index) if needs_copy else None
                    observed = self._read_object(volume_id, key, size, staged)
                    if original is not None and observed != {
                        name: original[name] for name in ("size", "sha256")
                    }:
                        raise RuntimeError(
                            "Remote content differs from its original checksum. "
                            "Keep the volume. Key " + json.dumps(key)
                        )
                    if key == OWNER_FILE:
                        owner, _ = _read_local(staged)
                        if owner != {"job_id": job["job_id"]}:
                            raise RuntimeError(
                                "Volume owner probe does not match the job. Keep the volume."
                            )
                    if released_pod_id is not None and kind == "control" and key != OWNER_FILE:
                        _released_record(job, key[len(prefix) :], staged, released_pod_id)
                    if key == prefix + MANIFEST_FILE:
                        remote_manifest, _ = _read_local(staged)
                        if remote_manifest != manifest:
                            raise RuntimeError(
                                "Remote manifest differs from the verified local manifest. "
                                "Keep the volume."
                            )
                    record = {"key": key, "kind": kind, **observed}
                    if kind == "archived_original":
                        record["archive_path"] = str(destination / manifest["bundle"]["archive"])
                    if needs_copy and snapshot is not None:
                        source = snapshot / f"{index:08d}.bin"
                        durable_replace(staged, source)
                        copies[key] = source
                    if source is not None:
                        if not _checked_path(source) or not _matches(source, observed):
                            raise RuntimeError(
                                "Volume audit local copy verification failed. Keep the volume."
                            )
                        record["local_path"] = str(source)
                    records.append(record)

            if snapshot is not None:
                _sync_directories(snapshot, copies)
                save(
                    snapshot / "objects.json",
                    {"job_id": job["job_id"], "volume_id": volume_id, "objects": records},
                )
            if dict(self._objects(volume_id)) != inventory:
                raise RuntimeError("Volume inventory changed during its audit. Keep the volume.")
            if manifest is not None:
                verify_local_backup(job, volume_id, destination)
            for record in records:
                if "local_path" in record:
                    source = Path(record["local_path"])
                    if record["kind"] == "input" and record["key"] not in copies:
                        source = validate_input_source(source)
                    if not _checked_path(source) or not _matches(source, record):
                        raise RuntimeError("Volume audit local copy changed. Keep the volume.")
            for record in missing_inputs:
                if not _matches(validate_input_source(record["local_path"]), record):
                    raise RuntimeError(
                        "Original local input changed during the audit. Keep the volume."
                    )

            remote_records = [
                {name: value for name, value in record.items() if name != "local_path"}
                for record in records
            ]
            inventory_bytes = json.dumps(
                remote_records, sort_keys=True, ensure_ascii=True, separators=(",", ":")
            ).encode()
            return {
                "job_id": job["job_id"],
                "volume_id": volume_id,
                "audited_utc": utc(),
                "mode": (
                    (
                        "unreleased_pod"
                        if allow_unreleased
                        else "released_unpublished"
                        if released_pod_id is not None
                        else "partial_outputs_copied"
                        if copy_all
                        else "pristine_inputs"
                    )
                    if allow_pristine_inputs
                    else "verified_backup"
                ),
                "object_count": len(records),
                "objects": records,
                "inputs_absent_from_volume": missing_inputs,
                "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
                "bundle_verified": bool(manifest and manifest.get("version") == 2),
                "control_backup_directory": str(snapshot) if snapshot is not None else None,
            }

    def _link_keys(self, bucket, prefix, job):
        """The keys the bundle's link safety index names. An S3 object under such a key may expose the
        link's target through the provider's bridge, so it is never read."""
        links = self.get_json(bucket, prefix + LINK_INDEX)
        if links is None:
            return set()
        if (
            not isinstance(links, dict)
            or set(links) != {"version", "job_id", "links"}
            or type(links["version"]) is not int
            or links["version"] != 1
            or links["job_id"] != job["job_id"]
            or not isinstance(links["links"], list)
            or any(
                not isinstance(name, str) or not name.startswith("artifacts/")
                for name in links["links"]
            )
        ):
            raise ValueError(
                "Malformed link safety index; retain source without exporting link targets."
            )
        return {prefix + name for name in links["links"]}

    def export_partial(self, job, bucket, destination):
        """Export accessible volume objects under opaque names. This never authorizes deletion."""
        prefix = _job_prefix(job)
        skip = self._link_keys(bucket, prefix, job)
        base = _destination(destination, create=True)
        export = Path(tempfile.mkdtemp(prefix="partial-export-", dir=base))
        objects = export / "objects"
        objects.mkdir()
        mapping = export / "objects.jsonl"

        saved, failed, listing_complete = 0, 0, False
        listing_error = None
        with mapping.open("x", encoding="utf-8", newline="\n") as journal:
            with tempfile.TemporaryDirectory(prefix=".export-stage-", dir=export) as staging:
                try:
                    for index, (key, size) in enumerate(self._objects(bucket)):
                        name = f"objects/{index:08d}.bin"
                        record = {
                            "key": key,
                            "local_path": name,
                            "listed_size": size,
                            "status": "pending",
                        }
                        _record_export(journal, record)
                        if key in skip:
                            failed += 1
                            _record_export(
                                journal,
                                {
                                    **record,
                                    "status": "skipped_symlink",
                                    "reason": (
                                        "link text is preserved in bundle metadata; "
                                        "never read target"
                                    ),
                                },
                            )
                            continue
                        staged = Path(staging) / str(index)
                        try:
                            observed = self._read_object(bucket, key, size, staged)
                            target = export / name
                            durable_replace(staged, target)
                            if not _checked_path(target) or not _matches(target, observed):
                                raise RuntimeError("Partial export local verification failed.")
                        except Exception as exc:
                            failed += 1
                            _record_export(
                                journal,
                                {**record, "status": "failed", "error_type": type(exc).__name__},
                            )
                            continue
                        saved += 1
                        _record_export(journal, {**record, **observed, "status": "saved"})
                    listing_complete = True
                except RuntimeError as exc:
                    listing_error = str(exc)
                    _record_export(journal, {"status": "listing_failed", "error": listing_error})

        _sync_directories(export, {"mapping": mapping, "objects": objects / "entry"})
        result = {
            "job_id": job["job_id"],
            "volume_id": bucket,
            "file_count": saved,
            "failed_count": failed,
            "listing_complete": listing_complete,
            "listing_error": listing_error,
            "download_directory": str(export),
            "mapping_file": str(mapping),
            "complete": False,
            "volume_deleted": False,
            "storage_retained": True,
        }
        save(export / "export-summary.json", result)
        return result
