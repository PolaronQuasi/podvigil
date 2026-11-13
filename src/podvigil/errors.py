"""What an error may say: own messages in full and redacted, other types by name and provider code only."""

import re

from .podrt.api import ApiError, _redact


# Exception types whose messages this code writes itself; any other type is reported by name only.
OWN_ERRORS = (ApiError, RuntimeError, ValueError, TimeoutError, PermissionError, FileNotFoundError)


def public_error(exc, secrets=()):
    """Type, redacted message and provider code of an error. Never a credential, a request or a response body.

    Called inside exception handlers of the cleanup paths, so it never raises.
    """
    try:
        return _public_error(exc, secrets)
    except Exception:
        return {"error_type": type(exc).__name__}


def _public_error(exc, secrets):
    record = {"error_type": type(exc).__name__}
    if isinstance(exc, OWN_ERRORS):
        record["message"] = _redact(str(exc), [s for s in secrets if s], 1000)
    if isinstance(exc, ApiError):
        record.update(http_status=exc.status, method=exc.method, path=exc.path)
    response = getattr(exc, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", code):
        record["provider_code"] = code  # an S3 error code such as AccessDenied
    cause = (
        exc.__cause__
        if exc.__cause__ is not None
        else (None if exc.__suppress_context__ else exc.__context__)
    )
    if cause is not None and cause is not exc:
        record["cause"] = {"error_type": type(cause).__name__}
        if isinstance(cause, OWN_ERRORS):
            record["cause"]["message"] = _redact(str(cause), [s for s in secrets if s], 1000)
    return record


def public_message(exc):
    """One line for the terminal: own messages in full, other types by name."""
    return str(exc) if isinstance(exc, OWN_ERRORS) else type(exc).__name__
