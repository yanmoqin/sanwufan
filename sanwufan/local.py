"""Desktop launcher with an isolated save and safe reuse on a second click."""
import argparse
import errno
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
import uuid
import webbrowser

from .remote import LaunchLock
from .owner import OwnerControl, management_url
from .storage import StorageError
from .web import PracticeServer


def running_url(status_path):
    try:
        state = json.loads(status_path.read_text(encoding="utf-8"))
        url = state["url"]
        parsed = urlsplit(url)
        if (state.get("state") != "ready" or parsed.scheme != "http" or
                parsed.hostname != "127.0.0.1" or not parsed.port or
                parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            return None
        with build_opener(ProxyHandler({})).open(url + "/healthz", timeout=1) as response:
            health = json.load(response)
        if health.get("status") == "ok" and health.get("local_launcher_id") == state["instance"]:
            return url
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="启动本机三五反牌桌；重复启动会打开已有牌桌")
    parser.add_argument("--port", type=int, default=8766, help="首选端口，默认8766；占用时自动选择空闲端口")
    parser.add_argument("--data-dir", type=Path, default=Path("data/local"), help="本机启动器独立存档目录")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("端口必须为0到65535")
    data = args.data_dir.resolve()
    lock = server = control = None
    status_path = data / "status.json"
    try:
        data.mkdir(parents=True, exist_ok=True)
        try:
            lock = LaunchLock(data / "launcher.lock")
        except OSError:
            for _ in range(20):
                url = running_url(status_path)
                if url:
                    print("本机牌桌已在运行：" + url, flush=True)
                    if not args.no_browser:
                        webbrowser.open(management_url(status_path, url))
                    return 0
                time.sleep(0.25)
            raise OSError("本机启动器已占用，尚未找到可用牌桌。请检查原启动窗口或目录权限。")
        status_path.write_text('{"state":"starting"}', encoding="utf-8")
        try:
            server = PracticeServer(("127.0.0.1", args.port), database=data / "games.sqlite3")
        except OSError as exc:
            if not args.port or (exc.errno not in (errno.EADDRINUSE, errno.EACCES) and
                                 getattr(exc, "winerror", None) not in (10048, 10013)):
                raise
            server = PracticeServer(("127.0.0.1", 0), database=data / "games.sqlite3")
        server.local_launcher_id = uuid.uuid4().hex
        url = f"http://127.0.0.1:{server.server_address[1]}"
        control = OwnerControl(url, server.shutdown)
        temporary = status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"state": "ready", "url": url, "instance": server.local_launcher_id,
                                        "manager_url": control.url}), encoding="utf-8")
        temporary.replace(status_path)
        print("三五反牌桌：" + url + "\n本机好友房：" + url + "/friends", flush=True)
        print("本机独立存档：" + str(data / "games.sqlite3"), flush=True)
        print("服务管理页：" + control.url, flush=True)
        print("打牌时保留管理页；关闭最后一个管理页约15秒后退出。也可按 Ctrl+C 停止。", flush=True)
        if not args.no_browser:
            webbrowser.open(control.url)
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, StorageError) as exc:
        print("无法启动本机牌桌：" + str(exc), flush=True)
        return 1
    finally:
        if control:
            control.close()
        if server:
            server.server_close()
        if lock:
            try:
                status_path.write_text('{"state":"stopped"}', encoding="utf-8")
            finally:
                lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
