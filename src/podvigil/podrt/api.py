"""REST v2 client with bounded, redacted diagnostics and no implicit POST retry."""

import datetime
from contextlib import contextmanager
import email.utils
import hashlib
import hmac
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import weakref


LIST_PAGE_LIMIT = 100
INVALID_LIST = "Malformed list response; refusing an incomplete resource inventory."
ERROR_BODY_LIMIT = 16384
RATE_HEADER_LIMIT = 8192
MAX_WINDOWS = 16


def _header(headers, name):
    values = (
        headers.get_all(name)
        if hasattr(headers, "get_all")
        else [value for key, value in headers.items() if key.lower() == name.lower()]
    )
    if not values:
        return None
    if any(not isinstance(value, str) for value in values):
        return "\n"  # rejected without retaining the malformed value
    return ", ".join(values)


def _sf_parts(value, separator):
    """Split the supported structured-field subset without splitting quoted strings."""
    if (
        not isinstance(value, str)
        or len(value) > RATE_HEADER_LIMIT
        or any(ord(c) < 32 or ord(c) > 126 for c in value)
    ):
        raise ValueError("Invalid quota header")
    parts, start, quoted, escaped = [], 0, False, False
    for i, char in enumerate(value):
        if escaped:
            if char not in ('"', "\\"):
                raise ValueError("Invalid quoted escape")
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and char == separator:
            parts.append(value[start:i].strip())
            start = i + 1
    parts.append(value[start:].strip())
    if quoted or escaped or not all(parts) or len(parts) > 64:
        raise ValueError("Invalid quota members")
    return parts


def _integer(value, positive=False):
    if not re.fullmatch(r"[0-9]{1,15}", value):
        raise ValueError("Invalid quota number")
    number = int(value)
    if positive and number == 0:
        raise ValueError("Invalid zero quota window")
    return number


def _parameters(parts):
    result = {}
    for part in parts:
        key, equals, value = part.partition("=")
        if not re.fullmatch(r"[a-z*][a-z0-9_.*-]*", key) or key in result:
            raise ValueError("Duplicate or invalid quota parameter")
        if equals and not (
            re.fullmatch(
                r"-?[0-9]{1,15}(?:\.[0-9]{1,3})?|\?[01]|[A-Za-z*][A-Za-z0-9_.*:/-]*", value
            )
            or re.fullmatch(r'"(?:[^"\\]|\\["\\])*"', value)
        ):
            raise ValueError("Invalid quota parameter value")
        result[key] = value if equals else "?1"
    return result


def parse_rate_limits(headers, now=None):
    """Numeric-only quota metadata. Unknown policy labels/extensions never escape.

    Runpod v2: \"minute\";r=0;t=12 paired with \"minute\";q=180;w=60.
    Also accepts the older dictionary and separate Limit/Remaining/Reset fields.
    Standard Reset is delta-seconds; only X-RateLimit-Reset is an epoch.
    """
    live, policy = _header(headers, "RateLimit"), _header(headers, "RateLimit-Policy")
    result = {"windows": [], "policies": [], "malformed": False}
    policies = {}
    if policy is not None:
        try:
            for member in _sf_parts(policy, ","):
                parts = _sf_parts(member, ";")
                params = _parameters(parts[1:])
                label = parts[0]
                if label.startswith('"'):
                    label = json.loads(label)
                    quota = _integer(params["q"])
                else:
                    quota = _integer(label)
                    label = "legacy:" + str(quota) + ":" + params["w"]
                window = _integer(params["w"], positive=True)
                if label in policies or len(policies) >= MAX_WINDOWS:
                    raise ValueError("Repeated quota policy")
                policies[label] = {"limit": quota, "window_seconds": window}
            result["policies"] = sorted(
                policies.values(), key=lambda p: (p["window_seconds"], p["limit"])
            )
        except (ValueError, KeyError, TypeError):
            policies = {}
            result["malformed"] = True
    try:
        if live is not None and live.lstrip().startswith('"'):
            windows = {}
            for member in _sf_parts(live, ","):
                parts = _sf_parts(member, ";")
                label, params = json.loads(parts[0]), _parameters(parts[1:])
                if not isinstance(label, str) or label in windows or len(windows) >= MAX_WINDOWS:
                    raise ValueError("Repeated quota window")
                item = {
                    "remaining": _integer(params["r"]),
                    "reset_after": _integer(params["t"], positive=True),
                    **policies.get(label, {}),
                }
                if item.get("limit", item["remaining"]) < item["remaining"]:
                    raise ValueError("Inconsistent quota")
                windows[label] = item
            # Stable order without publishing arbitrary provider-supplied labels.
            result["windows"] = [windows[label] for label in sorted(windows)]
        elif live is not None:
            fields = _parameters(_sf_parts(live, ","))
            item = {
                "limit": _integer(fields["limit"]),
                "remaining": _integer(fields["remaining"]),
                "reset_after": _integer(fields["reset"]),
            }
            matching = [p for p in policies.values() if p["limit"] == item["limit"]]
            if len(matching) == 1:
                item.update(matching[0])
            if item["remaining"] > item["limit"]:
                raise ValueError("Inconsistent quota")
            result["windows"] = [item]
        else:
            for prefix in ("RateLimit-", "X-RateLimit-"):
                values = [
                    _header(headers, prefix + name) for name in ("Limit", "Remaining", "Reset")
                ]
                if not any(v is not None for v in values):
                    continue
                limit, remaining, reset = [_integer(v.strip()) for v in values]
                if remaining > limit:
                    raise ValueError("Inconsistent quota")
                if prefix.startswith("X-"):
                    reset = max(0, reset - (time.time() if now is None else now))
                result["windows"] = [{"limit": limit, "remaining": remaining, "reset_after": reset}]
                break
    except (ValueError, KeyError, TypeError, AttributeError):
        result["malformed"] = True
        result["windows"] = []
    return result


def _audit_identity(entry):
    """Audit rows have no `id`; never relax the resource-list ID contract."""
    for field in ("actorId", "email", "value"):
        if field not in entry or entry[field] is not None and not isinstance(entry[field], str):
            raise RuntimeError(INVALID_LIST)
    for field in ("resourceType", "resourceId", "timestamp", "action"):
        if not isinstance(entry.get(field), str) or not entry[field] and field != "resourceId":
            raise RuntimeError(INVALID_LIST)
    try:
        stamp = datetime.datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Timestamp requires timezone")
    except ValueError:
        raise RuntimeError(INVALID_LIST) from None
    return entry["resourceType"], entry["resourceId"], stamp, entry["action"]


def _http_date(value):
    try:
        if not isinstance(value, str) or len(value) > 128:
            return None
        date = email.utils.parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        date = date.astimezone(datetime.timezone.utc)
        return date.timestamp(), email.utils.format_datetime(date, usegmt=True)
    except (ValueError, TypeError, OverflowError):
        return None


def _retry_after(value, now):
    if not isinstance(value, str) or len(value) > 128:
        return None
    if re.fullmatch(r"[0-9]+", value.strip()):
        seconds = float(value)
    else:
        date = _http_date(value)
        if date is None:
            return None
        seconds = max(0, date[0] - now)
    return seconds if math.isfinite(seconds) else None


def _secrets(body):
    """Only request values under credential/environment fields, never local env."""
    if not isinstance(body, dict):
        return []
    result = []
    for key, value in body.items():
        sensitive = re.search(
            r"env|key|token|secret|password|credential|authorization", str(key), re.I
        )
        if sensitive:

            def strings(item):
                if isinstance(item, str) and item:
                    result.append(item)
                elif isinstance(item, dict):
                    for child in item.values():
                        strings(child)
                elif isinstance(item, list):
                    for child in item:
                        strings(child)

            strings(value)
        elif isinstance(value, dict):
            result.extend(_secrets(value))
        elif isinstance(value, list):
            for child in value:
                result.extend(_secrets(child))
    return result


def _redact(value, secrets=(), limit=512):
    if not isinstance(value, str):
        return None
    # Redact before truncation, including a credential crossing the output bound.
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            for form in (secret, json.dumps(secret)[1:-1], urllib.parse.quote(secret, safe="")):
                value = value.replace(form, "[redacted]")
    value = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[^\s,;\"']+", "[redacted authorization]", value)
    value = re.sub(
        r"(?i)(?:[\w-]*(?:key|token|secret|password|credential|authorization)[\w-]*)"
        r"[\"']?\s*[:=]\s*(?:\"(?:\\.|[^\"\\])*\"|'[^']*'|[^\s,;&}]+)",
        "[redacted credential]",
        value,
    )
    value = re.sub(
        r"(?is)\benv(?:ironment(?:_variables)?)?[\"']?\s*[:=].*", "[redacted environment]", value
    )
    value = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", value)
    # Opaque tokens can be echoed without a label or without being in this request.
    value = re.sub(r"\b[A-Za-z0-9_-]{20,}\b", "[redacted opaque token]", value)
    value = " ".join(value.split())
    return value[:limit] + ("..." if len(value) > limit else "")


class ApiError(RuntimeError):
    def __init__(
        self,
        status,
        method,
        path,
        *,
        not_sent=False,
        retry_after=None,
        title=None,
        detail=None,
        response_date=None,
        secrets=(),
        rate_limits=None,
    ):
        self.status = status
        self.method = method
        self.path = _redact(path.split("?", 1)[0], secrets, 256)
        self.not_sent = bool(not_sent) if not status else False
        self.retry_after = (
            float(retry_after)
            if isinstance(retry_after, (int, float))
            and not isinstance(retry_after, bool)
            and retry_after >= 0
            and math.isfinite(retry_after)
            else None
        )
        self.title, self.detail = _redact(title, secrets, 160), _redact(detail, secrets)
        date = _http_date(response_date)
        self.response_date = date[1] if date else None
        self.rate_limits = rate_limits
        message = f"Runpod {method} {self.path}: HTTP {status or 'network failure'}"
        if self.not_sent:
            message += " (request not sent)"
        diagnostics = "; ".join(value for value in (self.title, self.detail) if value)
        super().__init__(message + (": " + diagnostics if diagnostics else ""))


class ApiPacingDeferred(ApiError):
    """No long sleeps in legacy retry helpers; required_wait is the real cooldown. `cause` names the error
    that made the shared pacing file unusable, when that is why. (Titles are cut at 160 characters.)"""

    def __init__(
        self, method, path, wait, *, not_sent=True, status=0, rate_limits=None, cause=None
    ):
        self.required_wait = max(0.0, float(wait))
        self.cause = cause
        title = "API pacing deferred; caller must retain supervision"
        if cause == "TimeoutError":
            title = "API pacing deferred: another process held the pacing file; try again"
        elif cause:
            title = (
                "API pacing deferred: this key's pacing file in the state folder is unusable ("
                + cause
                + "); when no job runs, delete the api-rate-limit files there"
            )
        super().__init__(
            status,
            method,
            path,
            not_sent=not_sent,
            retry_after=0,
            title=title,
            rate_limits=rate_limits,
        )


_PACERS = weakref.WeakValueDictionary()
_PACERS_LOCK = threading.Lock()


class _QuotaPacer:
    """Conservative reservations across threads and optionally local processes.

    A short file-lock transaction never covers HTTP or sleep. Failure to share
    state defers ordinary traffic; cleanup may use the process-local state.
    """

    def __init__(self, binding):
        self.binding, self.lock, self.path = binding, threading.RLock(), None
        self.state = {"windows": [], "blocked_until": 0.0, "next_read": 0.0, "sequence": 0}
        self.coordination = "process"

    def _load(self, path, tick):
        try:
            with path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("Oversized pacing state")
            value = json.loads(raw)
        except FileNotFoundError:
            return None
        # Treat valid JSON of the wrong shape as damaged. Raise ValueError so
        # cleanup can fall back to this process's own state.
        if not isinstance(value, dict) or not isinstance(value.get("salt", ""), str):
            raise ValueError("Invalid pacing state")
        salt = value.get("salt", "")
        if not re.fullmatch(r"[0-9a-f]{32}", salt) or not hmac.compare_digest(
            hmac.new(self.binding, salt.encode(), hashlib.sha256).hexdigest(),
            value.get("binding", ""),
        ):
            raise ValueError("Pacing state belongs to a different credential/origin")
        state = value["state"]
        if (
            not isinstance(state, dict)
            or set(state) != {"windows", "blocked_until", "next_read", "sequence"}
            or type(state["sequence"]) is not int
            or state["sequence"] < 0
            or not isinstance(state["windows"], list)
            or len(state["windows"]) > MAX_WINDOWS
        ):
            raise ValueError("Invalid pacing state")
        for item in state["windows"]:
            if (
                not isinstance(item, dict)
                or set(item) - {"remaining", "reset_at", "limit", "window_seconds"}
                or not {"remaining", "reset_at"} <= set(item)
            ):
                raise ValueError("Invalid pacing window")
        numbers = [state["blocked_until"], state["next_read"], value["monotonic"], value["wall"]]
        numbers += [n for item in state["windows"] for n in item.values()]
        if any(type(n) not in (int, float) or not math.isfinite(n) or n < 0 for n in numbers):
            raise ValueError("Invalid pacing numbers")
        # Monotonic values are shared by processes on this host, not across boots.
        if (
            tick < value["monotonic"]
            or abs((time.time() - value["wall"]) - (tick - value["monotonic"])) > 5
        ):
            return None
        return value

    def transaction(self, function, cleanup=False):
        with self.lock:
            tick = time.monotonic()
            if self.path is None:
                return function(self.state, tick)
            try:
                # Lazy import avoids the records -> ApiError module cycle.
                from .records import exclusive_lock

                with exclusive_lock(str(self.path) + ".lock", timeout=0.1):
                    tick = time.monotonic()  # another writer may have committed while we waited
                    loaded = self._load(self.path, tick)
                    state = loaded["state"] if loaded else self.state
                    result = function(state, tick)
                    salt = loaded["salt"] if loaded else os.urandom(16).hex()
                    value = {
                        "salt": salt,
                        "binding": hmac.new(
                            self.binding, salt.encode(), hashlib.sha256
                        ).hexdigest(),
                        "monotonic": tick,
                        "wall": time.time(),
                        "state": state,
                    }
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor, temporary = tempfile.mkstemp(
                        prefix=".pacing-", dir=self.path.parent
                    )
                    try:
                        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                            json.dump(value, stream, allow_nan=False)
                        os.replace(temporary, self.path)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
                    self.state, self.coordination = state, "shared"
                    return result
            except (OSError, ValueError, KeyError, TypeError, AttributeError, TimeoutError) as exc:
                self.coordination = "local_fallback" if cleanup else "unavailable"
                if cleanup:
                    return function(self.state, tick)
                raise ApiPacingDeferred("GET", "/", 0.1, cause=type(exc).__name__) from None

    def reserve(self, cleanup):
        def update(state, tick):
            state["windows"] = [w for w in state["windows"] if w["reset_at"] > tick]
            delay = max(0, state["blocked_until"] - tick)
            if not cleanup:
                delay = max(delay, state["next_read"] - tick)
            for window in state["windows"]:
                reserve = 0 if cleanup else min(2, max(0, window.get("limit", 3) - 1))
                if window["remaining"] <= reserve:
                    delay = max(delay, window["reset_at"] - tick)
            if delay > 0:
                return delay, None
            state["sequence"] += 1
            for window in state["windows"]:
                window["remaining"] = max(0, window["remaining"] - 1)
            if not cleanup and state["windows"]:
                # Pace ordinary observations within quota. Reserve the last two for teardown.
                interval = max(
                    (w["reset_at"] - tick) / max(1, w["remaining"] - 2) for w in state["windows"]
                )
                state["next_read"] = tick + interval
            return 0, state["sequence"]

        return self.transaction(update, cleanup)

    def observe(self, budget, retry_after, status, ticket=None):
        def update(state, tick):
            old = state["windows"]
            observed = []
            for index, entry in enumerate(budget["windows"]):
                item = {k: entry[k] for k in ("remaining", "limit", "window_seconds") if k in entry}
                item["reset_at"] = tick + entry["reset_after"]
                # Responses arriving late may not include subsequent reservations.
                if ticket is not None:
                    item["remaining"] = max(
                        0, item["remaining"] - max(0, state["sequence"] - ticket)
                    )
                if index < len(old) and old[index]["reset_at"] > tick:
                    item["remaining"] = min(item["remaining"], old[index]["remaining"])
                    item["reset_at"] = max(item["reset_at"], old[index]["reset_at"])
                observed.append(item)
            if observed:
                state["windows"] = observed
            if status == 429 or retry_after is not None:
                delay = max(
                    [retry_after or 0]
                    + [w["reset_after"] for w in budget["windows"] if w["remaining"] == 0]
                )
                state["blocked_until"] = max(state["blocked_until"], tick + delay)

        # Failed observations must leave POST delivery intact and allow emergency DELETE.
        self.transaction(update, cleanup=True)


class Api:
    def __init__(self, key, base="https://api.runpod.io/v2", timeout=20, post_timeout=90):
        # Only local integration tests use a loopback URL.
        if base != "https://api.runpod.io/v2" and not base.startswith("http://127.0.0.1:"):
            raise ValueError("Unsupported API origin.")
        self.key, self.base, self.timeout = key, base.rstrip("/"), timeout
        self.post_timeout = post_timeout
        self._clock_anchor = None
        self._response = threading.local()
        self._pacing_enabled = False
        self._max_wait, self._read_retries = 5.0, 2
        binding = hashlib.sha256((self.base + "\0" + key).encode()).digest()
        with _PACERS_LOCK:
            self._pacer = _PACERS.get(binding)
            if self._pacer is None:
                self._pacer = _QuotaPacer(binding)
                _PACERS[binding] = self._pacer
        self._rate_limits = {"windows": [], "policies": [], "malformed": False}

    def configure_pacing(self, state_path=None, *, max_wait=5, read_retries=2):
        """Enable pacing; use one writable state path per account on this host."""
        if (
            type(max_wait) not in (int, float)
            or not math.isfinite(max_wait)
            or not 0 <= max_wait <= 60
        ):
            raise ValueError("Pacing max_wait must be finite and between 0 and 60 seconds")
        if type(read_retries) is not int or not 0 <= read_retries <= 3:
            raise ValueError("read_retries must be between 0 and 3")
        self._max_wait, self._read_retries, self._pacing_enabled = (
            float(max_wait),
            read_retries,
            True,
        )
        if state_path is not None:
            path = Path(state_path).resolve()
            with self._pacer.lock:
                if self._pacer.path is not None and self._pacer.path != path:
                    raise ValueError("One account/origin must use one pacing state path")
                self._pacer.path = path
        return self

    @property
    def rate_limits(self):
        return json.loads(
            json.dumps(
                getattr(self, "_rate_limits", {"windows": [], "policies": [], "malformed": False})
            )
        )

    @property
    def pacing_coordination(self):
        return self._pacer.coordination

    @contextmanager
    def pacing(self, *, deadline=None, cleanup=False, max_wait=None):
        """Thread-local context. deadline is absolute time.monotonic(), not epoch."""
        if deadline is not None and (
            type(deadline) not in (int, float) or not math.isfinite(deadline)
        ):
            raise ValueError("Pacing deadline must be a finite monotonic time")
        if max_wait is not None and (
            type(max_wait) not in (int, float)
            or not math.isfinite(max_wait)
            or not 0 <= max_wait <= 60
        ):
            raise ValueError("Pacing max_wait must be between 0 and 60 seconds")
        previous = getattr(self._response, "pacing", None)
        deadlines = [
            v for v in (deadline, previous.get("deadline") if previous else None) if v is not None
        ]
        limits = [self._max_wait if max_wait is None else max_wait]
        if previous:
            limits.append(max(0, previous["max_wait"] - previous["spent"]))
        context = {
            "deadline": min(deadlines) if deadlines else None,
            "cleanup": cleanup or bool(previous and previous["cleanup"]),
            "max_wait": min(limits),
            "spent": 0.0,
        }
        self._response.pacing = context
        try:
            yield self
        finally:
            if previous:
                previous["spent"] += context["spent"]
            self._response.pacing = previous

    def _wait(self, delay, context, method, path, *, status=0, not_sent=True):
        available = context["max_wait"] - context["spent"]
        if context["deadline"] is not None:
            available = min(available, context["deadline"] - time.monotonic())
        if delay > max(0, available) or available < 0:
            raise ApiPacingDeferred(
                method, path, delay, status=status, not_sent=not_sent, rate_limits=self.rate_limits
            )
        if delay > 0:
            time.sleep(delay)
            context["spent"] += delay

    def now(self):
        """Provider epoch advanced by a monotonic clock; local epoch until observed."""
        anchor = self._clock_anchor
        return time.time() if anchor is None else anchor[0] + time.monotonic() - anchor[1]

    @property
    def last_response(self):
        """Safe per-thread metadata, never headers or the response body."""
        return getattr(self._response, "value", None)

    def _record_response(self, method, path, status, headers):
        headers = headers or {}
        date = _http_date(headers.get("Date"))
        if date:
            tick = time.monotonic()
            previous = self._clock_anchor
            epoch = max(date[0], previous[0] + tick - previous[1]) if previous else date[0]
            self._clock_anchor = epoch, tick
        budget = parse_rate_limits(headers, date[0] if date else self.now())
        retry = _retry_after(_header(headers, "Retry-After"), date[0] if date else self.now())
        if status == 429:
            depleted = [w["reset_after"] for w in budget["windows"] if w["remaining"] == 0]
            if depleted:
                retry = max([retry or 0, *depleted])
        metadata = {
            "method": method,
            "path": path.split("?", 1)[0],
            "status": status,
            "response_date": date[1] if date else None,
            "retry_after": retry,
        }
        if budget["windows"] or budget["policies"] or budget["malformed"]:
            metadata["rate_limits"] = budget
            self._rate_limits = budget
        self._pacer.observe(budget, retry, status, getattr(self._response, "ticket", None))
        self._response.value = metadata
        return metadata

    def request(self, method, path, body=None):
        context = getattr(self._response, "pacing", None)
        if not self._pacing_enabled and context is None:
            return self._request_once(method, path, body)
        if context is None:
            with self.pacing():
                return self.request(method, path, body)
        method = method.upper()
        cleanup = context["cleanup"] or method == "DELETE"
        original_deadline = context["deadline"]
        request_end = (
            time.monotonic()
            + (self.post_timeout if method == "POST" else self.timeout)
            + context["max_wait"]
        )
        context["deadline"] = (
            min(original_deadline, request_end) if original_deadline is not None else request_end
        )
        try:
            for attempt in range(self._read_retries + 1):
                self._response.value = None
                while True:
                    if time.monotonic() >= context["deadline"]:
                        raise ApiPacingDeferred(method, path, 0, rate_limits=self.rate_limits)
                    try:
                        delay, ticket = self._pacer.reserve(cleanup)
                    except ApiPacingDeferred as deferred:
                        # Report why the pacing file could not be used.
                        raise ApiPacingDeferred(
                            method, path, 0.1, rate_limits=self.rate_limits, cause=deferred.cause
                        ) from None
                    if not delay:
                        break
                    self._wait(delay, context, method, path)
                self._response.ticket = ticket
                if time.monotonic() >= context["deadline"]:
                    raise ApiPacingDeferred(method, path, 0, rate_limits=self.rate_limits)
                try:
                    result = self._request_once(method, path, body)
                    meta = self.last_response or {}
                    # Legacy cleanup reads retry_after. Preserve the full hint
                    # separately while later calls enforce cooldown.
                    if (meta.get("retry_after") or 0) > max(
                        0, context["max_wait"] - context["spent"]
                    ):
                        meta["required_wait"], meta["retry_after"] = meta["retry_after"], 0
                    return result
                except ApiError as exc:
                    delay = exc.retry_after or 0
                    if delay > max(
                        0,
                        min(
                            context["max_wait"] - context["spent"],
                            context["deadline"] - time.monotonic(),
                        ),
                    ):
                        raise ApiPacingDeferred(
                            method,
                            path,
                            delay,
                            not_sent=exc.not_sent,
                            status=exc.status,
                            rate_limits=exc.rate_limits,
                        ) from None
                    if (
                        method not in ("GET", "HEAD")
                        or exc.status != 429
                        or attempt >= self._read_retries
                    ):
                        raise
                    # Without any provider delay, avoid a tight automatic retry.
                    self._wait(max(delay, 1), context, method, path, status=429, not_sent=False)
            raise AssertionError("Unreachable request retry state")
        finally:
            self._response.ticket = None
            context["deadline"] = original_deadline

    def _request_once(self, method, path, body=None):
        method = method.upper()
        self._response.value = None
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
        )
        state = {"sent": False, "not_sent": False}
        secrets = [self.key, *_secrets(body)]
        try:
            # Do not follow redirects carrying account credentials to another origin.
            opener = urllib.request.build_opener(
                NoRedirect(), _HTTPHandler(state), _HTTPSHandler(state)
            )
            timeout = self.post_timeout if method == "POST" else self.timeout
            context = getattr(self._response, "pacing", None)
            if context is not None and context["deadline"] is not None:
                timeout = min(timeout, max(0.001, context["deadline"] - time.monotonic()))
            with opener.open(req, timeout=timeout) as response:
                self._record_response(method, path, response.status, response.headers)
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            status = exc.code
            metadata = self._record_response(method, path, status, exc.headers)
            problem = {}
            try:
                raw = exc.read(ERROR_BODY_LIMIT + 1)
                if len(raw) <= ERROR_BODY_LIMIT:
                    value = json.loads(raw)
                    if isinstance(value, dict):
                        problem = value
            except (OSError, ValueError, http.client.HTTPException):
                pass
            finally:
                exc.close()
            raise ApiError(
                status,
                method,
                path,
                title=problem.get("title"),
                detail=problem.get("detail"),
                retry_after=metadata["retry_after"],
                response_date=metadata["response_date"],
                secrets=secrets,
                rate_limits=metadata.get("rate_limits"),
            ) from None
        except (OSError, ValueError, http.client.HTTPException):
            metadata = self.last_response or {}
            raise ApiError(
                0,
                method,
                path,
                not_sent=state["not_sent"],
                secrets=secrets,
                retry_after=metadata.get("retry_after"),
                response_date=metadata.get("response_date"),
                rate_limits=metadata.get("rate_limits"),
            ) from None

    def get_or_none(self, path):
        try:
            value = self.request("GET", path)
            expected_id = urllib.parse.unquote(path.split("?", 1)[0].rsplit("/", 1)[-1])
            if not isinstance(value, dict) or value.get("id") != expected_id:
                raise RuntimeError("Malformed resource response; absence is not verified.")
            return value
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def listing(self, path, field):
        if (
            getattr(self, "_pacing_enabled", False)
            and getattr(self._response, "pacing", None) is None
        ):
            with self.pacing(deadline=time.monotonic() + self.timeout + self._max_wait):
                return self.listing(path, field)
        parts = urllib.parse.urlsplit(path)
        if parts.scheme or parts.netloc or parts.fragment:
            raise ValueError("List path must be a relative API path without a fragment.")
        filters = [
            (name, value)
            for name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if name not in ("cursor", "limit")
        ]
        result, cursor, seen_cursors, seen_ids = [], None, set(), set()
        limit = LIST_PAGE_LIMIT
        wait_context = getattr(getattr(self, "_response", None), "pacing", None)
        if wait_context is None:
            wait_context = {"max_wait": 60, "spent": 0, "deadline": None}
        while True:
            query = [*filters, ("limit", str(limit))]
            if cursor is not None:
                query.append(("cursor", cursor))
            page_path = urllib.parse.urlunsplit(
                ("", "", parts.path, urllib.parse.urlencode(query, doseq=True), "")
            )
            try:
                data = self.request("GET", page_path)
            except ApiError as exc:
                if isinstance(exc, ApiPacingDeferred) or exc.status != 413 or limit == 1:
                    raise
                limit = max(1, limit // 2)
                if exc.retry_after is not None:
                    self._wait(
                        exc.retry_after, wait_context, "GET", page_path, status=413, not_sent=False
                    )
                continue

            if not isinstance(data, dict) or not isinstance(data.get(field), list):
                raise RuntimeError(INVALID_LIST)
            entries = data[field]
            for entry in entries:
                if not isinstance(entry, dict):
                    raise RuntimeError(INVALID_LIST)
                resource_id = entry.get("id")
                if parts.path == "/audit-logs" and field == "auditLogs":
                    resource_id = _audit_identity(entry)
                elif not isinstance(resource_id, str) or not resource_id:
                    raise RuntimeError(INVALID_LIST)
                if resource_id in seen_ids:
                    raise RuntimeError(INVALID_LIST)
                seen_ids.add(resource_id)

            page = data.get("pagination")
            if (
                not isinstance(page, dict)
                or type(page.get("hasNextPage")) is not bool
                or "nextCursor" not in page
            ):
                raise RuntimeError(INVALID_LIST)
            has_next = page["hasNextPage"]
            next_cursor = page["nextCursor"]
            if not has_next:
                if next_cursor is not None:
                    raise RuntimeError(INVALID_LIST)
                result.extend(entries)
                return result
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise RuntimeError(INVALID_LIST)
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            result.extend(entries)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _TrackedConnection:
    """Mark uncertainty before sendall: even a partial send is ambiguous."""

    def __init__(self, *args, send_state, **kwargs):
        self._send_state, self._connecting = send_state, False
        super().__init__(*args, **kwargs)

    def connect(self):
        self._connecting = True
        try:
            return super().connect()
        except (socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError):
            self._send_state["not_sent"] = not self._send_state["sent"]
            raise
        finally:
            self._connecting = False

    def send(self, data):
        if self.sock is None:
            self.connect()
        # Proxy CONNECT is not transmission of the application request.
        if not self._connecting:
            self._send_state["sent"] = True
        return super().send(data)


class _HTTPConnection(_TrackedConnection, http.client.HTTPConnection):
    pass


class _HTTPSConnection(_TrackedConnection, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def http_open(self, req):
        return self.do_open(_HTTPConnection, req, send_state=self.state)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context, send_state=self.state)
