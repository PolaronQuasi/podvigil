"""The credentials this process resolves, and keyed proofs of them for the watchdog's arming.

They come from one file, which settings.env_file finds: --env-file, <NAME>_ENV_FILE, the per-user file,
then the repository's .env. The account key comes from that file only; the S3 keys come from the
environment first, then from the file.
"""

import hashlib
import hmac
import os

from . import settings
from .provider.client import configured_api


def load_key():
    """Read the account key from the configured credentials file."""
    path = settings.env_file()
    values = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = raw.strip().partition("=")
        if separator and name.strip() == "RUNPOD_API_KEY":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values.append(value)
    if len(values) != 1 or not values[0] or any(c.isspace() for c in values[0]):
        raise ValueError(
            "The Runpod .env must contain one nonempty RUNPOD_API_KEY without whitespace."
        )
    return values[0]


def local_secrets():
    values = {}
    path = settings.env_file()
    try:
        contents = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        contents = ""
    for raw in contents.splitlines():
        name, sep, value = raw.strip().partition("=")
        if sep and name and not name.startswith("#"):
            values[name.strip()] = value.strip().strip("\"'")
    return {
        name: os.environ.get(name) or values.get(name)
        for name in ("RUNPOD_S3_ACCESS_KEY", "RUNPOD_S3_SECRET_KEY")
    }


def account_api(job):
    key = load_key()
    expected = job.get("auth_key_fingerprint")
    actual = (
        hmac.new(job["auth_fingerprint_salt"].encode(), key.encode(), hashlib.sha256).hexdigest()
        if job.get("auth_fingerprint_salt")
        else hashlib.sha256(key.encode()).hexdigest()
    )
    if expected and not hmac.compare_digest(actual, expected):
        raise RuntimeError(
            "Job credential changed; verify account identity before rebinding cleanup. "
            "Absence is not verified."
        )
    return configured_api(key)


def resolve_credentials():
    """The credentials this process would use, resolved exactly as launch, recover and the watchdog do.

    The account key comes from .env only; the S3 keys come from the environment first, then .env. A watchdog
    started through WMI on Windows does not inherit this shell's environment, so the two can differ.
    """
    try:
        api_key = load_key()
    except Exception:
        api_key = None
    try:
        s3 = local_secrets()
    except Exception:
        s3 = {}
    return {
        "RUNPOD_API_KEY": api_key or None,
        "RUNPOD_S3_ACCESS_KEY": s3.get("RUNPOD_S3_ACCESS_KEY") or None,
        "RUNPOD_S3_SECRET_KEY": s3.get("RUNPOD_S3_SECRET_KEY") or None,
    }


def credential_proof(salt):
    """Keyed hashes of this process's credentials, never the values; None marks a missing one."""
    return {
        name: (
            hmac.new(salt.encode(), value.encode(), hashlib.sha256).hexdigest() if value else None
        )
        for name, value in resolve_credentials().items()
    }
