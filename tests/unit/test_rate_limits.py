"""Local contract/pacing regressions; no account, credentials, or provider calls."""

from contextlib import contextmanager
from email.message import Message
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import uuid
from unittest.mock import Mock, patch

from podvigil.podrt.api import Api, ApiError, ApiPacingDeferred, parse_rate_limits


DATE = "Tue, 10 Nov 2026 20:00:00 GMT"


def headers(remaining=10, reset=20, limit=100, window=60, **extra):
    return {
        "Date": DATE,
        "RateLimit": f'"minute";r={remaining};t={reset}',
        "RateLimit-Policy": f'"minute";q={limit};w={window}',
        **extra,
    }


class Response(io.BytesIO):
    def __init__(self, body=None, status=200, values=None):
        super().__init__(json.dumps(body if body is not None else {"id": "p"}).encode())
        self.status, self.headers = status, values or {"Date": DATE}


def error(status, values):
    return urllib.error.HTTPError(
        "http://127.0.0.1:1/pods",
        status,
        "dummy",
        values,
        io.BytesIO(b'{"title":"Throttled","detail":"Try later"}'),
    )


class Clock:
    def __init__(self):
        self.tick, self.sleeps = 1000.0, []

    def monotonic(self):
        return self.tick

    def time(self):
        return 1794340800 + self.tick

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.tick += seconds


@contextmanager
def transport(responses, clock=None):
    opener = Mock()
    opener.open.side_effect = responses
    with patch("podvigil.podrt.api.urllib.request.build_opener", return_value=opener):
        if clock:
            with patch("podvigil.podrt.api.time", clock):
                yield opener
        else:
            yield opener


def client(**options):
    # A different dummy binding isolates each test's process-level budget.
    return Api("dummy-" + uuid.uuid4().hex, "http://127.0.0.1:1").configure_pacing(**options)


class RateHeaderTests(unittest.TestCase):
    def test_live_runpod_shape_pairs_all_windows_with_policies(self):
        budget = parse_rate_limits(
            {
                "RateLimit": '"minute";r=0;t=12, "hour";r=2800;t=1812, "day";r=49500;t=45012',
                "RateLimit-Policy": '"day";q=50000;w=86400, "minute";q=180;w=60, "hour";q=3600;w=3600',
            }
        )
        self.assertFalse(budget["malformed"])
        self.assertEqual(
            {
                w["window_seconds"]: (w["limit"], w["remaining"], w["reset_after"])
                for w in budget["windows"]
            },
            {60: (180, 0, 12), 3600: (3600, 2800, 1812), 86400: (50000, 49500, 45012)},
        )

    def test_older_dictionary_and_multiple_policy_windows(self):
        budget = parse_rate_limits(
            {
                "RateLimit": "limit=100, remaining=7, reset=13",
                "RateLimit-Policy": "100;w=60, 1000;w=3600",
            }
        )
        self.assertEqual(
            budget["windows"],
            [{"limit": 100, "remaining": 7, "reset_after": 13, "window_seconds": 60}],
        )
        self.assertEqual(len(budget["policies"]), 2)

    def test_separate_legacy_fields_and_epoch_variant(self):
        self.assertEqual(
            parse_rate_limits(
                {"ratelimit-limit": "10", "ratelimit-remaining": "2", "ratelimit-reset": "7"}
            )["windows"][0]["reset_after"],
            7,
        )
        self.assertEqual(
            parse_rate_limits(
                {
                    "X-RateLimit-Limit": "10",
                    "X-RateLimit-Remaining": "2",
                    "X-RateLimit-Reset": "107",
                },
                now=100,
            )["windows"][0]["reset_after"],
            7,
        )

    def test_duplicate_header_lines_quotes_and_unpublished_labels(self):
        value = Message()
        value.add_header("RateLimit", '"secret,window";r=2;t=4;comment="a;b,c"')
        value.add_header("RateLimit", '"other";r=4;t=5')
        value.add_header("RateLimit-Policy", '"secret,window";q=10;w=6')
        budget = parse_rate_limits(value)
        self.assertFalse(budget["malformed"])
        self.assertEqual(len(budget["windows"]), 2)
        serialized = json.dumps(budget)
        for secret in ("secret", 'window"', "a;b,c", "other", "comment"):
            self.assertNotIn(secret, serialized)

    def test_malformed_headers_never_escape_or_invent_quota(self):
        for live in (
            '"minute";r=-1;t=2',
            '"minute";r=NaN;t=2',
            '"minute";r=1.5;t=2',
            '"minute";r=1;t=0',
            '"minute";r=1;r=2;t=3',
            '"minute";r=1',
            '"minute";r=1;t=2,',
            '"minute";r=1;t=2, "minute";r=2;t=3',
            '"unclosed;r=1;t=2',
            "limit=1, remaining=2, reset=3",
            "limit=1, remaining=0, reset=Infinity",
            "sensitive\nheader",
            "x" * 8193,
        ):
            with self.subTest(live=live[:70]):
                budget = parse_rate_limits({"RateLimit": live})
                self.assertTrue(budget["malformed"])
                self.assertEqual(budget["windows"], [])
                self.assertNotIn(live, json.dumps(budget))

    def test_invalid_policy_does_not_erase_valid_depleted_state(self):
        budget = parse_rate_limits(
            {"RateLimit": '"minute";r=0;t=10', "RateLimit-Policy": '"minute";q=-1;w=60'}
        )
        self.assertTrue(budget["malformed"])
        self.assertEqual(budget["policies"], [])
        self.assertEqual(budget["windows"], [{"remaining": 0, "reset_after": 10}])

    def test_omitted_headers_make_no_exemption_or_quota_claim(self):
        self.assertEqual(parse_rate_limits({}), {"windows": [], "policies": [], "malformed": False})

    def test_policy_only_is_metadata_not_an_available_budget(self):
        budget = parse_rate_limits({"RateLimit-Policy": '"minute";q=10;w=60'})
        self.assertEqual(budget["windows"], [])
        self.assertEqual(budget["policies"], [{"limit": 10, "window_seconds": 60}])


class PacingTests(unittest.TestCase):
    def test_read_429_waits_for_max_retry_after_and_all_depleted_resets(self):
        api, clock = client(max_wait=10, read_retries=1), Clock()
        values = {
            "Date": DATE,
            "Retry-After": "2",
            "RateLimit": '"minute";r=0;t=3, "hour";r=0;t=5',
            "RateLimit-Policy": '"minute";q=10;w=60, "hour";q=100;w=3600',
        }
        with transport([error(429, values), Response()], clock) as opener:
            self.assertEqual(api.get_or_none("/pods/p"), {"id": "p"})
        self.assertEqual(clock.sleeps, [5])
        self.assertEqual(opener.open.call_count, 2)

    def test_read_429_with_no_headers_has_bounded_retry_count(self):
        api, clock = client(max_wait=4, read_retries=2), Clock()
        with transport([error(429, {}) for _ in range(3)], clock) as opener:
            with self.assertRaises(ApiError) as caught:
                api.request("GET", "/pods")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(clock.sleeps, [1, 1])

    def test_creation_429_never_retries_post(self):
        api, clock = client(max_wait=5), Clock()
        with transport([error(429, headers(remaining=0, reset=2))], clock) as opener:
            with self.assertRaises(ApiError) as caught:
                api.request("POST", "/pods", {"env": {"TOKEN": "dummy-value"}})
        self.assertEqual(caught.exception.status, 429)
        self.assertFalse(caught.exception.not_sent)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(clock.sleeps, [])

    def test_preflight_depletion_defers_post_without_sending(self):
        api, clock = client(max_wait=1), Clock()
        with transport([Response(values=headers(remaining=0, reset=20))], clock) as opener:
            api.request("GET", "/pods")
            with self.assertRaises(ApiPacingDeferred) as caught:
                api.request("POST", "/pods", {})
        self.assertTrue(caught.exception.not_sent)
        self.assertEqual(caught.exception.required_wait, 20)
        self.assertEqual(caught.exception.retry_after, 0)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(clock.sleeps, [])

    def test_long_provider_429_is_not_reclassified_as_unsent(self):
        api, clock = client(max_wait=1), Clock()
        with transport([error(429, headers(remaining=0, reset=3600))], clock) as opener:
            with self.assertRaises(ApiPacingDeferred) as caught:
                api.request("POST", "/pods", {})
        self.assertEqual(caught.exception.status, 429)
        self.assertFalse(caught.exception.not_sent)
        self.assertEqual(caught.exception.required_wait, 3600)
        self.assertEqual(caught.exception.retry_after, 0)
        self.assertEqual(opener.open.call_count, 1)

    def test_cleanup_can_use_reserved_budget_and_bypass_read_smoothing(self):
        api, clock = client(max_wait=0), Clock()
        with transport(
            [Response(values=headers(remaining=2)), Response(status=204)], clock
        ) as opener:
            api.request("GET", "/pods")
            with self.assertRaises(ApiPacingDeferred):
                api.request("GET", "/pods")
            api.request("DELETE", "/pods/p")
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(clock.sleeps, [])

    def test_cleanup_context_also_prioritizes_get_and_list(self):
        api, clock = client(max_wait=0), Clock()
        page = {"pods": [], "pagination": {"hasNextPage": False, "nextCursor": None}}
        with transport([Response(values=headers(remaining=2)), Response(), Response(page)], clock):
            api.request("GET", "/pods")
            with api.pacing(cleanup=True):
                self.assertEqual(api.get_or_none("/pods/p"), {"id": "p"})
                self.assertEqual(api.listing("/pods", "pods"), [])

    def test_cleanup_does_not_ignore_actual_depletion_or_429_cooldown(self):
        api, clock = client(max_wait=0), Clock()
        with transport([Response(values=headers(remaining=0, reset=10))], clock) as opener:
            api.request("GET", "/pods")
            with self.assertRaises(ApiPacingDeferred):
                api.request("DELETE", "/pods/p")
        self.assertEqual(opener.open.call_count, 1)

    def test_deadline_prevents_wait_and_clamps_network_timeout(self):
        api, clock = client(max_wait=10), Clock()
        with transport([Response(values=headers(remaining=0, reset=7))], clock) as opener:
            with api.pacing(deadline=clock.tick + 3):
                api.request("GET", "/pods")
                with self.assertRaises(ApiPacingDeferred):
                    api.request("GET", "/pods")
        self.assertLessEqual(opener.open.call_args.kwargs["timeout"], 3)
        self.assertEqual(clock.sleeps, [])

    def test_expired_context_never_sends_even_cleanup_or_post(self):
        api, clock = client(), Clock()
        with transport([], clock) as opener:
            with api.pacing(deadline=clock.tick - 1, cleanup=True):
                for method in ("GET", "DELETE", "POST"):
                    with (
                        self.subTest(method=method),
                        self.assertRaises(ApiPacingDeferred) as caught,
                    ):
                        api.request(method, "/pods/p")
                    self.assertTrue(caught.exception.not_sent)
            self.assertEqual(opener.open.call_count, 0)

    def test_deadline_expiring_during_reservation_prevents_dispatch(self):
        api, clock = client(), Clock()

        def reserve(_):
            clock.tick += 2
            return 0, 1

        with (
            transport([], clock) as opener,
            patch.object(api._pacer, "reserve", side_effect=reserve),
        ):
            with (
                api.pacing(deadline=clock.tick + 1, cleanup=True),
                self.assertRaises(ApiPacingDeferred) as caught,
            ):
                api.request("POST", "/pods", {})
        self.assertTrue(caught.exception.not_sent)
        self.assertEqual(opener.open.call_count, 0)

    def test_nested_context_cannot_extend_parent_deadline_or_wait_budget(self):
        api, clock = client(max_wait=10), Clock()
        with transport([error(429, {"Retry-After": "3"})], clock) as opener:
            with api.pacing(deadline=clock.tick + 2, max_wait=1):
                with api.pacing(deadline=clock.tick + 100, max_wait=20, cleanup=True):
                    with self.assertRaises(ApiPacingDeferred):
                        api.request("GET", "/pods")
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(clock.sleeps, [])

    def test_context_is_restored_after_failure(self):
        api, clock = client(), Clock()
        with transport([Response()], clock) as opener:
            with self.assertRaises(ApiPacingDeferred):
                with api.pacing(deadline=clock.tick - 1):
                    api.request("GET", "/pods")
            api.request("GET", "/pods")
        self.assertEqual(opener.open.call_count, 1)

    def test_late_response_cannot_refill_a_reserved_window(self):
        api, clock = client(max_wait=0), Clock()
        with patch("podvigil.podrt.api.time", clock):
            api._record_response("GET", "/pods", 200, headers(remaining=5))
            _, first = api._pacer.reserve(True)
            _, second = api._pacer.reserve(True)
            api._pacer.observe(parse_rate_limits(headers(remaining=3)), None, 200, second)
            api._pacer.observe(parse_rate_limits(headers(remaining=4)), None, 200, first)
            self.assertEqual(api._pacer.state["windows"][0]["remaining"], 3)

    def test_malformed_or_omitted_headers_do_not_clear_known_depletion(self):
        api, clock = client(max_wait=0), Clock()
        with patch("podvigil.podrt.api.time", clock):
            api._record_response("GET", "/pods", 200, headers(remaining=0, reset=10))
            api._record_response("GET", "/pods", 200, {"RateLimit": "malformed"})
            api._record_response("GET", "/pods", 200, {})
            delay, ticket = api._pacer.reserve(True)
            self.assertEqual(delay, 10)
            self.assertIsNone(ticket)

    def test_observations_are_numeric_only_and_copy_safe(self):
        api = client()
        values = {
            "RateLimit": '"Bearer private-window-secret";r=2;t=3;x="private-comment"',
            "RateLimit-Policy": '"Bearer private-window-secret";q=10;w=60',
        }
        with transport([Response(values=values)]):
            api.request("GET", "/pods")
        budget = api.rate_limits
        self.assertEqual(budget, api.last_response["rate_limits"])
        self.assertNotIn("private", json.dumps(budget))
        budget["windows"].clear()
        self.assertTrue(api.rate_limits["windows"])

    def test_legacy_raw_client_keeps_single_request_default(self):
        api = Api("dummy-unconfigured", "http://127.0.0.1:1")
        with transport([error(429, headers(remaining=0, reset=90))]) as opener:
            with self.assertRaises(ApiError) as caught:
                api.request("GET", "/pods")
        self.assertNotIsInstance(caught.exception, ApiPacingDeferred)
        self.assertEqual(caught.exception.retry_after, 90)
        self.assertEqual(opener.open.call_count, 1)

    def test_helper_retry_cannot_turn_very_large_header_into_sleep(self):
        from podvigil.podrt.teardown import delete_verified

        api, clock, legacy_sleeps = client(max_wait=1), Clock(), []
        with transport([error(429, {"Retry-After": "86400"})], clock) as opener:
            with api.pacing(cleanup=True), self.assertRaisesRegex(RuntimeError, "NOT verified"):
                delete_verified(
                    api, "/pods/p", lambda _: True, attempts=2, sleep=legacy_sleeps.append
                )
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(legacy_sleeps, [1])
        self.assertEqual(clock.sleeps, [])


class CoordinationTests(unittest.TestCase):
    def test_reading_state_written_while_waiting_for_lock_is_not_a_reboot(self):
        from podvigil.podrt.records import exclusive_lock

        clock = Clock()
        with tempfile.TemporaryDirectory(prefix="runpod-rate-lock-") as temporary:
            path = Path(temporary) / "budget.json"
            api = client(state_path=path, max_wait=0)
            with patch("podvigil.podrt.api.time", clock):
                clock.tick = 1001
                api._record_response("GET", "/pods", 200, headers(remaining=0, reset=10))
                api._pacer.state = {
                    "windows": [],
                    "blocked_until": 0,
                    "next_read": 0,
                    "sequence": 0,
                }
                clock.tick = 1000

                @contextmanager
                def waited(*args, **kwargs):
                    with exclusive_lock(*args, **kwargs):
                        clock.tick = 1002
                        yield

                with patch("podvigil.podrt.records.exclusive_lock", waited):
                    delay, ticket = api._pacer.reserve(True)
            self.assertEqual(delay, 9)
            self.assertIsNone(ticket)

    def test_threads_and_instances_cannot_spend_same_remaining_unit(self):
        key = "dummy-concurrent-" + uuid.uuid4().hex
        apis = [Api(key, "http://127.0.0.1:1").configure_pacing(max_wait=0) for _ in range(10)]
        apis[0]._record_response("GET", "/pods", 200, headers(remaining=3, reset=60))
        barrier, lock, outcomes = threading.Barrier(len(apis)), threading.Lock(), []

        def worker(api):
            barrier.wait(timeout=3)
            with api.pacing(cleanup=True):
                try:
                    api.request("GET", "/pods/p")
                    outcome = "sent"
                except ApiPacingDeferred:
                    outcome = "deferred"
            with lock:
                outcomes.append(outcome)

        with transport(lambda *args, **kwargs: Response()) as opener:
            workers = [threading.Thread(target=worker, args=(api,)) for api in apis]
            for worker_thread in workers:
                worker_thread.start()
            for worker_thread in workers:
                worker_thread.join(5)
                self.assertFalse(worker_thread.is_alive())
        self.assertEqual(outcomes.count("sent"), 3)
        self.assertEqual(outcomes.count("deferred"), 7)
        self.assertEqual(opener.open.call_count, 3)

    def test_waiting_read_does_not_hold_lock_or_delay_cleanup(self):
        api = client(max_wait=5)
        api._record_response("GET", "/pods", 200, headers(remaining=3, reset=4))
        api._pacer.state["next_read"] = time.monotonic() + 2
        waiting, release, outcomes = threading.Event(), threading.Event(), []

        def sleep(_):
            waiting.set()
            if not release.wait(3):
                raise AssertionError("cleanup starved")
            # End the ordinary operation after proving cleanup progressed.
            raise ApiPacingDeferred("GET", "/pods", 2)

        def ordinary():
            try:
                api.request("GET", "/pods")
            except ApiPacingDeferred:
                outcomes.append("deferred")

        with (
            transport([Response(status=204)]) as opener,
            patch("podvigil.podrt.api.time.sleep", side_effect=sleep),
        ):
            worker = threading.Thread(target=ordinary)
            worker.start()
            try:
                self.assertTrue(waiting.wait(2))
                api.request("DELETE", "/pods/p")
                self.assertEqual(opener.open.call_count, 1)
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes, ["deferred"])

    def test_processes_share_budget_with_numeric_only_disk_state(self):
        with tempfile.TemporaryDirectory(prefix="runpod-rate-") as temporary:
            path = Path(temporary) / "budget.json"
            key = "dummy-process-key"
            api = Api(key, "http://127.0.0.1:1").configure_pacing(path, max_wait=0)
            api._record_response("GET", "/pods", 200, headers(remaining=1, reset=60))
            code = """import sys
import tests
from podvigil.podrt.api import Api
a=Api('dummy-process-key','http://127.0.0.1:1').configure_pacing(sys.argv[1],max_wait=0)
delay,ticket=a._pacer.reserve(True)
print('reserved' if ticket else 'deferred')
"""
            children = [
                subprocess.Popen(
                    [sys.executable, "-B", "-c", code, str(path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    **(
                        {"creationflags": subprocess.CREATE_NO_WINDOW}
                        if sys.platform == "win32"
                        else {}
                    ),
                )
                for _ in range(3)
            ]
            outputs = []
            try:
                for child in children:
                    out, err = child.communicate(timeout=10)
                    self.assertEqual(child.returncode, 0, err)
                    outputs.append(out.strip())
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=5)
            self.assertEqual(outputs.count("reserved"), 1)
            self.assertEqual(outputs.count("deferred"), 2)
            serialized = path.read_text()
            for secret in (key, "RateLimit", "Bearer", "/pods", "minute"):
                self.assertNotIn(secret, serialized)

    def test_corrupt_or_other_credential_state_defers_normal_but_not_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="runpod-rate-binding-") as temporary:
            path = Path(temporary) / "budget.json"
            first = client(state_path=path, max_wait=0)
            first._record_response("GET", "/pods", 200, headers(remaining=0, reset=60))
            original = path.read_bytes()
            second = client(state_path=path, max_wait=0)
            with transport([Response(status=204)]) as opener:
                with self.assertRaises(ApiPacingDeferred):
                    second.request("GET", "/pods")
                second.request("DELETE", "/pods/p")
            self.assertEqual(opener.open.call_count, 1)
            self.assertEqual(second.pacing_coordination, "local_fallback")
            self.assertEqual(path.read_bytes(), original)

    def test_valid_json_of_the_wrong_shape_defers_normal_but_not_cleanup(self):
        # Valid JSON with the wrong object shape still counts as a damaged pacing file.
        # Before, a list raised AttributeError past the handler, and even a cleanup request failed.
        binding_free = [
            "[]",
            '"text"',
            "7",
            "null",
            '{"salt": 5}',
            '{"salt": "' + "0" * 32 + '", "binding": 5}',
        ]
        for damaged in binding_free:
            with (
                self.subTest(damaged=damaged),
                tempfile.TemporaryDirectory(prefix="runpod-rate-shape-") as temporary,
            ):
                path = Path(temporary) / "budget.json"
                path.write_text(damaged, encoding="utf-8")
                api = client(state_path=path, max_wait=0)
                with transport([Response(status=204)]) as opener:
                    with self.assertRaises(ApiPacingDeferred) as caught:
                        api.request("GET", "/pods")
                    self.assertIn(
                        "pacing file in the state folder is unusable", str(caught.exception)
                    )
                    # Cleanup continues with this process's own state.
                    api.request("DELETE", "/pods/p")
                self.assertEqual(opener.open.call_count, 1)
                self.assertEqual(api.pacing_coordination, "local_fallback")
                self.assertEqual(path.read_text(encoding="utf-8"), damaged)  # never overwritten

    def test_a_valid_binding_with_a_malformed_state_defers_normal_but_not_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="runpod-rate-state-") as temporary:
            path = Path(temporary) / "budget.json"
            # Reuse the key so the file's binding matches.
            key = "dummy-" + uuid.uuid4().hex
            first = Api(key, "http://127.0.0.1:1").configure_pacing(state_path=path, max_wait=0)
            first._record_response("GET", "/pods", 200, headers(remaining=5, reset=60))
            good = json.loads(path.read_text(encoding="utf-8"))
            for state in ([], "text", 7, {**good["state"], "windows": [["remaining", "reset_at"]]}):
                with self.subTest(state=state):
                    path.write_text(json.dumps({**good, "state": state}), encoding="utf-8")
                    second = Api(key, "http://127.0.0.1:1").configure_pacing(
                        state_path=path, max_wait=0
                    )
                    with transport([Response(status=204)]) as opener:
                        with self.assertRaises(ApiPacingDeferred) as caught:
                            second.request("GET", "/pods")
                        # The malformed shape causes the deferral.
                        self.assertIn(caught.exception.cause, ("ValueError", "TypeError"))
                        second.request("DELETE", "/pods/p")
                    self.assertEqual(opener.open.call_count, 1)
                    self.assertEqual(second.pacing_coordination, "local_fallback")

    def test_the_deferral_names_its_cause_and_the_remedy(self):
        with tempfile.TemporaryDirectory(prefix="runpod-rate-cause-") as temporary:
            path = Path(temporary) / "budget.json"
            path.write_text("{not json", encoding="utf-8")  # damaged
            api = client(state_path=path, max_wait=0)
            with transport([]), self.assertRaises(ApiPacingDeferred) as caught:
                api.request("GET", "/pods")
            message = str(caught.exception)
            self.assertIn(
                "this key's pacing file in the state folder is unusable (JSONDecodeError)", message
            )
            # The remedy appears in full.
            self.assertIn("when no job runs, delete the api-rate-limit files there", message)
            self.assertEqual(caught.exception.cause, "JSONDecodeError")
            from podvigil.podrt.records import exclusive_lock

            path.unlink()
            with exclusive_lock(str(path.resolve()) + ".lock"):  # another process holds the file
                with transport([]), self.assertRaises(ApiPacingDeferred) as caught:
                    api.request("GET", "/pods")
            self.assertIn("another process held the pacing file; try again", str(caught.exception))

    def test_a_new_key_is_not_held_back_by_the_old_keys_pacing_file(self):
        # F5: one pacing file per key in the state folder; neither key is ever written.
        from podvigil import settings

        with tempfile.TemporaryDirectory(prefix="runpod-rate-keys-") as temporary:
            settings.configure(state_dir=temporary)
            self.addCleanup(settings.configure)
            old_key, new_key = "dummy-old-" + uuid.uuid4().hex, "dummy-new-" + uuid.uuid4().hex
            old = Api(old_key, "http://127.0.0.1:1").configure_pacing(
                settings.pacing_file(old_key), max_wait=0
            )
            # The old key's budget is spent.
            old._record_response("GET", "/pods", 200, headers(remaining=0, reset=60))
            new = Api(new_key, "http://127.0.0.1:1").configure_pacing(
                settings.pacing_file(new_key), max_wait=0
            )
            with transport([Response({"id": "p"})]) as opener:
                new.request("GET", "/pods/p")
            self.assertEqual(opener.open.call_count, 1)
            written = list(Path(temporary).glob("api-rate-limit-*.json"))
            self.assertEqual(len(written), 2)
            for path in written:
                for key in (old_key, new_key):
                    self.assertNotIn(key, path.name + path.read_text(encoding="utf-8"))


class AuditListingTests(unittest.TestCase):
    def row(self, resource="p", stamp="2026-11-10T20:00:00Z"):
        return {
            "actorId": None,
            "email": None,
            "resourceType": "pod",
            "resourceId": resource,
            "action": "create",
            "value": '{"name":"dummy-job"}',
            "timestamp": stamp,
        }

    def test_audit_rows_without_id_paginate_with_filters_and_413_reduction(self):
        api = Api("dummy-audit")
        responses = [
            {"auditLogs": [self.row()], "pagination": {"hasNextPage": True, "nextCursor": "c+/="}},
            ApiError(413, "GET", "/audit-logs"),
            {
                "auditLogs": [self.row("q")],
                "pagination": {"hasNextPage": False, "nextCursor": None},
            },
        ]
        with patch.object(api, "request", side_effect=responses) as request:
            rows = api.listing("/audit-logs?actions=create&search=unique+name", "auditLogs")
        self.assertEqual([r["resourceId"] for r in rows], ["p", "q"])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args.args[1]).query)
        self.assertEqual(
            query,
            {"actions": ["create"], "search": ["unique name"], "limit": ["50"], "cursor": ["c+/="]},
        )

    def test_audit_invalid_fields_and_repeated_rows_rejected(self):
        row = self.row()
        for invalid in (
            {k: v for k, v in row.items() if k != "value"},
            {**row, "value": {}},
            {**row, "actorId": 2},
            {**row, "timestamp": "bad"},
            {**row, "timestamp": "2026-11-10T20:00:00"},
            {**row, "action": ""},
        ):
            api = Api("dummy-audit-invalid")
            with (
                self.subTest(invalid=invalid),
                patch.object(
                    api,
                    "request",
                    return_value={
                        "auditLogs": [invalid],
                        "pagination": {"hasNextPage": False, "nextCursor": None},
                    },
                ),
            ):
                with self.assertRaises(RuntimeError):
                    api.listing("/audit-logs", "auditLogs")
        with patch.object(
            api,
            "request",
            return_value={
                "auditLogs": [row, row],
                "pagination": {"hasNextPage": False, "nextCursor": None},
            },
        ):
            with self.assertRaises(RuntimeError):
                api.listing("/audit-logs", "auditLogs")

    def test_resource_list_cannot_use_audit_identity_exception(self):
        api = Api("dummy-resource-id")
        with patch.object(
            api,
            "request",
            return_value={
                "pods": [self.row()],
                "pagination": {"hasNextPage": False, "nextCursor": None},
            },
        ):
            with self.assertRaises(RuntimeError):
                api.listing("/pods", "pods")


if __name__ == "__main__":
    unittest.main()
