"""A loopback HTTP front for FakeApi, so that real processes can use the production REST client.

Moved here from test_safety.HttpAndCrashTests, whose setUp the Windows launcher tests also borrowed.
Error responses carry a secret-looking detail that must never reach a message or a log.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from podvigil.podrt.api import ApiError
from tests.support.fakes import FakeApi
from tests.support.jobs import fixture


class LoopbackApi:
    """FakeApi(job) behind http://127.0.0.1:<port>. Use start() and stop(), or a with block."""

    def __init__(self, job=None):
        self.job = job or fixture()
        self.api = FakeApi(self.job)
        fake = self.api

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.handle_request("GET")

            def do_DELETE(self):
                self.handle_request("DELETE")

            def handle_request(self, method):
                try:
                    data = fake.request(method, self.path)
                    self.send_response(200 if data is not None else 204)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    if data is not None:
                        self.wfile.write(json.dumps(data).encode())
                except ApiError as exc:
                    self.send_response(exc.status)
                    self.end_headers()
                    self.wfile.write(b'{"detail":"api_key=SECRET_MUST_NOT_APPEAR"}')

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        if self.thread.is_alive():
            self.server.shutdown()  # Waits for serve_forever, so only after start().
        self.server.server_close()
        self.thread.join(timeout=2)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
