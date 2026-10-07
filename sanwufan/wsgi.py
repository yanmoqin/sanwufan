"""Production WSGI adapter; does not trust client-supplied proxy headers."""
from http import HTTPStatus
from urllib.parse import urlsplit

from .http_app import HTTPApplication
from .service import GameService


def validate_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or value.endswith("/")):
        raise ValueError("PUBLIC_ORIGIN需为完整HTTPS源地址，例如https://play.example.com，不含路径或末尾斜杠")
    try:
        parsed.port
    except ValueError:
        raise ValueError("PUBLIC_ORIGIN端口无效") from None
    if any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("PUBLIC_ORIGIN含无效字符")
    return value


class WSGIApplication:
    def __init__(self, service):
        self.service = service
        self.http = HTTPApplication(service)

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        headers = {key[5:].replace("_", "-"): value for key, value in environ.items() if key.startswith("HTTP_")}
        headers["Content-Type"] = environ.get("CONTENT_TYPE", "")
        headers["Content-Length"] = environ.get("CONTENT_LENGTH", "0")
        body = b""
        try:
            try:
                length = int(headers["Content-Length"] or "0")
            except ValueError:
                length = 0
            if method == "POST" and 0 < length <= 8192:
                body = environ["wsgi.input"].read(length)
            if self.service.closing:
                response = self.http.error(503, "SERVICE_STOPPING", "服务正在重启，请稍后重试")
            else:
                response = self.http.handle(method, path, headers, body)
        except Exception:
            self.service.logger.exception("http_request_failed method=%s", method)
            response = self.http.error(500, "INTERNAL_ERROR", "服务暂时无法处理，请稍后重试")
        status, response_headers, data = response
        # No query strings, cookies, nicknames, or hand contents in access logs.
        if method == "POST" or status >= 400:
            route = path if path in ("/api/action", "/api/friends/action", "/healthz") else "other"
            self.service.logger.info("request method=%s route=%s status=%d", method, route, status)
        start_response(f"{status} {HTTPStatus(status).phrase}", response_headers)
        return [data]

    def close(self):
        self.service.close()


def create_app(database, public_origin, host="127.0.0.1", port=8080):
    origin = validate_origin(public_origin) if public_origin else None
    return WSGIApplication(GameService((host, port), database=database, public_origin=origin))
