import json
import http.client
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit

from sanwufan.local import running_url

ROOT = Path(__file__).resolve().parents[1]
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class LocalLauncherTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows launch scripts')
    def test_closing_management_page_exits_actual_cmd_and_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            command = f'"{os.environ["ComSpec"]}" /d /c 启动牌桌.cmd --no-browser --port 0 --data-dir "{folder}"'
            log_path = Path(folder) / 'process.log'
            with log_path.open('wb') as log:
                process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT, creationflags=FLAGS)
            try:
                deadline = time.monotonic() + 10
                state = None
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        state = json.loads((Path(folder) / 'status.json').read_text())
                        if state.get('state') == 'ready':
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(.05)
                self.assertIsNotNone(state, log_path.read_text(encoding='utf-8'))
                self.assertEqual(state['state'], 'ready', log_path.read_text(encoding='utf-8'))
                url = urlsplit(state['manager_url'])
                origin = f'http://127.0.0.1:{url.port}'
                client = http.client.HTTPConnection('127.0.0.1', url.port, timeout=3)
                client.request('POST', '/session', json.dumps({'token': url.fragment}), {'Origin': origin})
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                cookie = response.getheader('Set-Cookie').split(';')[0]
                response.read()
                client.request('GET', '/events', headers={'Cookie': cookie})
                stream = client.getresponse()
                self.assertEqual(stream.readline(), b'data: running\n')
                stream.close()
                client.close()
                self.assertEqual(process.wait(timeout=22), 0)
                self.assertEqual(json.loads((Path(folder) / 'status.json').read_text())['state'], 'stopped')
                self.assertIsNone(running_url(Path(folder) / 'status.json'))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def test_occupied_port_and_second_launch_reuse_the_owned_table(self):
        with tempfile.TemporaryDirectory() as folder, socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            command = [sys.executable, "-X", "utf8", "-m", "sanwufan.local", "--no-browser",
                       "--port", str(occupied.getsockname()[1]), "--data-dir", folder]
            with (Path(folder) / "process.log").open("wb") as log:
                process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                           creationflags=FLAGS)
            try:
                status = Path(folder) / "status.json"
                deadline = time.monotonic() + 15
                url = None
                while time.monotonic() < deadline and process.poll() is None:
                    url = running_url(status)
                    if url:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(url, (Path(folder) / "process.log").read_text(encoding="utf-8"))
                self.assertFalse(url.endswith(":" + str(occupied.getsockname()[1])))
                duplicate = subprocess.run(command, cwd=ROOT, capture_output=True, encoding="utf-8",
                                           creationflags=FLAGS, timeout=10)
                self.assertEqual(duplicate.returncode, 0, duplicate.stdout + duplicate.stderr)
                self.assertIn(url, duplicate.stdout)
                self.assertIsNone(process.poll())
                fake = Path(folder) / "fake.json"
                state = json.loads(status.read_text())
                state["instance"] = "a different table"
                fake.write_text(json.dumps(state))
                self.assertIsNone(running_url(fake))
            finally:
                process.kill()
                process.wait(timeout=5)

    def test_invalid_save_is_preserved_and_not_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            save = Path(folder) / "games.sqlite3"
            original = b"keep this invalid save for recovery"
            save.write_bytes(original)
            result = subprocess.run([sys.executable, "-X", "utf8", "-m", "sanwufan.local",
                                     "--no-browser", "--port", "0", "--data-dir", folder],
                                    cwd=ROOT, capture_output=True, encoding="utf-8", timeout=10,
                                    creationflags=FLAGS)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(save.read_bytes(), original)
            self.assertIsNone(running_url(Path(folder) / "status.json"))

    @unittest.skipUnless(os.name == "nt", "Windows launch scripts")
    def test_both_actual_cmd_entrypoints_display_help_without_starting(self):
        for name, option in [("启动牌桌.cmd", "--data-dir"), ("启动远程试玩.cmd", "--no-browser")]:
            with self.subTest(script=name):
                result = subprocess.run([os.environ["ComSpec"], "/d", "/c", name + " --help"],
                                        cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True,
                                        encoding="utf-8", timeout=10, creationflags=FLAGS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(option, result.stdout)
                self.assertNotIn("不是内部或外部命令", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
