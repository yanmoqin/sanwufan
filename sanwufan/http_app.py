"""Shared HTTP application for the local server and the production WSGI entry."""
from http.cookies import SimpleCookie
import json
from pathlib import Path
from urllib.parse import urlsplit

from .models import RuleViolation
from .storage import StorageError


ASSETS = Path(__file__).with_name("static")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/friends": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
COOKIE = "sanwufan_practice"
FRIEND_COOKIE = "sanwufan_player"


class HTTPApplication:
    def __init__(self, service):
        self.service = service

    def response(self, status, data, content_type="application/json; charset=utf-8", cookie=None, cookie_name=COOKIE):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = [("Content-Type", content_type), ("Content-Length", str(len(data))),
                   ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                   ("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'")]
        if cookie:
            secure = "; Secure" if self.service.public_origin else ""
            headers.append(("Set-Cookie", f"{cookie_name}={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000{secure}"))
        if self.service.public_origin:
            headers.append(("Strict-Transport-Security", "max-age=31536000"))
        return status, headers, data

    def error(self, status, code, message, state=None):
        body = {"error": {"code": code, "message": message}}
        if state is not None:
            body["state"] = state
        return self.response(status, body)

    def token(self, headers, name):
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
            return cookie[name].value if name in cookie else None
        except Exception:
            return None

    def handle(self, method, target, headers, body=b""):
        headers = {k.lower(): v for k, v in headers.items()}
        path = urlsplit(target).path
        if method == "GET" and path == "/healthz":
            health = {"status": "ok", "version": "1.2.1"}
            if hasattr(self.service, "local_launcher_id"):
                health["local_launcher_id"] = self.service.local_launcher_id
            return self.response(200, health)
        host = headers.get("host", "")
        public = self.service.public_origin
        port = self.service.server_address[1]
        valid = {f"{h}:{port}" for h in self.service.allowed_hosts}
        if port == 80:
            valid |= self.service.allowed_hosts
        if public:
            valid = {urlsplit(public).netloc}
        if host not in valid:
            return self.error(403, "INVALID_HOST", "请通过服务启动时允许的地址打开牌桌")
        origin = headers.get("origin")
        expected = public or f"http://{host}"
        if (origin and origin != expected) or (public and method == "POST" and origin != expected):
            return self.error(403, "INVALID_ORIGIN", "请在牌桌页面内操作")
        try:
            if method == "GET":
                if path in STATIC:
                    filename, kind = STATIC[path]
                    return self.response(200, (ASSETS / filename).read_bytes(), kind)
                if path == "/api/state":
                    token, room, created = self.service.room(self.token(headers, COOKIE), create=True)
                    return self.response(200, room.snapshot(), cookie=token if created else None)
                if path == "/api/friends/state":
                    token, _, created = self.service.friends.session(self.token(headers, FRIEND_COOKIE))
                    return self.response(200, self.service.friends.state(token), cookie=token if created else None, cookie_name=FRIEND_COOKIE)
                if path == "/favicon.ico":
                    return self.response(204, b"", "image/x-icon")
                return self.error(404, "NOT_FOUND", "页面不存在")
            if method != "POST":
                return self.error(405, "METHOD_NOT_ALLOWED", "不支持此请求方法")
            if path not in ("/api/action", "/api/friends/action"):
                return self.error(404, "NOT_FOUND", "接口不存在")
            friend = path == "/api/friends/action"
            token = self.token(headers, FRIEND_COOKIE if friend else COOKIE)
            _, room, _ = self.service.room(token) if not friend else (None, None, False)
            if (friend and token not in self.service.friends.sessions) or (not friend and room is None):
                return self.error(401, "SESSION_EXPIRED", "请刷新页面，重新入座")
            if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                return self.error(415, "INVALID_CONTENT_TYPE", "请求必须为JSON")
            try:
                length = int(headers.get("content-length", "0"))
                if not 0 < length <= 8192 or len(body) != length:
                    raise ValueError
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeError):
                return self.error(400, "INVALID_JSON", "操作请求内容无效")
            try:
                state = self.service.friends.request(token, payload) if friend else room.action(payload)
                return self.response(200, state)
            except RuleViolation as exc:
                state = self.service.friends.state(token) if friend else room.snapshot()
                return self.error(409 if exc.code == "STATE_CHANGED" else 400, exc.code, str(exc), state)
        except RuleViolation as exc:
            return self.error(503, exc.code, str(exc))
        except StorageError:
            self.service.logger.exception("checkpoint_failed")
            return self.error(503, "STORAGE_UNAVAILABLE", "保存未成功，本次操作未提交，请稍后重试")
