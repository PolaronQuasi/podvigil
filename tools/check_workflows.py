"""Run pinned actionlint after checking its release archive against a fixed SHA-256."""

from pathlib import Path
import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.7.12"
ARCHIVES = {
    "nt": ("windows_amd64.zip", "6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9"),
    "posix": (
        "linux_amd64.tar.gz",
        "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
    ),
}


def main():
    suffix, expected = ARCHIVES[os.name]
    url = (
        "https://github.com/rhysd/actionlint/releases/download/v"
        + VERSION
        + "/actionlint_"
        + VERSION
        + "_"
        + suffix
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("actionlint archive checksum mismatch")
    name = "actionlint.exe" if os.name == "nt" else "actionlint"
    with tempfile.TemporaryDirectory(prefix="podvigil-actionlint-") as tmp:
        folder = Path(tmp).resolve()
        assert folder.parent == Path(tempfile.gettempdir()).resolve()
        binary = folder / name
        if suffix.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                binary.write_bytes(archive.read(name))
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                binary.write_bytes(archive.extractfile(name).read())
        binary.chmod(0o700)
        workflows = sorted(str(p) for p in (ROOT / ".github/workflows").glob("*.yml"))
        if not workflows:
            raise RuntimeError("No GitHub workflows found")
        subprocess.run(
            [str(binary), "-shellcheck=", "-pyflakes=", *workflows], cwd=ROOT, check=True
        )
    print("GitHub workflow syntax and expressions passed actionlint " + VERSION)


if __name__ == "__main__":
    main()
