"""Local HTTP server; production deployments use sanwufan.wsgi with Waitress."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import logging
from pathlib import Path

from .http_app import ASSETS, STATIC, COOKIE, FRIEND_COOKIE, HTTPApplication
from .service import GameService
from .storage import StorageError


class PracticeServer(GameService, ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, seed=None, allowed_hosts=(), database=None, public_origin=None):
        ThreadingHTTPServer.__init__(self, address, PracticeHandler)
        try:
            GameService.__init__(self, self.server_address, seed, allowed_hosts, database, public_origin)
            self.http = HTTPApplication(self)
        except Exception:
            ThreadingHTTPServer.server_close(self)
            raise

    def server_close(self):
        try:
            # HTTPServer also calls this when binding fails, before game setup.
            if hasattr(self, "closing"):
                GameService.close(self)
        finally:
            ThreadingHTTPServer.server_close(self)


class PracticeHandler(BaseHTTPRequestHandler):
    server_version = "Sanwufan/0.4"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        pass

    def _handle(self):
        body = b""
        if self.command == "POST":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if 0 < length <= 8192:
                    body = self.rfile.read(length)
            except (ValueError, TimeoutError, OSError):
                pass
        status, headers, data = self.server.http.handle(self.command, self.path, dict(self.headers.items()), body)
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle


def main():
    parser = argparse.ArgumentParser(description="启动三五反练习桌与四人好友房")
    parser.add_argument("--port", type=int, default=8765, help="本机端口，默认8765")
    parser.add_argument("--seed", type=int, help="仅用于复现测试")
    parser.add_argument("--host", default="127.0.0.1", help="监听IPv4地址；局域网可设置0.0.0.0")
    parser.add_argument("--allow-host", action="append", default=[], help="允许访问的局域网IPv4地址，可重复")
    parser.add_argument("--database", default="data/sanwufan.sqlite3", help="好友房存档路径，默认data/sanwufan.sqlite3")
    parser.add_argument("--no-persist", action="store_true", help="仅本次内存试玩，不写存档")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须为1到65535")
    try:
        ipaddress.IPv4Address(args.host)
        for host in args.allow_host:
            ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        parser.error("host和allow-host必须为IPv4地址")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        server = PracticeServer((args.host, args.port), args.seed, args.allow_host, None if args.no_persist else args.database)
    except (OSError, StorageError) as exc:
        parser.exit(1, f"无法启动牌桌：{exc}\n")
    print(f"三五反牌桌：http://127.0.0.1:{args.port}\n四人好友房：http://127.0.0.1:{args.port}/friends", flush=True)
    for host in args.allow_host:
        print(f"好友访问：http://{host}:{args.port}/friends", flush=True)
    print("好友房存档：" + ("关闭" if args.no_persist else str(Path(args.database).resolve())), flush=True)
    print("按 Ctrl+C 停止。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
