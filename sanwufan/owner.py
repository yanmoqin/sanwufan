"""Loopback-only launcher control, separate from ordinary game clients."""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit


def management_url(status_path, fallback):
    try:
        url = json.loads(status_path.read_text(encoding="utf-8")).get("manager_url")
        parsed = urlsplit(url)
        if (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port and
                not parsed.username and not parsed.password and not parsed.query and
                parsed.path == "/" and len(parsed.fragment) == 43):
            return url
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return fallback


class OwnerControl:
    def __init__(self, game_url, stop, grace=15):
        self.game_url, self.stop, self.grace = game_url, stop, grace
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.streams = 0
        self.deadline = None
        self.closed = threading.Event()
        control = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def reply(self, status, body=b"", content_type="application/json", cookie=False):
                self.close_connection = True
                self.send_response(status)
                self.send_header("Connection", "close")
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                if cookie:
                    self.send_header("Set-Cookie", f"{control.cookie}={control.token}; HttpOnly; SameSite=Strict; Path=/")
                self.end_headers()
                self.wfile.write(body)

            def trusted_host(self):
                return self.headers.get("Host") == control.origin.removeprefix("http://")

            def authorized(self):
                try:
                    cookies = SimpleCookie(self.headers.get("Cookie", ""))
                    value = cookies.get(control.cookie)
                    return bool(value and secrets.compare_digest(value.value, control.token))
                except Exception:
                    return False

            def do_GET(self):
                if not self.trusted_host():
                    self.reply(403)
                    return
                assets = {"/": ("owner.html", "text/html; charset=utf-8"),
                          "/owner.js": ("owner.js", "text/javascript; charset=utf-8"),
                          "/owner.css": ("owner.css", "text/css; charset=utf-8")}
                if self.path in assets:
                    filename, mime = assets[self.path]
                    self.reply(200, (Path(__file__).parent / "static" / filename).read_bytes(), mime)
                    return
                if self.path != "/events":
                    self.reply(404)
                    return
                if not self.authorized() or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
                    self.reply(403)
                    return
                # A live stream survives background timer throttling. Only losing
                # the last authenticated management page starts the grace period.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.connection.settimeout(3)
                control.connected()
                try:
                    while not control.closed.is_set():
                        self.wfile.write(b"data: running\n\n")
                        self.wfile.flush()
                        control.closed.wait(0.5)
                except (OSError, TimeoutError):
                    pass
                finally:
                    control.disconnected()
                    self.close_connection = True

            def do_POST(self):
                if not self.trusted_host() or self.headers.get("Origin") != control.origin:
                    self.reply(403)
                    return
                self.connection.settimeout(3)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1024:
                        self.reply(400)
                        return
                    body = json.loads(self.rfile.read(length))
                except (OSError, ValueError):
                    self.reply(400)
                    return
                if self.path == "/session":
                    token = body.get("token") if isinstance(body, dict) else None
                    if not isinstance(token, str) or not token.isascii() or not secrets.compare_digest(token, control.token):
                        self.reply(403)
                        return
                    self.reply(200, json.dumps({"game_url": control.game_url, "grace": control.grace}).encode(), cookie=True)
                elif self.path == "/stop" and self.authorized():
                    self.reply(200, b'{"stopped":true}')
                    control.request_stop()
                else:
                    self.reply(403)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.cookie = f"swf_owner_{self.server.server_port}"
        self.url = self.origin + "/#" + self.token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self.monitor, daemon=True).start()

    def connected(self):
        with self.lock:
            self.streams += 1
            self.deadline = None

    def disconnected(self):
        with self.lock:
            self.streams -= 1
            if not self.streams and not self.closed.is_set():
                self.deadline = time.monotonic() + self.grace

    def monitor(self):
        while not self.closed.wait(0.1):
            with self.lock:
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    self.closed.set()
            if self.closed.is_set():
                self.stop()

    def request_stop(self):
        with self.lock:
            if self.closed.is_set():
                return
            self.closed.set()
        self.stop()

    def close(self):
        self.closed.set()
        self.server.shutdown()
        self.server.server_close()
