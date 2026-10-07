import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from sanwufan.tunnel import forward_request


class TunnelProxyTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        calls = self.calls
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", 0))
                calls.append((self.path, dict(self.headers), self.rfile.read(size)))
                self.send_response(200)
                self.send_header("Set-Cookie", "a=first; Secure; HttpOnly")
                self.send_header("Set-Cookie", "b=second; Secure; HttpOnly")
                self.end_headers()
                self.wfile.write("恢复座位".encode())
            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, **changes):
        message = {"requestId": "one", "url": "/api/friends/action", "method": "POST",
                   "body": base64.b64encode(b'{"command":"ready"}').decode(),
                   "headers": {"Host": "abc.tunnelmole.net", "Origin": "https://abc.tunnelmole.net",
                               "Cookie": "sanwufan_player=test", "Content-Type": "application/json"}}
        message.update(changes)
        return forward_request(message, self.server.server_address[1])

    def test_forwarding_preserves_origin_cookie_bytes_and_multiple_response_cookies(self):
        result = self.request()
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(base64.b64decode(result["body"]).decode(), "恢复座位")
        self.assertEqual(len(result["headers"]["set-cookie"]), 2)
        self.assertEqual(self.calls[0][1]["Host"], "abc.tunnelmole.net")
        self.assertEqual(self.calls[0][1]["Origin"], "https://abc.tunnelmole.net")
        self.assertEqual(self.calls[0][1]["Cookie"], "sanwufan_player=test")
        self.assertEqual(self.calls[0][2], b'{"command":"ready"}')

    def test_absolute_urls_and_oversized_bodies_never_reach_local_service(self):
        self.assertEqual(self.request(url="https://evil.example/api/friends/action")["statusCode"], 400)
        self.assertEqual(self.request(url="//evil.example/api")["statusCode"], 400)
        self.assertEqual(self.request(body=base64.b64encode(b'x' * 8193).decode())["statusCode"], 413)
        self.assertEqual(self.calls, [])

    def test_unavailable_upstream_returns_gateway_error(self):
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual(self.request()["statusCode"], 502)
