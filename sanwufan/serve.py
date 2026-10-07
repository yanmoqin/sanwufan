"""Production entry: python -m sanwufan.serve, behind the supplied HTTPS proxy."""
import argparse
import logging
import os
import signal

from .storage import StorageError
from .wsgi import create_app


def main():
    parser = argparse.ArgumentParser(description="启动三五反部署服务")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--database", default=os.environ.get("DATABASE", "data/sanwufan.sqlite3"))
    parser.add_argument("--public-origin", default=os.environ.get("PUBLIC_ORIGIN", ""))
    parser.add_argument("--local-test", action="store_true", help="仅本机HTTP部署入口测试")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须为1到65535")
    if not args.public_origin and not (args.local_test and args.host == "127.0.0.1"):
        parser.error("部署模式必须设置PUBLIC_ORIGIN；仅本机验证可用--local-test")
    if args.local_test and args.public_origin:
        parser.error("local-test不能同时设置public-origin")
    try:
        from waitress import create_server
    except ImportError:
        parser.exit(1, "缺少部署依赖，请运行 python -m pip install -r requirements-server.txt\n")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        app = create_app(args.database, args.public_origin, args.host, args.port)
    except (OSError, StorageError, ValueError) as exc:
        parser.exit(1, f"无法启动部署服务：{exc}\n")
    server = None
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        server = create_server(app, host=args.host, port=args.port, threads=8,
                               connection_limit=256, channel_timeout=30,
                               max_request_header_size=16384, max_request_body_size=8192,
                               expose_tracebacks=False, clear_untrusted_proxy_headers=True)
        logging.getLogger("sanwufan").info("service_started persistent=true rooms=%d", len(app.service.friends.rooms))
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.close()
            server.task_dispatcher.shutdown()
        app.close()


if __name__ == "__main__":
    main()
