from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
import urllib.parse

from podvigil.podrt.api import Api, ApiError, LIST_PAGE_LIMIT


def page(entries, has_next=False, cursor=None):
    return {"pods": entries, "pagination": {"hasNextPage": has_next, "nextCursor": cursor}}


class FakeTransport(Api):
    def __init__(self, transport):
        self.transport = transport
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.transport(method, path, body)


class HttpContractTests(unittest.TestCase):
    def setUp(self):
        self.status = 200
        self.body = b""
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append(self.path)
                payload = (
                    owner.body if isinstance(owner.body, bytes) else json.dumps(owner.body).encode()
                )
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Api("dummy-key", f"http://127.0.0.1:{self.server.server_port}", timeout=2)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def respond(self, status, body=b""):
        self.status, self.body = status, body

    def test_get_or_none_accepts_only_matching_object_or_404(self):
        self.respond(200, {"id": "expected", "name": "owned"})
        self.assertEqual(self.client.get_or_none("/pods/expected")["name"], "owned")

        self.respond(404, {"detail": "not found"})
        self.assertIsNone(self.client.get_or_none("/pods/expected"))

        for status in (400, 401, 403, 409, 413, 422, 429, 500):
            with self.subTest(status=status):
                self.respond(status, {"detail": "must not be reflected"})
                with self.assertRaises(ApiError) as caught:
                    self.client.get_or_none("/pods/expected")
                self.assertEqual(caught.exception.status, status)

    def test_get_or_none_rejects_empty_null_and_malformed_success(self):
        for body in (b"", b"null", b"{}", b"[]", b'{"id":"different"}'):
            with self.subTest(body=body):
                self.respond(200, body)
                with self.assertRaisesRegex(RuntimeError, "absence is not verified"):
                    self.client.get_or_none("/pods/expected")

        self.respond(200, b"{")
        with self.assertRaises(ApiError) as caught:
            self.client.get_or_none("/pods/expected")
        self.assertEqual(caught.exception.status, 0)


class ListingContractTests(unittest.TestCase):
    @staticmethod
    def query(path):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(path).query, keep_blank_values=True)

    def test_413_halves_limit_without_losing_cursor_or_filters(self):
        cursor = "opaque+/="

        def transport(method, path, body):
            self.assertEqual(method, "GET")
            query = self.query(path)
            limit = int(query["limit"][0])
            current = query.get("cursor", [None])[0]
            if current is None:
                return page([{"id": "pod-1"}], True, cursor)
            self.assertEqual(current, cursor)
            if limit > 25:
                raise ApiError(413, method, path)
            return page([{"id": "pod-2"}])

        api = FakeTransport(transport)
        result = api.listing("/pods?cloud=SECURE&tag=alpha&tag=beta&blank=", "pods")

        self.assertEqual([item["id"] for item in result], ["pod-1", "pod-2"])
        queries = [self.query(path) for method, path, body in api.calls]
        self.assertEqual(
            [int(query["limit"][0]) for query in queries],
            [LIST_PAGE_LIMIT, LIST_PAGE_LIMIT, 50, 25],
        )
        self.assertNotIn("cursor", queries[0])
        self.assertEqual([query["cursor"] for query in queries[1:]], [[cursor], [cursor], [cursor]])
        for query in queries:
            self.assertEqual(query["cloud"], ["SECURE"])
            self.assertEqual(query["tag"], ["alpha", "beta"])
            self.assertEqual(query["blank"], [""])

    def test_fully_paginates_in_order(self):
        pages = {
            None: page([{"id": "pod-1"}, {"id": "pod-2"}], True, "next-1"),
            "next-1": page([{"id": "pod-3"}], True, "next-2"),
            "next-2": page([]),
        }

        def transport(method, path, body):
            query = self.query(path)
            self.assertEqual(query["limit"], [str(LIST_PAGE_LIMIT)])
            return pages[query.get("cursor", [None])[0]]

        api = FakeTransport(transport)
        result = api.listing("/pods", "pods")
        self.assertEqual([item["id"] for item in result], ["pod-1", "pod-2", "pod-3"])
        self.assertEqual(len(api.calls), 3)

    def test_rejects_malformed_pages_entries_and_cursors(self):
        valid_last = {"hasNextPage": False, "nextCursor": None}
        cases = [
            None,
            [],
            {},
            {"pods": {}, "pagination": valid_last},
            {"pods": [None], "pagination": valid_last},
            {"pods": [{}], "pagination": valid_last},
            {"pods": [{"id": ""}], "pagination": valid_last},
            {"pods": [{"id": 7}], "pagination": valid_last},
            {"pods": [{"id": "same"}, {"id": "same"}], "pagination": valid_last},
            {"pods": [], "pagination": []},
            {"pods": [], "pagination": {"nextCursor": None}},
            {"pods": [], "pagination": {"hasNextPage": 0, "nextCursor": None}},
            {"pods": [], "pagination": {"hasNextPage": False}},
            {"pods": [], "pagination": {"hasNextPage": False, "nextCursor": "extra"}},
            {"pods": [], "pagination": {"hasNextPage": True, "nextCursor": None}},
            {"pods": [], "pagination": {"hasNextPage": True, "nextCursor": ""}},
            {"pods": [], "pagination": {"hasNextPage": True, "nextCursor": 3}},
        ]
        for response in cases:
            with self.subTest(response=response):
                api = FakeTransport(lambda method, path, body, value=response: value)
                with self.assertRaisesRegex(RuntimeError, "incomplete resource inventory"):
                    api.listing("/pods", "pods")

    def test_rejects_repeated_cursor_and_duplicate_id_across_pages(self):
        for second in (page([{"id": "pod-2"}], True, "again"), page([{"id": "pod-1"}])):
            responses = iter((page([{"id": "pod-1"}], True, "again"), second))
            api = FakeTransport(lambda method, path, body: next(responses))
            with (
                self.subTest(second=second),
                self.assertRaisesRegex(RuntimeError, "incomplete resource inventory"),
            ):
                api.listing("/pods", "pods")

    def test_non_413_statuses_propagate_and_413_at_limit_one_stops(self):
        for status in (400, 401, 403, 404, 409, 422, 429, 500):

            def fail(method, path, body, error_status=status):
                raise ApiError(error_status, method, path)

            api = FakeTransport(fail)
            with self.subTest(status=status), self.assertRaises(ApiError) as caught:
                api.listing("/pods", "pods")
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(len(api.calls), 1)

        def too_large(method, path, body):
            raise ApiError(413, method, path)

        api = FakeTransport(too_large)
        with self.assertRaises(ApiError) as caught:
            api.listing("/pods", "pods")
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(
            [int(self.query(path)["limit"][0]) for method, path, body in api.calls],
            [100, 50, 25, 12, 6, 3, 1],
        )


if __name__ == "__main__":
    unittest.main()
