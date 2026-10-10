from pathlib import Path
import shutil
import subprocess
import unittest


class FrontendStateTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Frontend regression requires developer Node.js')
    def test_initial_practice_connection_and_chat_snapshot_order(self):
        result = subprocess.run([shutil.which('node'), str(Path(__file__).with_name('frontend_state.test.js'))],
                                capture_output=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
