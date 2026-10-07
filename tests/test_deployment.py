from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sanwufan.http_app import FRIEND_COOKIE
from sanwufan.storage import StorageError
from sanwufan.wsgi import create_app, validate_origin


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "game.sqlite3"
        self.origin = "https://play.example.com"
        self.app = create_app(self.path, self.origin)
        self.app.service.stop_ticks.set()
        self.app.service.ticker.join(2)

    def tearDown(self):
        self.app.close()
        self.folder.cleanup()

    def request(self, method="GET", path="/api/friends/state", cookie=None, payload=None, host="play.example.com", origin=None, extra=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "HTTP_HOST": host,
               "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(body)), "wsgi.input": BytesIO(body)}
        if cookie:
            env["HTTP_COOKIE"] = cookie
        if origin:
            env["HTTP_ORIGIN"] = origin
        env.update(extra or {})
        meta = {}
        def start(status, headers):
            meta.update(status=int(status.split()[0]), headers=dict(headers))
        data = b"".join(self.app(env, start))
        return meta["status"], meta["headers"], json.loads(data)

    def session(self):
        status, headers, state = self.request()
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_https_cookie_is_secure_persistent_and_not_client_proxy_controlled(self):
        status, headers, state = self.request(extra={"HTTP_X_FORWARDED_PROTO": "http"})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Max-Age=2592000"):
            self.assertIn(flag, cookie)
        self.assertIn("Strict-Transport-Security", headers)

    def test_origin_is_required_and_host_proxy_spoof_is_rejected(self):
        cookie = self.session()
        payload = {"command": "create", "name": "测试"}
        for origin in (None, "https://evil.example", "http://play.example.com"):
            self.assertEqual(self.request("POST", "/api/friends/action", cookie, payload, origin=origin)[0], 403)
        self.assertEqual(self.request("POST", "/api/friends/action", cookie, payload, origin=self.origin)[0], 200)
        self.assertEqual(self.request(host="evil.example", extra={"HTTP_X_FORWARDED_HOST": "play.example.com"})[0], 403)

    def test_storage_error_returns_503_and_no_acknowledged_seat(self):
        cookie = self.session()
        token = cookie.split("=", 1)[1]
        with patch.object(self.app.service.friends.store, "write", side_effect=StorageError("disk full")):
            with self.assertLogs("sanwufan", "ERROR"):
                status, _, result = self.request("POST", "/api/friends/action", cookie,
                       {"command": "create", "name": "测试"}, origin=self.origin)
        self.assertEqual(status, 503)
        self.assertEqual(result["error"]["code"], "STORAGE_UNAVAILABLE")
        self.assertEqual(self.app.service.friends.state(token)["mode"], "lobby")

    def test_restart_keeps_cookie_room_and_version(self):
        cookie = self.session()
        _, _, original = self.request("POST", "/api/friends/action", cookie,
                        {"command": "create", "name": "测试"}, origin=self.origin)
        self.app.close()
        self.app = create_app(self.path, self.origin)
        status, headers, restored = self.request(cookie=cookie)
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", headers)
        for key in ("table_id", "version", "room_code", "player_seat", "is_owner"):
            self.assertEqual(restored[key], original[key])

    def test_health_and_malformed_request_do_not_leak_state(self):
        status, _, body = self.request(path="/healthz", host="127.0.0.1:8080")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"status", "version"})
        cookie = self.session()
        for headers in ({"CONTENT_LENGTH": "no"}, {"CONTENT_LENGTH": "8193"}):
            status, _, body = self.request("POST", "/api/friends/action", cookie, {}, origin=self.origin, extra=headers)
            self.assertEqual(status, 400)
        self.assertEqual(self.request("DELETE")[0], 405)

    def test_invalid_public_origins_fail_before_startup(self):
        for origin in ("", "http://play.example.com", "https://play.example.com/", "https://a@b.com", "https://b.com/path", "https://b.com:bad", "https://b.com\n"):
            with self.assertRaises(ValueError):
                validate_origin(origin)
        self.assertEqual(validate_origin("https://play.example.com:8443"), "https://play.example.com:8443")


if __name__ == "__main__":
    unittest.main()
