"""Real Waitress process, four cookie jars, abrupt restart, and live backup."""
from http.cookiejar import CookieJar
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from sanwufan.storage import SQLiteStore
from sanwufan.friends import FriendRooms
from sanwufan.practice import DEAL_INTERVAL


@unittest.skipUnless(importlib.util.find_spec("waitress"), "安装requirements-server.txt后运行部署入口测试")
class ServeProcessTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = Path(self.folder.name) / "server.sqlite3"
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            self.port = bound.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.clients = [build_opener(HTTPCookieProcessor(CookieJar())) for _ in range(4)]
        self.process = None
        self.start()

    def tearDown(self):
        self.kill()
        self.folder.cleanup()

    def start(self):
        self.process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "sanwufan.serve", "--local-test",
                    "--port", str(self.port), "--database", str(self.database)],
                    cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with self.clients[0].open(self.base + "/healthz", timeout=1) as response:
                    self.assertEqual(json.load(response)["status"], "ok")
                    return
            except (URLError, TimeoutError):
                if self.process.poll() is not None:
                    break
                time.sleep(0.05)
        _, error = self.process.communicate(timeout=2)
        self.fail("部署服务未启动：" + error.decode("utf-8", errors="replace"))

    def kill(self):
        if self.process:
            if self.process.poll() is None:
                self.process.kill()
            self.process.communicate(timeout=5)
            self.process = None

    def state(self, p):
        with self.clients[p].open(self.base + "/api/friends/state", timeout=3) as response:
            return json.load(response)

    def send(self, p, payload):
        request = Request(self.base + "/api/friends/action", data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with self.clients[p].open(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def action(self, p, action, view=None):
        view = view or self.state(p)
        payload = {"action": action, "table_id": view["table_id"], "version": view["version"],
                   "cards": view["hint"]["cards"] if view["hint"] and view["hint"]["action"] == action else []}
        status, result = self.send(p, payload)
        self.assertEqual(status, 200, result)
        return result, payload

    def test_four_clients_keep_private_hands_and_reject_replay_after_process_kill(self):
        for p in range(4):
            self.assertEqual(self.state(p)["mode"], "lobby")
        status, view = self.send(0, {"command": "create", "name": "房主"})
        self.assertEqual(status, 200)
        for p in range(1, 4):
            self.assertEqual(self.send(p, {"command": "join", "name": f"朋友{p}", "room_code": view["room_code"]})[0], 200)
        for p in range(4):
            self.action(p, "ready")
        deadline = time.monotonic() + 48 * DEAL_INTERVAL + 12
        while time.monotonic() < deadline:
            views = [self.state(p) for p in range(4)]
            if views[0]["phase"] == "declaring":
                break
            for p, v in enumerate(views):
                if "call_trump" in v["available_actions"]:
                    self.action(p, "call_trump", v)
                    break
            time.sleep(0.1)
        self.assertEqual(self.state(0)["phase"], "declaring")
        for p in range(4):
            self.action(p, "confirm")
        dealer = self.state(0)["dealer"]
        self.action(dealer, "take_bottom")
        self.action(dealer, "discard")
        played, replay = self.action(dealer, "play")
        before = [self.state(p) for p in range(4)]
        backup = Path(self.folder.name) / "live-backup.sqlite3"
        completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "sanwufan.backup", str(self.database), str(backup)],
                        capture_output=True, text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        copied = FriendRooms(store=SQLiteStore(backup))
        try:
            saved = next(iter(copied.rooms.values()))
            self.assertEqual(saved.version, played["version"])
            self.assertEqual(saved.game.phase.value, "playing")
        finally:
            copied.close()
        self.kill()
        self.start()
        restored = [self.state(p) for p in range(4)]
        for p in range(4):
            for key in ("room_code", "table_id", "version", "player_seat", "hand", "hand_counts", "current_plays", "team_points"):
                self.assertEqual(restored[p][key], before[p][key])
            self.assertNotIn("hands", restored[p])
            self.assertNotIn("stock", restored[p])
        self.assertEqual(self.send(dealer, replay)[0], 409)
        self.assertEqual(self.state(dealer)["version"], played["version"])


if __name__ == "__main__":
    unittest.main()
