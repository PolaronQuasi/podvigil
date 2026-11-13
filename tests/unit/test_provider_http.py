"""Provider contract regressions. Only loopback HTTP and in-process transports."""

import copy
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import socket
import ssl
import threading
import unittest
import urllib.parse
from unittest.mock import Mock, patch

from podvigil.controller.ownership import creation_receipt, receipt_owns
from podvigil.podrt.api import Api, ApiError, ERROR_BODY_LIMIT
from podvigil.podrt.teardown import delete_verified


DATE = "Tue, 10 Nov 2026 20:00:00 GMT"
EPOCH = datetime.datetime(2026, 11, 10, 20, tzinfo=datetime.timezone.utc).timestamp()


class Response(io.BytesIO):
    def __init__(self, body=b"{}", status=200, headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}


class LocalHttpTests(unittest.TestCase):
    def setUp(self):
        self.status, self.body, self.headers = 200, {}, {"Date": DATE}
        self.calls, self.lose_response = [], False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def serve(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.calls.append((self.command, self.path))
                if owner.lose_response:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                body = (
                    owner.body if isinstance(owner.body, bytes) else json.dumps(owner.body).encode()
                )
                self.send_response_only(owner.status)
                for key, value in owner.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_DELETE = serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True
        )
        self.thread.start()
        self.proxy_patch = patch("urllib.request.getproxies", return_value={})
        self.proxy_patch.start()
        self.api = Api(
            "dummy-account-secret",
            f"http://127.0.0.1:{self.server.server_port}",
            timeout=2,
            post_timeout=2,
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.proxy_patch.stop()

    def test_bounded_redacted_problem_details_and_retry_metadata(self):
        self.status = 409
        self.headers["Retry-After"] = "37"
        self.body = {
            "title": "Volume too young",
            "detail": "Retry after 37 seconds. dummy-account-secret env-value body-password "
            'Bearer unknown-auth-value password="unknown words" '
            "https://host/path?api_key=unknown-query-value " + "diagnostic word " * 100,
            "env": {"ignored": "never-show-this"},
        }
        with self.assertRaises(ApiError) as caught:
            self.api.request(
                "POST",
                "/pods?hidden=must-not-log",
                {"env": {"CUSTOM": "env-value"}, "password": "body-password"},
            )
        error = caught.exception
        for secret in (
            "dummy-account-secret",
            "env-value",
            "body-password",
            "unknown-auth-value",
            "unknown words",
            "unknown-query-value",
            "never-show-this",
            "must-not-log",
        ):
            self.assertNotIn(secret, str(error))
        self.assertIn("Retry after 37 seconds", str(error))
        self.assertLessEqual(len(error.title), 163)
        self.assertLessEqual(len(error.detail), 515)
        self.assertEqual(error.retry_after, 37)
        self.assertEqual(error.response_date, DATE)
        self.assertFalse(error.not_sent)
        self.assertEqual(len(self.calls), 1, "POST must never be retried")
        self.assertEqual(
            set(self.api.last_response),
            {"method", "path", "status", "response_date", "retry_after"},
        )

    def test_unlabelled_tokens_and_environment_echoes_are_redacted(self):
        self.status = 400
        self.body = {
            "title": "Bad request",
            "detail": 'Invalid field: OPAQUE_UNKNOWN_VALUE_123456. env={"CUSTOM":"unfamiliar value"}',
        }
        with self.assertRaises(ApiError) as caught:
            self.api.request("POST", "/pods", {})
        self.assertIn("Invalid field", str(caught.exception))
        self.assertNotIn("OPAQUE_UNKNOWN_VALUE_123456", str(caught.exception))
        self.assertNotIn("unfamiliar value", str(caught.exception))

    def test_invalid_or_oversize_problem_body_cannot_leak(self):
        self.status = 422
        for body in (
            b"<html>private-response</html>",
            b'["private-response"]',
            json.dumps({"title": "private-response", "detail": "x" * ERROR_BODY_LIMIT}).encode(),
        ):
            with self.subTest(size=len(body)):
                self.body = body
                with self.assertRaises(ApiError) as caught:
                    self.api.request("POST", "/pods", {})
                self.assertIsNone(caught.exception.title)
                self.assertIsNone(caught.exception.detail)
                self.assertNotIn("private-response", str(caught.exception))

    def test_status_errors_and_redirects_do_not_retry_post(self):
        for status in (307, 400, 401, 403, 409, 422, 429, 500, 503):
            with self.subTest(status=status):
                self.status = status
                self.headers.update({"Retry-After": "2", "Location": "/other"})
                before = len(self.calls)
                with self.assertRaises(ApiError) as caught:
                    self.api.request("POST", "/pods", {})
                self.assertEqual(caught.exception.status, status)
                self.assertFalse(caught.exception.not_sent)
                self.assertEqual(len(self.calls), before + 1)

    def test_lost_post_response_is_ambiguous_and_not_retried(self):
        self.lose_response = True
        with self.assertRaises(ApiError) as caught:
            self.api.request("POST", "/pods", {"name": "dummy-pod"})
        self.assertEqual(caught.exception.status, 0)
        self.assertFalse(caught.exception.not_sent)
        self.assertEqual(self.calls, [("POST", "/pods")])

    def test_retry_after_http_date_uses_provider_date_despite_local_skew(self):
        self.status = 429
        self.headers["Retry-After"] = "Tue, 10 Nov 2026 20:01:30 GMT"
        with (
            patch("podvigil.podrt.api.time.time", return_value=0),
            self.assertRaises(ApiError) as caught,
        ):
            self.api.request("GET", "/pods")
        self.assertEqual(caught.exception.retry_after, 90)
        self.assertEqual(caught.exception.response_date, DATE)

    def test_malformed_retry_after_is_not_a_delay(self):
        self.status = 429
        for value in ("-1", "NaN", "1.5", "later", "9" * 129):
            with self.subTest(value=value):
                self.headers["Retry-After"] = value
                with self.assertRaises(ApiError) as caught:
                    self.api.request("GET", "/pods")
                self.assertIsNone(caught.exception.retry_after)

    def test_only_404_is_absence_and_success_identity_is_checked(self):
        for body in (b"", b"null", b"[]", b"{}", b'{"id":"wrong"}'):
            self.body = body
            with self.subTest(body=body), self.assertRaises(RuntimeError):
                self.api.get_or_none("/pods/expected")
        self.status = 404
        self.assertIsNone(self.api.get_or_none("/pods/expected"))
        self.status, self.body = 200, {"id": "expected"}
        self.assertEqual(self.api.get_or_none("/pods/expected"), self.body)


class TransportTests(unittest.TestCase):
    def setUp(self):
        proxy_patch = patch("urllib.request.getproxies", return_value={})
        proxy_patch.start()
        self.addCleanup(proxy_patch.stop)

    def test_actual_loopback_connection_refusal_is_not_sent(self):
        with socket.socket() as reserved:
            # Reserve the port without listening so no other server owns it.
            reserved.bind(("127.0.0.1", 0))
            api = Api("dummy", f"http://127.0.0.1:{reserved.getsockname()[1]}", post_timeout=5)
            with patch("socket.create_connection", wraps=socket.create_connection) as connect:
                with self.assertRaises(ApiError) as caught:
                    api.request("POST", "/pods", {})
            self.assertTrue(caught.exception.not_sent)
            self.assertEqual(connect.call_count, 1)

    def test_dns_refusal_and_certificate_fail_before_application_send(self):
        cases = [
            ("HTTPConnection", socket.gaierror(-2, "dummy DNS")),
            ("HTTPConnection", ConnectionRefusedError("dummy refused")),
            ("HTTPSConnection", ssl.SSLCertVerificationError(1, "dummy certificate")),
        ]
        for connection, error in cases:
            base = (
                "https://api.runpod.io/v2"
                if connection == "HTTPSConnection"
                else "http://127.0.0.1:1"
            )
            api = Api("dummy", base)
            with (
                self.subTest(error=type(error).__name__),
                patch("http.client." + connection + ".connect", side_effect=error) as connect,
            ):
                with self.assertRaises(ApiError) as caught:
                    api.request("POST", "/pods", {})
                self.assertTrue(caught.exception.not_sent)
                self.assertEqual(connect.call_count, 1)

    def test_timeouts_resets_and_partial_sends_remain_ambiguous(self):
        api = Api("dummy", "http://127.0.0.1:1")
        with patch(
            "http.client.HTTPConnection.connect", side_effect=TimeoutError("connect timeout")
        ):
            with self.assertRaises(ApiError) as caught:
                api.request("POST", "/pods", {})
            self.assertFalse(caught.exception.not_sent)
        for failure in (
            ConnectionRefusedError("after send began"),
            ConnectionResetError("reset"),
            TimeoutError("send timeout"),
            ssl.SSLCertVerificationError(1, "after send began"),
        ):
            sock = Mock()
            sock.sendall.side_effect = failure
            with (
                self.subTest(failure=type(failure).__name__),
                patch("socket.create_connection", return_value=sock) as connect,
            ):
                with self.assertRaises(ApiError) as caught:
                    api.request("POST", "/pods", {})
                self.assertFalse(caught.exception.not_sent)
                self.assertEqual(connect.call_count, 1)
                self.assertEqual(sock.sendall.call_count, 1)

    def test_longer_post_timeout_and_read_default(self):
        for api, expected_read, expected_post in (
            (Api("dummy"), 20, 90),
            (Api("dummy", timeout=3, post_timeout=95), 3, 95),
        ):
            opener = Mock()
            opener.open.side_effect = [Response(), Response(), Response()]
            with patch("urllib.request.build_opener", return_value=opener):
                api.request("GET", "/pods")
                api.request("POST", "/pods", {})
                api.request("DELETE", "/pods/x")
            self.assertEqual(
                [call.kwargs["timeout"] for call in opener.open.call_args_list],
                [expected_read, expected_post, expected_read],
            )

    def test_provider_clock_uses_monotonic_and_ignores_invalid_dates(self):
        api = Api("dummy")
        opener = Mock()
        opener.open.side_effect = [
            Response(headers={"Date": DATE}),
            Response(headers={"Date": "malformed"}),
            Response(headers={"Date": DATE}),
        ]
        with (
            patch("podvigil.podrt.api.time") as clock,
            patch("urllib.request.build_opener", return_value=opener),
        ):
            clock.time.return_value, clock.monotonic.return_value = -9999, 100
            self.assertEqual(api.now(), -9999)
            api.request("GET", "/pods")
            self.assertEqual(api.now(), EPOCH)
            clock.monotonic.return_value, clock.time.return_value = 125, 999999999999
            api.request("GET", "/pods")
            self.assertEqual(api.now(), EPOCH + 25)
            self.assertIsNone(api.last_response["response_date"])
            api.request("GET", "/pods")  # A stale Date must not move an anchored clock backwards.
            self.assertEqual(api.now(), EPOCH + 25)

    def test_413_retry_honors_delay_and_preserves_cursor(self):
        api = Api("dummy")
        responses = [
            {"pods": [{"id": "a"}], "pagination": {"hasNextPage": True, "nextCursor": "c+/="}},
            ApiError(413, "GET", "/pods", retry_after=9),
            {"pods": [{"id": "b"}], "pagination": {"hasNextPage": False, "nextCursor": None}},
        ]
        with (
            patch.object(api, "request", side_effect=responses) as request,
            patch("podvigil.podrt.api.time.sleep") as sleep,
        ):
            self.assertEqual(
                [p["id"] for p in api.listing("/pods?cloud=SECURE", "pods")], ["a", "b"]
            )
        sleep.assert_called_once_with(9)
        queries = [
            urllib.parse.parse_qs(urllib.parse.urlsplit(call.args[1]).query)
            for call in request.call_args_list
        ]
        self.assertEqual(queries[-1], {"cloud": ["SECURE"], "limit": ["50"], "cursor": ["c+/="]})


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.job = {"job_id": "job", "prior_pod_ids": [], "prior_volume_ids": []}
        self.pod = {
            "id": "p",
            "createdAt": "2026-11-10T20:00:00.123000Z",
            "cloud": "SECURE",
            "dataCenterId": None,
            "gpu": {"id": "gpu"},
            "env": {"do-not-save": "dummy-secret"},
        }

    def test_normalized_timestamp_and_diagnostic_drift_preserve_receipt(self):
        for origin in ("create_response", "reconciled"):
            receipt = creation_receipt(self.pod, self.job, "pod", origin)
            current = {
                "id": "p",
                "createdAt": "2026-11-10T22:00:00.123+02:00",
                "cloud": "COMMUNITY",
                "dataCenterId": "changed",
                "gpu": {"id": "other", "count": 2},
            }
            self.assertTrue(receipt_owns(current, self.job, receipt, "pod"))
            self.assertEqual(
                receipt["fingerprint"], {"id": "p", "createdAt": "2026-11-10T20:00:00.123Z"}
            )
            self.assertIn("cloud", receipt["diagnostics"])
            self.assertNotIn("dummy-secret", json.dumps(receipt))

    def test_legacy_fingerprint_scheduling_fields_no_longer_block_cleanup(self):
        receipt = creation_receipt(self.pod, self.job, "pod", "create_response")
        receipt["fingerprint"].update(
            cloud="SECURE", dataCenterId="original", gpu={"id": "old", "count": 1}
        )
        receipt["fingerprint"]["createdAt"] = "2026-11-10T20:00:00.123000Z"
        current = {"id": "p", "createdAt": "2026-11-10T20:00:00.123Z", "gpu": None}
        self.assertTrue(receipt_owns(current, self.job, receipt, "pod"))

    def test_wrong_identity_or_creation_instant_is_rejected(self):
        receipt = creation_receipt(self.pod, self.job, "pod", "create_response")
        for changes in (
            {"id": "other"},
            {"createdAt": "2026-11-10T20:00:01.123Z"},
            {"createdAt": "not-a-date"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(receipt_owns({**self.pod, **changes}, self.job, receipt, "pod"))
        for changes in (
            {"job_id": "other"},
            {"kind": "volume"},
            {"established_by": "unknown"},
            {"id": "other"},
            {"fingerprint": None},
        ):
            self.assertFalse(receipt_owns(self.pod, self.job, {**receipt, **changes}, "pod"))
        self.job["prior_pod_ids"] = ["p"]
        self.assertFalse(receipt_owns(self.pod, self.job, receipt, "pod"))

    def test_missing_or_null_readback_timestamp_does_not_revoke_definitive_id(self):
        receipt = creation_receipt(self.pod, self.job, "pod", "create_response")
        for current in ({"id": "p"}, {"id": "p", "createdAt": None}):
            self.assertTrue(receipt_owns(current, self.job, receipt, "pod"))
            self.assertFalse(receipt_owns({**current, "id": "other"}, self.job, receipt, "pod"))

    def test_nanosecond_identity_is_not_rounded_away(self):
        self.pod["createdAt"] = "2026-11-10T20:00:00.123456789Z"
        receipt = creation_receipt(self.pod, self.job, "pod", "create_response")
        self.assertFalse(
            receipt_owns(
                {**self.pod, "createdAt": "2026-11-10T20:00:00.123456780Z"},
                self.job,
                receipt,
                "pod",
            )
        )
        self.assertTrue(
            receipt_owns(
                {**self.pod, "createdAt": "2026-11-10T22:00:00.1234567890+02:00"},
                self.job,
                receipt,
                "pod",
            )
        )

    def test_definitive_id_survives_missing_initial_timestamp_and_volume_drift(self):
        for stamp in (None, "invalid"):
            receipt = creation_receipt(
                {**self.pod, "createdAt": stamp}, self.job, "pod", "create_response"
            )
            self.assertEqual(receipt["fingerprint"], {"id": "p"})
            self.assertTrue(receipt_owns(self.pod, self.job, receipt, "pod"))
        volume = {"id": "v", "dataCenter": "original", "type": "STANDARD"}
        receipt = creation_receipt(volume, self.job, "volume", "create_response")
        self.assertTrue(
            receipt_owns(
                {"id": "v", "dataCenter": "changed", "type": "different"},
                self.job,
                receipt,
                "volume",
            )
        )
        with self.assertRaises(ValueError):
            creation_receipt({}, self.job, "pod", "create_response")


class CleanupApi(Api):
    """Production GET validation/list pagination, scripted resource responses."""

    def __init__(self, reads, deletes=None, inventory=None):
        super().__init__("dummy")
        self.reads = list(reads)
        self.deletes = list(deletes or [204])
        self.inventory = inventory or [
            {"pods": [], "pagination": {"hasNextPage": False, "nextCursor": None}}
        ]
        self.list_index, self.calls = 0, []

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        self._response.value = None
        if method == "DELETE":
            response = self.deletes.pop(0) if len(self.deletes) > 1 else self.deletes[0]
            if isinstance(response, ApiError):
                raise response
            self._record_response(method, path, response, {"Date": DATE})
            return None
        if urllib.parse.urlsplit(path).path == "/pods":
            value = self.inventory[min(self.list_index, len(self.inventory) - 1)]
            self.list_index += 1
            return copy.deepcopy(value)
        response = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        if isinstance(response, ApiError):
            raise response
        self._record_response(method, path, 404 if response is None else 200, {"Date": DATE})
        if response is None:
            raise ApiError(404, method, path, response_date=DATE)
        return copy.deepcopy(response)


LIVE = {"id": "p", "status": "RUNNING"}
TOMB = {"id": "p", "status": "TERMINATED"}


class CleanupEvidenceTests(unittest.TestCase):
    def clean(self, api, **kwargs):
        return delete_verified(
            api, "/pods/p", lambda obj: obj["id"] == "p", sleep=lambda _: None, **kwargs
        )

    def test_owned_tombstone_requires_complete_inventory_and_no_new_delete(self):
        api = CleanupApi(
            [TOMB],
            inventory=[
                {
                    "pods": [{"id": "unrelated"}],
                    "pagination": {"hasNextPage": True, "nextCursor": "next"},
                },
                {"pods": [], "pagination": {"hasNextPage": False, "nextCursor": None}},
            ],
        )
        evidence = self.clean(api, actor="watchdog")
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["delete_attempts"], [])
        self.assertEqual(evidence["actor"], "watchdog")
        self.assertNotIn("deleted_by", evidence, "The verifier cannot infer the deletion actor")
        self.assertEqual(evidence["proof"]["point_states"], ["terminated", "terminated"])
        self.assertTrue(evidence["proof"]["inventory"]["complete"])
        self.assertTrue(evidence["proof"]["inventory"]["absent"])
        self.assertEqual(api.list_index, 2)
        json.dumps(evidence, allow_nan=False)

    def test_tombstone_still_in_later_page_is_not_deletion_proof(self):
        api = CleanupApi(
            [TOMB],
            inventory=[
                {"pods": [], "pagination": {"hasNextPage": True, "nextCursor": "next"}},
                {"pods": [TOMB], "pagination": {"hasNextPage": False, "nextCursor": None}},
            ],
        )
        with self.assertRaisesRegex(RuntimeError, "NOT verified") as caught:
            self.clean(api, attempts=1)
        self.assertFalse(caught.exception.evidence["verified"])
        self.assertEqual(caught.exception.evidence["delete_attempts"], [])

    def test_tombstone_malformed_inventory_and_unowned_tombstone_fail_closed(self):
        api = CleanupApi([TOMB], inventory=[{"pods": [], "pagination": {"hasNextPage": True}}])
        with self.assertRaises(RuntimeError):
            self.clean(api, attempts=1)
        api = CleanupApi([TOMB])
        with self.assertRaises(PermissionError):
            delete_verified(api, "/pods/p", lambda _: False, sleep=lambda _: None)
        self.assertEqual(api.calls, [("GET", "/pods/p")])

    def test_delete_then_tombstone_returns_actual_attempt_status_and_readback(self):
        evidence = self.clean(CleanupApi([LIVE, TOMB, TOMB]), actor="controller")
        self.assertEqual(evidence["delete_attempts"][0]["status"], 204)
        self.assertEqual(evidence["delete_attempts"][0]["outcome"], "accepted")
        self.assertEqual(evidence["delete_attempts"][0]["actor"], "controller")
        self.assertTrue(evidence["proof"]["observed_tombstone"])
        self.assertFalse(evidence["proof"]["observed_absence"])

    def test_preexisting_absence_does_not_invent_a_delete_attempt(self):
        evidence = self.clean(CleanupApi([None]), confirm_absent=lambda: True)
        self.assertEqual(evidence["delete_attempts"], [])
        self.assertTrue(evidence["proof"]["observed_absence"])
        self.assertFalse(evidence["proof"]["observed_tombstone"])
        self.assertEqual(
            [r["status"] for r in evidence["observations"] if r["type"] == "point_read"], [404, 404]
        )

    def test_optional_inventory_contract_is_explicit_in_evidence(self):
        evidence = self.clean(CleanupApi([None]))
        self.assertIsNone(evidence["proof"]["inventory"])
        with self.assertRaisesRegex(RuntimeError, "NOT verified"):
            self.clean(CleanupApi([None]), attempts=1, confirm_absent=lambda: False)

    def test_exited_or_still_present_is_not_verified_by_delete_acceptance(self):
        for status in ("RUNNING", "EXITED", "ERROR"):
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(RuntimeError, "NOT verified") as caught,
            ):
                self.clean(CleanupApi([{**LIVE, "status": status}]), attempts=1)
            self.assertFalse(caught.exception.evidence["verified"])
            self.assertIsNone(caught.exception.evidence["proof"])
            self.assertEqual(caught.exception.evidence["delete_attempts"][0]["status"], 204)

    def test_lost_delete_response_followed_by_absence_is_reported_truthfully(self):
        evidence = self.clean(CleanupApi([LIVE, None], deletes=[ApiError(0, "DELETE", "/pods/p")]))
        self.assertEqual(len(evidence["delete_attempts"]), 1)
        self.assertEqual(evidence["delete_attempts"][0]["status"], 0)
        self.assertEqual(evidence["delete_attempts"][0]["outcome"], "error")
        self.assertTrue(evidence["proof"]["observed_absence"])

    def test_delete_404_still_requires_separated_readback(self):
        evidence = self.clean(
            CleanupApi([LIVE, None], deletes=[ApiError(404, "DELETE", "/pods/p")])
        )
        self.assertEqual(evidence["delete_attempts"][0]["outcome"], "not_found")
        self.assertEqual(evidence["proof"]["point_states"], ["absent", "absent"])

    def test_cleanup_retry_after_applies_to_delete_and_observation_errors(self):
        for api in (
            CleanupApi(
                [LIVE, LIVE, None],
                deletes=[ApiError(409, "DELETE", "/pods/p", retry_after=42), 204],
            ),
            CleanupApi([ApiError(429, "GET", "/pods/p", retry_after=42), None]),
        ):
            delays = []
            evidence = delete_verified(api, "/pods/p", lambda _: True, sleep=delays.append)
            self.assertTrue(evidence["verified"])
            self.assertEqual(delays, [42, 1])

    def test_auth_error_stops_and_preserves_failed_attempt_evidence(self):
        api = CleanupApi([LIVE], deletes=[ApiError(403, "DELETE", "/pods/p", retry_after=40)])
        delays = []
        with self.assertRaisesRegex(RuntimeError, "NOT verified") as caught:
            delete_verified(api, "/pods/p", lambda _: True, sleep=delays.append)
        self.assertEqual(delays, [])
        self.assertFalse(caught.exception.evidence["verified"])
        self.assertEqual(caught.exception.evidence["delete_attempts"][0]["status"], 403)


if __name__ == "__main__":
    unittest.main()
