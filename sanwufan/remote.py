"""One-click temporary HTTPS play, with an isolated database and owned processes."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser
from .owner import OwnerControl, management_url


URL = re.compile(r"https://[a-z0-9-]+\.tunnelmole\.(?:net|com)")


def registered_origin(line):
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("type") != "sanwufan.tunnel.ready":
        return None
    found = URL.fullmatch(event.get("origin", "")) if isinstance(event.get("origin"), str) else None
    return found.group() if found else None


class WindowsJob:
    """Closing the launcher console also kills only its game and tunnel children."""
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise OSError("无法建立启动进程管理，请重新启动后再试")

    def add(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            process.kill()
            process.wait()
            raise OSError("无法管理后台进程，已停止启动")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class LaunchLock:
    def __init__(self, path):
        self.file = path.open("a+b")
        try:
            self.file.seek(0)
            if not self.file.read(1):
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise OSError("远程试玩已在运行，请使用已有窗口中的网址") from None

    def close(self):
        self.file.close()


def free_port():
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        return bound.getsockname()[1]


def existing_invitation(data):
    try:
        state = json.loads((data / "status.json").read_text(encoding="utf-8"))
        origin = registered_origin(json.dumps({"type": "sanwufan.tunnel.ready", "origin": state.get("origin")}))
        if state.get("state") == "ready" and origin and healthy(origin):
            return origin + "/friends"
    except (OSError, ValueError, TypeError):
        pass
    return None


def healthy(base, timeout=5, local=False):
    opener = build_opener(ProxyHandler({})) if local else build_opener()
    try:
        with opener.open(Request(base + "/healthz"), timeout=timeout) as response:
            return response.status == 200 and json.load(response).get("status") == "ok"
    except (OSError, ValueError):
        return False


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="启动临时公网三五反牌桌")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=root / "data" / "remote")
    args = parser.parse_args()
    data = args.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=True)
    lock = job = control = None
    children, logs, readers = [], [], []
    status = data / "status.json"
    invite_file = root / "试玩网址.txt"
    events = queue.Queue()
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    origin = None
    def save_status(state):
        temporary = status.with_suffix(".tmp")
        temporary.write_text(json.dumps({"state": state, "origin": origin,
                                        "manager_url": control.url if control else None,
                                        "launcher_pid": os.getpid(), "child_pids": [p.pid for p in children]},
                                       ensure_ascii=False), encoding="utf-8")
        temporary.replace(status)
    def start(command, name, read=False):
        stream = (data / name).open("w", encoding="utf-8")
        logs.append(stream)
        process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE if read else stream, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        children.append(process)
        job.add(process)
        if read:
            def consume():
                for raw in iter(process.stdout.readline, b""):
                    line = raw.decode("utf-8", errors="replace")
                    stream.write(line)
                    stream.flush()
                    found = registered_origin(line)
                    if found:
                        events.put(found)
                process.stdout.close()
            reader = threading.Thread(target=consume, daemon=True)
            readers.append(reader)
            reader.start()
        return process
    try:
        try:
            lock = LaunchLock(data / "launcher.lock")
        except OSError:
            invitation = existing_invitation(data)
            if not invitation:
                raise
            print("远程试玩已在运行：\n" + invitation, flush=True)
            if not args.no_browser:
                webbrowser.open(management_url(status, invitation))
            return 0
        job = WindowsJob()
        invite_file.unlink(missing_ok=True)
        port = free_port()
        command = [sys.executable, "-X", "utf8", "-m", "sanwufan.tunnel", "--port", str(port),
                   "--identity", str(data / "tunnel_identity.txt")]
        print("正在连接远程试玩通道，请稍等……")
        for attempt in range(3):
            tunnel = start(command, "tunnel.log", read=True)
            save_status("connecting")
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and not stopped.is_set():
                if tunnel.poll() is not None:
                    break
                try:
                    origin = events.get(timeout=0.25)
                    break
                except queue.Empty:
                    continue
            if origin or stopped.is_set():
                break
            if tunnel.poll() is None:
                tunnel.terminate()
                tunnel.wait(timeout=5)
            if attempt < 2:
                print("连接暂未成功，正在重试……")
                stopped.wait(1)
        if stopped.is_set():
            return 0
        if not origin:
            raise OSError("远程通道未能连接，请检查网络后重试。详情见 data/remote/tunnel.log。")
        save_status("checking")
        server = start([sys.executable, "-X", "utf8", "-m", "sanwufan.serve", "--host", "127.0.0.1",
                        "--port", str(port), "--database", str(data / "games.sqlite3"),
                        "--public-origin", origin], "game.log")
        deadline = time.monotonic() + 15
        while not healthy(f"http://127.0.0.1:{port}", timeout=1, local=True):
            if server.poll() is not None or time.monotonic() > deadline:
                raise OSError("牌桌服务未能启动。请查看 data/remote/game.log，存档已保留。")
            if stopped.wait(0.2):
                return 0
        print("正在检查邀请网址是否能访问……")
        deadline = time.monotonic() + 60
        while not healthy(origin):
            if server.poll() is not None or tunnel.poll() is not None or time.monotonic() > deadline:
                raise OSError("邀请网址暂时无法访问，请稍后重试；没有发布不可用的邀请。")
            if stopped.wait(1):
                return 0
        invitation = origin + "/friends"
        control = OwnerControl(invitation, stopped.set)
        invite_file.write_text(invitation + "\n\n先创建好友房，再复制页面里的房间邀请发给朋友。\n保持启动窗口和电脑运行。\n", encoding="utf-8-sig")
        save_status("ready")
        print("\n牌桌已启动：\n" + invitation)
        print("\n创建房间后点击“复制邀请”，发给朋友即可。")
        print("请保存页面里的个人恢复码，网址变更后可恢复原座位。")
        print("保持电脑联网、不要休眠。按 Ctrl+C 或关闭此窗口结束试玩。")
        print("服务管理页：" + control.url)
        print("打牌时保留管理页；关闭最后一个管理页约15秒后退出游戏与通道。")
        if not args.no_browser:
            webbrowser.open(control.url)
        while not stopped.wait(0.5):
            if server.poll() is not None or tunnel.poll() is not None:
                raise OSError("远程试玩连接已结束。重新双击启动并分享新网址；可用个人恢复码找回座位。")
            while not events.empty():
                if events.get_nowait() != origin:
                    raise OSError("临时网址已变更。请重新启动并分享新网址，使用个人恢复码恢复座位。")
        return 0
    except KeyboardInterrupt:
        print("\n正在关闭远程试玩……")
        return 0
    except (OSError, ValueError) as exc:
        print("\n" + str(exc))
        return 1
    finally:
        if control:
            control.close()
        if lock:
            # Stop the public entrance before the application. Windows Job also
            # handles abrupt console closure, when this finally cannot run.
            for process in children:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if job:
                job.close()
            for reader in readers:
                reader.join(timeout=2)
            for stream in logs:
                stream.close()
            invite_file.unlink(missing_ok=True)
            save_status("stopped")
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
