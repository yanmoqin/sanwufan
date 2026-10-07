import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sanwufan.remote import LaunchLock, WindowsJob, registered_origin, existing_invitation


class RemoteTests(unittest.TestCase):
    def test_reuse_requires_a_ready_authorized_and_healthy_invitation(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            status = data / "status.json"
            origin = "https://abc123.tunnelmole.net"
            with patch("sanwufan.remote.healthy", return_value=True) as check:
                status.write_text(json.dumps({"state": "stopped", "origin": origin}))
                self.assertIsNone(existing_invitation(data))
                status.write_text(json.dumps({"state": "ready", "origin": "https://evil.example"}))
                self.assertIsNone(existing_invitation(data))
                check.assert_not_called()
                status.write_text(json.dumps({"state": "ready", "origin": origin}))
                self.assertEqual(existing_invitation(data), origin + "/friends")
            with patch("sanwufan.remote.healthy", return_value=False):
                self.assertIsNone(existing_invitation(data))

    def test_tunnel_banner_is_not_mistaken_for_a_working_invitation(self):
        self.assertIsNone(registered_origin("Visit https://dashboard.tunnelmole.com/"))
        self.assertIsNone(registered_origin(json.dumps({"type": "error", "origin": "https://wrong.tunnelmole.net"})))
        self.assertIsNone(registered_origin(json.dumps({"type": "sanwufan.tunnel.ready", "origin": "https://wrong.tunnelmole.net.evil.com"})))
        self.assertEqual(registered_origin(json.dumps({"type": "sanwufan.tunnel.ready",
                            "origin": "https://abc123.tunnelmole.net"})), "https://abc123.tunnelmole.net")

    def test_launch_lock_rejects_duplicate_and_releases_after_close(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launcher.lock"
            owner = LaunchLock(path)
            try:
                with self.assertRaises(OSError):
                    LaunchLock(path)
            finally:
                owner.close()
            LaunchLock(path).close()

    @unittest.skipUnless(os.name == "nt", "Windows process ownership")
    def test_job_close_kills_owned_child_but_not_another_process(self):
        flags = subprocess.CREATE_NO_WINDOW
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], creationflags=flags)
        unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], creationflags=flags)
        job = WindowsJob()
        try:
            job.add(child)
            job.close()
            child.wait(timeout=5)
            self.assertIsNone(unrelated.poll())
        finally:
            job.close()
            for process in (child, unrelated):
                if process.poll() is None:
                    process.kill()
                process.wait()
