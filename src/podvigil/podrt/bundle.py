"""Stdlib output snapshots. No source is removed and no link target is read.

Call finalize only after stopping every writer, including HashCache's worker.
The complete manifest is the last durable write. A cutoff leaves raw outputs and
possibly an uncommitted bundle for partial recovery; it never grants deletion.
publish copies a snapshot from the Pod's disk to the volume, manifest last.
"""

from pathlib import Path
from contextlib import contextmanager
import argparse
import hashlib
import json
import os
import re
import stat
import tarfile
import threading
import time
import unicodedata
import uuid

from .records import durable_replace, fsync_file, save, utc

BLOCK = 4 * 1024 * 1024
LIMIT = 16 * 1024 * 1024
MANIFEST = "artifact-manifest.json"
SHA = re.compile(r"[0-9a-f]{64}")


def _cutoff(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Output snapshot cutoff; retain raw outputs.")


def _link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def identity(info):
    # POSIX ctime detects same-size rewrites even if mtime is restored. Windows
    # ctime can mean birth time, so always rehash there.
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns if os.name != "nt" else 0,
        info.st_uid,
        info.st_gid,
    )


def _parts(name):
    if not isinstance(name, str) or "\0" in name:
        raise ValueError("Invalid snapshot path.")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Snapshot paths must be relative without traversal.")
    return parts


def _directory(path):
    path = Path(path).absolute()
    for parent in [*reversed(path.parents), path]:
        info = parent.lstat()
        if _link(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Unsafe snapshot directory.")
    return path


@contextmanager
def _open_file(root, name, expected=None):
    parts = _parts(name)
    root = _directory(root)
    descriptors = []
    try:
        if os.name == "posix":
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open(root, flags)
            descriptors.append(parent)
            for component in parts[:-1]:
                parent = os.open(component, flags, dir_fd=parent)
                descriptors.append(parent)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        else:
            _directory(root.joinpath(*parts[:-1]))
            path = root.joinpath(*parts)
            if _link(path.lstat()):
                raise ValueError("Linked source cannot be opened as content.")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or (
                expected is not None and identity(info) != expected
            ):
                raise RuntimeError("Output changed before content read.")
            yield stream
            if identity(os.fstat(stream.fileno())) != identity(info):
                raise RuntimeError("Output changed during content read.")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def inventory(output, deadline=None):
    """lstat every entry, without opening links, devices or external hardlinks."""
    output = _directory(output)
    root = output.lstat()
    result = {"": {"stat": root}}

    def visit(path, relative, fd=None):
        _cutoff(deadline)
        with os.scandir(fd if fd is not None else path) as scan:
            names = sorted(entry.name for entry in scan)
        for name in names:
            _cutoff(deadline)
            key = relative + name
            info = (
                os.stat(name, dir_fd=fd, follow_symlinks=False)
                if fd is not None
                else (path / name).lstat()
            )
            if info.st_dev != root.st_dev:
                raise ValueError("Separately mounted output cannot be snapshotted.")
            record = {"stat": info}
            result[key] = record
            if stat.S_ISLNK(info.st_mode):
                record["target"] = (
                    os.readlink(name, dir_fd=fd) if fd is not None else os.readlink(path / name)
                )
            elif _link(info):
                raise ValueError("Unsupported reparse point; retain source.")
            elif stat.S_ISDIR(info.st_mode):
                if fd is not None:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        if identity(os.fstat(child)) != identity(info):
                            raise RuntimeError("Output directory changed during inventory.")
                        visit(path / name, key + "/", child)
                    finally:
                        os.close(child)
                else:
                    visit(path / name, key + "/")
            elif not stat.S_ISREG(info.st_mode):
                raise ValueError("Special output file cannot be snapshotted; retain source.")

    fd = (
        os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if os.name == "posix"
        else None
    )
    try:
        visit(output, "", fd)
    finally:
        if fd is not None:
            os.close(fd)
    groups = {}
    for record in result.values():
        info = record["stat"]
        if stat.S_ISREG(info.st_mode):
            groups.setdefault((info.st_dev, info.st_ino), []).append(info)
    for group in groups.values():
        if any(info.st_nlink != len(group) for info in group):
            raise ValueError("Hardlink leaves the output tree; never read its target.")
    return result


def _fingerprint(tree):
    return {name: (identity(record["stat"]), record.get("target")) for name, record in tree.items()}


def _portable(tree):
    aliases = set()
    populated = {name.rpartition("/")[0] for name in tree if name}
    if stat.S_IMODE(tree[""]["stat"].st_mode) not in (0o700, 0o755, 0o777):
        return False
    for name, record in tree.items():
        if not name:
            continue
        info = record["stat"]
        if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            return False
        # Metadata preserves executable, special and non-default directory modes.
        # Ordinary private directories use the portable format.
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o7000 or (stat.S_ISREG(info.st_mode) and mode not in (0o600, 0o644, 0o666)):
            return False
        if stat.S_ISDIR(info.st_mode) and mode not in (0o700, 0o755, 0o777):
            return False
        if stat.S_ISDIR(info.st_mode) and name not in populated:
            return False  # Legacy manifests cannot represent an empty directory.
        folded = []
        for part in name.split("/"):
            if (
                part.endswith((" ", "."))
                or re.search(r'[\x00-\x1f<>:"\\|?*\ud800-\udfff]', part)
                or len(part.encode("utf-16-le")) > 510
            ):
                return False
            alias = unicodedata.normalize("NFKC", part).upper().casefold()
            stem = alias.split(".", 1)[0].rstrip(" ")
            if re.fullmatch(
                r"con|prn|aux|nul|clock\$|conin\$|conout\$|(?:com|lpt)[0-9]", stem
            ) or re.fullmatch(r"[^.]{1,6}~[0-9]+(?:\.[^.]{1,3})?", alias):
                return False
            folded.append(alias)
        key = "/".join(folded)
        if key in aliases:
            return False
        aliases.add(key)
    return True


class HashCache:
    """In-memory hints, revalidated by inode/size/mtime/ctime at finalization.

    No persisted hash is trusted. On Windows every final hash is recomputed.
    Applications can publish versioned checkpoints using CheckpointJournal;
    the background worker primes hashes while the workload is still running.
    """

    def __init__(self, output, cache_dir=None):
        self.output = Path(output).absolute()
        self.cache_dir = cache_dir  # Reserved: no disk cache is trusted or required.
        self._values = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.bytes_hashed = 0
        self.cache_hits = 0
        self.last_error = None

    def digest(self, name, expected, deadline=None, sync=False):
        with _open_file(self.output, name, expected) as stream:
            with self._lock:
                cached = self._values.get(name)
            if sync:
                if os.name == "nt":
                    fsync_file(self.output / name)
                else:
                    os.fsync(stream.fileno())
            if os.name == "posix" and cached and cached[0] == expected:
                self.cache_hits += 1
                return cached[1]
            checksum = hashlib.sha256()
            while True:
                _cutoff(deadline)
                if threading.current_thread() is self._thread and self._stop.is_set():
                    raise InterruptedError("Hash worker stopped.")
                chunk = stream.read(BLOCK)
                if not chunk:
                    break
                self.bytes_hashed += len(chunk)
                checksum.update(chunk)
            value = checksum.hexdigest()
        with self._lock:
            self._values[name] = (expected, value)
        return value

    def scan_once(self):
        tree = inventory(self.output)
        count = 0
        for name, record in tree.items():
            if stat.S_ISREG(record["stat"].st_mode):
                self.digest(name, identity(record["stat"]))
                count += 1
        with self._lock:
            self._values = {name: value for name, value in self._values.items() if name in tree}
        return count

    def start(self, interval=5):
        if interval <= 0 or self._thread is not None:
            raise ValueError("Hash worker already started or invalid interval.")

        def work():
            while not self._stop.is_set():
                try:
                    self.scan_once()
                    self.last_error = None
                except Exception as exc:
                    self.last_error = type(exc).__name__
                self._stop.wait(interval)

        self._thread = threading.Thread(target=work, name="output-hash-cache", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise RuntimeError("Hash worker did not quiesce before snapshot.")


def _secure_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700)
    _directory(path)
    if os.name == "posix" and path.stat().st_uid != os.geteuid():
        raise ValueError("Snapshot directory must belong to the supervisor.")
    return path


def _info(info):
    return {
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mtime_ns": info.st_mtime_ns,
    }


class _HashReader:
    def __init__(self, stream, deadline):
        self.stream, self.deadline = stream, deadline
        self.checksum = hashlib.sha256()

    def read(self, size=-1):
        _cutoff(self.deadline)
        data = self.stream.read(size)
        self.checksum.update(data)
        return data


class _HashWriter:
    def __init__(self, stream):
        self.stream = stream
        self.checksum = hashlib.sha256()
        self.size = 0

    def write(self, data):
        written = self.stream.write(data)
        self.checksum.update(data[:written])
        self.size += written
        return written

    def tell(self):
        return self.stream.tell()


def _file_entry(root, name, deadline=None):
    info = (root / name).lstat()
    with _open_file(root, name, identity(info)) as stream:
        reader = _HashReader(stream, deadline)
        while reader.read(BLOCK):
            pass
    return {"path": name, "size": info.st_size, "sha256": reader.checksum.hexdigest()}


def finalize(job, folder, cache=None, deadline=None, quiesced=False):
    """Publish legacy portable files or manifest v2 after a complete snapshot.

    Caller must stop *all* workload processes. No complete manifest survives a
    failed re-attestation. The absolute monotonic deadline never gets extended.
    """
    if quiesced is not True:
        raise ValueError("Quiesce all writers before finalization.")
    folder = _directory(folder)
    (folder / MANIFEST).unlink(missing_ok=True)
    _sync(folder)
    _cutoff(deadline)
    output = folder / "artifacts"
    tree = inventory(output, deadline)
    control = folder / "control"
    if not control.exists():
        _secure_directory(control)
    _directory(control)
    save(
        control / "output-links.json",
        {
            "version": 1,
            "job_id": job["job_id"],
            "links": ["artifacts/" + name for name, record in tree.items() if "target" in record],
        },
    )
    cache = cache or HashCache(output)
    if cache.output != output.absolute() or (
        cache._thread is not None and cache._thread.is_alive()
    ):
        raise ValueError("Wrong output hash cache or worker is not stopped.")
    for name in ("result.json", "train.log"):
        if name not in tree or not stat.S_ISREG(tree[name]["stat"].st_mode):
            raise RuntimeError("Missing regular result or log.")
    with _open_file(output, "result.json", identity(tree["result.json"]["stat"])) as stream:
        result = _json(stream)
    if not isinstance(result, dict) or result.get("job_id") != job["job_id"]:
        raise ValueError("Invalid result identity.")
    if result.get("outcome") == "success":
        for name in job.get("config", {}).get("required_artifacts", []):
            if name not in tree or not stat.S_ISREG(tree[name]["stat"].st_mode):
                raise RuntimeError("Missing required successful-job artifact.")
    manifest = {"job_id": job["job_id"], "complete": True, "created_utc": utc(), "files": []}
    if _portable(tree):
        for name, record in sorted(tree.items()):
            _cutoff(deadline)
            if stat.S_ISREG(record["stat"].st_mode):
                checksum = cache.digest(name, identity(record["stat"]), deadline, sync=True)
                manifest["files"].append(
                    {
                        "path": "artifacts/" + name,
                        "size": record["stat"].st_size,
                        "sha256": checksum,
                    }
                )
            else:
                _sync(output / name)
    else:
        parent = folder / "output-bundle"
        if not parent.exists():
            _secure_directory(parent)
        _directory(parent)
        if os.name == "posix" and (
            parent.stat().st_uid != os.geteuid() or stat.S_IMODE(parent.stat().st_mode) != 0o700
        ):
            raise ValueError("Bundle parent must be supervisor-owned and private.")
        snapshot_id = uuid.uuid4().hex
        snapshot = _secure_directory(parent / snapshot_id)
        metadata = {
            "version": 1,
            "job_id": job["job_id"],
            "snapshot_id": snapshot_id,
            "root": _info(tree[""]["stat"]),
            "entries": [],
        }
        groups = {}
        archive = snapshot / "archive.tar"
        with archive.open("xb") as raw:
            writer = _HashWriter(raw)
            with tarfile.open(fileobj=writer, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for name, record in sorted(tree.items()):
                    _cutoff(deadline)
                    if not name:
                        continue
                    info = record["stat"]
                    entry = {"path": name, **_info(info)}
                    if stat.S_ISDIR(info.st_mode):
                        entry["kind"] = "directory"
                    elif stat.S_ISLNK(info.st_mode):
                        entry.update(kind="symlink", target=record["target"])
                    else:
                        group = (info.st_dev, info.st_ino)
                        if group in groups:
                            original = groups[group]
                            entry.update(
                                kind="hardlink",
                                target=original["path"],
                                size=info.st_size,
                                sha256=original["sha256"],
                            )
                        else:
                            member = f"objects/{len(groups):08d}"
                            header = tarfile.TarInfo(member)
                            header.mode, header.size = 0o600, info.st_size
                            with _open_file(output, name, identity(info)) as stream:
                                reader = _HashReader(stream, deadline)
                                tar.addfile(header, reader)
                            entry.update(
                                kind="file",
                                member=member,
                                size=info.st_size,
                                sha256=reader.checksum.hexdigest(),
                            )
                            groups[group] = entry
                    metadata["entries"].append(entry)
            raw.flush()
            os.fsync(raw.fileno())
        save(snapshot / "metadata.json", metadata)
        for path in (archive, snapshot / "metadata.json"):
            os.chmod(path, 0o400)
            if os.name != "nt":
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        _sync(snapshot)
        _sync(parent)
        bundle = {
            name: f"output-bundle/{snapshot_id}/{filename}"
            for name, filename in (("archive", "archive.tar"), ("metadata", "metadata.json"))
        }
        manifest.update(version=2, bundle=bundle)
        manifest["files"].append(
            {"path": bundle["archive"], "size": writer.size, "sha256": writer.checksum.hexdigest()}
        )
        for name in ("artifacts/result.json", "artifacts/train.log", bundle["metadata"]):
            manifest["files"].append(_file_entry(folder, name, deadline))
    if _fingerprint(inventory(output, deadline)) != _fingerprint(tree):
        raise RuntimeError("Output tree changed during snapshot; retain source.")
    _cutoff(deadline)
    save(folder / MANIFEST, manifest)
    return manifest


def _sync(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _json(stream):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate snapshot metadata member.")
            value[key] = item
        return value

    raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("Snapshot metadata too large.")
    return json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Invalid number")),
    )


def _target_directory(root, parts):
    """The folder `parts` below `root`, created where missing, never through a link."""
    path = _directory(root)
    for part in parts:
        path = path / part
        try:
            path.mkdir()
        except FileExistsError:
            pass
        info = path.lstat()
        if _link(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Unsafe copy directory.")
    return path


def copy_file(
    source,
    name,
    destination,
    expected=None,
    *,
    deadline=None,
    check=None,
    replace=False,
    executable=False,
    source_identity=None,
):
    """Copy a regular file to the same name below `destination`; return its folder to sync.

    No link is followed. The bytes read must match `expected` (size, SHA-256) if given; the
    copy gets its name only when whole and flushed, and replaces a file only with `replace`.
    `executable` keeps an execute bit (0700 copy, else 0600).
    """
    parts = _parts(name)
    target = _target_directory(destination, parts[:-1])
    final = target / parts[-1]
    try:
        present = final.lstat()
    except FileNotFoundError:
        pass
    else:
        if not replace or _link(present) or not stat.S_ISREG(present.st_mode):
            raise RuntimeError("Copy destination already exists; nothing is replaced.")
    temporary = target / (".wondomon-" + uuid.uuid4().hex + ".tmp")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    checksum, size = hashlib.sha256(), 0
    try:
        with _open_file(source, name, source_identity) as stream:
            mode = 0o700 if executable and os.fstat(stream.fileno()).st_mode & 0o111 else 0o600
            with open(os.open(temporary, flags, mode), "wb") as output:
                while True:
                    _cutoff(deadline)
                    if check is not None:
                        check()
                    chunk = stream.read(BLOCK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if expected is not None and size > expected[0]:
                        raise RuntimeError("Copy source is larger than recorded.")
                    checksum.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        # _open_file has checked that the source did not change while read.
        if expected is not None and (size, checksum.hexdigest()) != tuple(expected):
            raise RuntimeError("Copy source differs from its recorded size and SHA-256.")
        _cutoff(deadline)
        os.replace(temporary, final)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target


def publish(job, folder, volume, manifest, deadline=None, replace=()):
    """Copy a complete local snapshot to the job's folder on the volume, the manifest last.

    The controller then finds the keys it read when the workload wrote to the volume itself:
    each manifest file, for a bundle also each original file below artifacts/ (no links),
    control/output-links.json and the manifest bytes. Each copy is checked against its hash;
    only `replace` names may exist. A cutoff or failure leaves no manifest: the volume is kept.
    """
    folder, volume = _directory(folder), _directory(volume)
    if (
        not isinstance(manifest, dict)
        or manifest.get("job_id") != job["job_id"]
        or manifest.get("complete") is not True
    ):
        raise ValueError("Only a complete manifest of this job is published.")
    with _open_file(folder, MANIFEST) as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT or json.loads(raw) != manifest:
        raise RuntimeError("The local manifest differs from the snapshot taken.")
    try:
        (volume / MANIFEST).lstat()
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError("The volume already holds a manifest for this job.")
    wanted = {item["path"]: (item["size"], item["sha256"]) for item in manifest["files"]}
    folders = []
    if manifest.get("version") == 2:
        with _open_file(folder, manifest["bundle"]["metadata"]) as stream:
            entries = validate_metadata(_json(stream), job["job_id"])
        for name, entry in entries.items():
            if entry["kind"] == "directory":
                folders.append(_parts("artifacts/" + name))
            elif entry["kind"] in ("file", "hardlink"):
                value = (entry["size"], entry["sha256"])
                if wanted.setdefault("artifacts/" + name, value) != value:
                    raise RuntimeError("Bundle metadata and manifest disagree.")
    first = [name for name in ("artifacts/result.json", "artifacts/train.log") if name in wanted]
    touched = {volume}
    for parts in sorted(folders, key=len):
        touched.add(_target_directory(volume, parts))
    for name in first + sorted(set(wanted) - set(first)):
        touched.add(
            copy_file(
                folder, name, volume, wanted[name], deadline=deadline, replace=name in replace
            )
        )
    touched.add(copy_file(folder, "control/output-links.json", volume, deadline=deadline))
    synced = set()
    for path in touched:
        while path not in synced and (path == volume or volume in path.parents):
            synced.add(path)
            path = path.parent
    for path in sorted(synced, key=lambda path: len(path.parts), reverse=True):
        _sync(path)
    _cutoff(deadline)
    copy_file(
        folder, MANIFEST, volume, (len(raw), hashlib.sha256(raw).hexdigest()), deadline=deadline
    )
    _sync(volume)
    return {"files": len(wanted), "bytes": sum(size for size, _ in wanted.values())}


def _salvage_files(output, deadline):
    """Independently checked files, skipping every link and unsafe/unreadable subtree."""
    output = _directory(output)
    root, files = output.lstat(), {}

    def visit(path, relative, fd=None):
        _cutoff(deadline)
        with os.scandir(fd if fd is not None else path) as scan:
            names = sorted(entry.name for entry in scan)
        for name in names:
            _cutoff(deadline)
            key = relative + name
            try:
                info = (
                    os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if fd is not None
                    else (path / name).lstat()
                )
                if _link(info) or info.st_dev != root.st_dev:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if fd is None:
                        visit(_directory(path / name), key + "/")
                    else:
                        child = os.open(
                            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                        )
                        try:
                            if identity(os.fstat(child)) == identity(info):
                                visit(path / name, key + "/", child)
                        finally:
                            os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    files[key] = info
            except TimeoutError:
                raise
            except (OSError, ValueError):
                continue

    fd = (
        os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if os.name == "posix"
        else None
    )
    try:
        if fd is None or identity(os.fstat(fd)) == identity(root):
            visit(output, "", fd)
    finally:
        if fd is not None:
            os.close(fd)
    return files


def salvage(folder, volume, deadline=None, replace=()):
    """After a failed snapshot copy the raw regular files that fit (no manifest); return the count.

    For partial recovery only, after writers stop. All links (including hardlinks) and special
    files are skipped individually; only `replace` names are replaced. Stops at the deadline.
    """
    names = ["result.json", "train.log"]
    try:
        files = _salvage_files(Path(folder) / "artifacts", deadline)
    except Exception:
        files = {}
    names += sorted(name for name in files if name not in names)
    copied, touched = 0, set()
    for name in names:
        try:
            info = files.get(name)
            if info is None:
                info = (Path(folder) / "artifacts" / name).lstat()
            if _link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                continue
            touched.add(
                copy_file(
                    folder,
                    "artifacts/" + name,
                    volume,
                    deadline=deadline,
                    replace="artifacts/" + name in replace,
                    source_identity=identity(info),
                )
            )
        except TimeoutError:
            break
        except Exception:
            continue
        copied += 1
    for path in touched:
        try:
            _sync(path)
        except OSError:
            pass
    return copied


def validate_metadata(metadata, job_id=None):
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"version", "job_id", "snapshot_id", "root", "entries"}
        or type(metadata["version"]) is not int
        or metadata["version"] != 1
        or not isinstance(metadata["job_id"], str)
        or (job_id is not None and metadata["job_id"] != job_id)
        or not isinstance(metadata["snapshot_id"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", metadata["snapshot_id"])
        or not isinstance(metadata["entries"], list)
    ):
        raise ValueError("Invalid bundle metadata identity/schema.")
    base = {"mode", "uid", "gid", "mtime_ns"}

    def attributes(record):
        if (
            any(type(record.get(key)) is not int for key in base)
            or not 0 <= record["mode"] <= 0o7777
        ):
            raise ValueError("Invalid filesystem metadata.")
        if record["uid"] < 0 or record["gid"] < 0:
            raise ValueError("Invalid ownership metadata.")

    if not isinstance(metadata["root"], dict) or set(metadata["root"]) != base:
        raise ValueError("Invalid root metadata.")
    attributes(metadata["root"])
    entries, members = {}, set()
    extras = {
        "directory": set(),
        "symlink": {"target"},
        "file": {"member", "size", "sha256"},
        "hardlink": {"target", "size", "sha256"},
    }
    for item in metadata["entries"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("kind"), str)
            or item["kind"] not in extras
        ):
            raise ValueError("Invalid bundle entry type.")
        kind = item["kind"]
        if set(item) != base | {"path", "kind"} | extras[kind]:
            raise ValueError("Invalid bundle entry schema.")
        parts = _parts(item["path"])
        if item["path"] in entries:
            raise ValueError("Duplicate bundle path.")
        attributes(item)
        if kind in ("file", "hardlink"):
            if (
                type(item["size"]) is not int
                or item["size"] < 0
                or not isinstance(item["sha256"], str)
                or not SHA.fullmatch(item["sha256"])
            ):
                raise ValueError("Invalid bundle content checksum.")
        if kind == "file":
            member = item["member"]
            if (
                not isinstance(member, str)
                or not re.fullmatch(r"objects/[0-9]{8}", member)
                or member in members
            ):
                raise ValueError("Invalid/duplicate opaque archive member.")
            members.add(member)
        if kind in ("symlink", "hardlink") and (
            not isinstance(item["target"], str) or not item["target"] or "\0" in item["target"]
        ):
            raise ValueError("Invalid link text.")
        entries[item["path"]] = item
    for name, item in entries.items():
        parts = _parts(name)
        for index in range(1, len(parts)):
            parent = entries.get("/".join(parts[:index]))
            if parent is None or parent["kind"] != "directory":
                raise ValueError("Bundle path crosses a missing directory or link.")
        if item["kind"] == "hardlink":
            original = entries.get(item["target"])
            if (
                original is None
                or original["kind"] != "file"
                or any(item[key] != original[key] for key in base | {"size", "sha256"})
            ):
                raise ValueError("Invalid hardlink identity/content.")
    return entries


def verify_bundle(archive, metadata, job_id=None, sink=None):
    """Verify opaque tar contents without extraction or following stored links."""
    if not isinstance(metadata, dict):
        with _open_file(Path(metadata).parent, Path(metadata).name) as stream:
            metadata = _json(stream)
    entries = validate_metadata(metadata, job_id)
    expected = {item["member"]: item for item in entries.values() if item["kind"] == "file"}
    seen = set()
    with _open_file(Path(archive).parent, Path(archive).name) as raw:
        with tarfile.open(fileobj=raw, mode="r|") as tar:
            for member in tar:
                item = expected.get(member.name)
                if (
                    item is None
                    or member.name in seen
                    or not member.isreg()
                    or member.linkname
                    or member.size != item["size"]
                    or member.sparse is not None
                    or (member.pax_headers and member.pax_headers != {"size": str(item["size"])})
                ):
                    raise ValueError("Unexpected or unsafe archive member.")
                seen.add(member.name)
                checksum, count = hashlib.sha256(), 0
                with tar.extractfile(member) as stream:
                    for chunk in iter(lambda: stream.read(BLOCK), b""):
                        checksum.update(chunk)
                        count += len(chunk)
                        if sink is not None:
                            sink(member.name, chunk)
                if count != item["size"] or checksum.hexdigest() != item["sha256"]:
                    raise ValueError("Archive member checksum mismatch.")
                if sink is not None:
                    sink(member.name, None)
    if seen != expected.keys():
        raise ValueError("Archive lacks metadata content.")
    return metadata


def _contained_link(name, target, entries):
    """Check actual traversal order: a link or file cannot be erased by a later '..'."""
    if target.startswith("/"):
        return False
    stack = _parts(name)[:-1]
    for part in target.split("/"):
        if stack and entries["/".join(stack)]["kind"] != "directory":
            return False
        if part == "..":
            if not stack:
                return False
            stack.pop()
        elif part not in ("", "."):
            stack.append(part)
            node = entries.get("/".join(stack))
            if node is None or node["kind"] not in ("file", "hardlink", "directory"):
                return False
    return bool(stack)


def extract_bundle(archive, metadata, destination, restore_links=False):
    """Recover into a NEW directory. Windows/default uses opaque files + mapping.

    POSIX restore_links=True restores relative names, modes and hardlinks. Only
    symlinks whose lexical target stays inside the tree are materialized; all
    link text remains in metadata. Never restore ownership or setuid/setgid bits.
    """
    metadata = verify_bundle(archive, metadata)
    destination = Path(destination).absolute()
    _directory(destination.parent)
    _secure_directory(destination)  # No overwrite, merge or symlink escape.
    objects = _secure_directory(destination / "objects")
    streams = {}

    def sink(name, chunk):
        stream = streams.get(name)
        if stream is None:
            stream = (objects / name.split("/")[-1]).open("xb")
            streams[name] = stream
        if chunk is None:
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
        else:
            stream.write(chunk)

    try:
        verify_bundle(archive, metadata, sink=sink)
    finally:
        for stream in streams.values():
            stream.close()
    save(destination / "mapping.json", metadata)
    if restore_links and os.name == "posix":
        tree = _secure_directory(destination / "tree")
        entries = validate_metadata(metadata)
        for name, item in sorted(entries.items(), key=lambda pair: (len(_parts(pair[0])), pair[0])):
            if item["kind"] == "directory":
                (tree / name).mkdir(mode=0o700)
        for name, item in entries.items():
            if item["kind"] in ("file", "hardlink"):
                original = item if item["kind"] == "file" else entries[item["target"]]
                os.link(objects / original["member"].split("/")[-1], tree / name)
        for name, item in entries.items():
            if item["kind"] == "symlink":
                target = item["target"]
                if _contained_link(name, target, entries):
                    os.symlink(target, tree / name)
        for name, item in sorted(
            entries.items(), key=lambda pair: len(_parts(pair[0])), reverse=True
        ):
            if item["kind"] != "symlink":
                path = tree / name
                os.utime(path, ns=(item["mtime_ns"], item["mtime_ns"]), follow_symlinks=False)
                os.chmod(path, item["mode"] & 0o777)
                if item["kind"] == "directory":
                    _sync(path)
        os.chmod(tree, metadata["root"]["mode"] & 0o777)
        os.utime(tree, ns=(metadata["root"]["mtime_ns"], metadata["root"]["mtime_ns"]))
        _sync(tree)
    _sync(objects)
    _sync(destination)
    _sync(destination.parent)
    return {
        "directory": str(destination),
        "mapping_file": str(destination / "mapping.json"),
        "restored_tree": bool(restore_links and os.name == "posix"),
    }


class CheckpointJournal:
    """Application API: publish unique durable checkpoints, never overwrite one.

    Use a directory inside artifacts/. Readers use the returned path only after
    publish succeeds. A readonly mode is accidental-write protection, not an
    isolation boundary; finalization always revalidates the inode signature.
    Package this module and its standard-library-only records module with the
    application if used by an isolated workload. Do not expose the
    supervisor's private code directory to make this optional API importable.
    """

    def __init__(self, directory):
        directory = Path(directory).absolute()
        if not directory.exists():
            _directory(directory.parent)
            _secure_directory(directory)
        self.directory = _directory(directory)

    def publish(self, source, deadline=None):
        source = Path(source).absolute()
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or _link(info) or info.st_nlink != 1:
            raise ValueError("Checkpoint source must be a private regular file.")
        token = uuid.uuid4().hex
        staged = self.directory / (token + ".partial")
        published = self.directory / (token + ".bin")
        with (
            _open_file(source.parent, source.name, identity(info)) as stream,
            staged.open("xb") as output,
        ):
            reader = _HashReader(stream, deadline)
            for chunk in iter(lambda: reader.read(BLOCK), b""):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        _cutoff(deadline)
        durable_replace(staged, published)
        os.chmod(published, 0o400)
        save(
            self.directory / (token + ".json"),
            {
                "version": 1,
                "path": published.name,
                "size": info.st_size,
                "sha256": reader.checksum.hexdigest(),
            },
        )
        _sync(self.directory)
        return published


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify/recover an output bundle into a new directory."
    )
    parser.add_argument("archive")
    parser.add_argument("metadata")
    parser.add_argument("destination")
    parser.add_argument(
        "--restore-links", action="store_true", help="POSIX only; external links remain metadata"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            extract_bundle(args.archive, args.metadata, args.destination, args.restore_links)
        )
    )
