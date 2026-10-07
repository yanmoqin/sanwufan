from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sanwufan.friends import FriendRooms
from sanwufan.models import RuleViolation
from sanwufan.storage import SQLiteStore, StorageError


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "game.sqlite3"
        self.rooms = FriendRooms(seed=7, store=SQLiteStore(self.path))
        self.tokens = [self.rooms.session(None)[0] for _ in range(4)]
        self.view = self.rooms.request(self.tokens[0], {"command": "create", "name": "房主"})
        for i in range(1, 4):
            self.rooms.request(self.tokens[i], {"command": "join", "name": str(i), "room_code": self.view["room_code"]})

    def tearDown(self):
        self.rooms.close()
        self.folder.cleanup()

    def recover(self, token, code):
        return self.rooms.request(token, {"command": "recover", "recovery_code": code})

    def test_restart_and_changed_browser_restore_owner_and_private_hand(self):
        # A dealt game tests preservation beyond an empty lobby.
        room = self.rooms.rooms[self.view["room_code"]]
        for seat in range(4):
            room.game = room.game.ready(seat)
        room.game = room.game.start_deal(room._order())
        for _ in range(12):
            room.game = room.game.deal_one()
        before = self.rooms.state(self.tokens[0])
        key = before["recovery_code"]
        self.rooms.close()
        self.rooms = FriendRooms(store=SQLiteStore(self.path))
        new = self.rooms.session(None)[0]
        restored = self.recover(new, key)
        for field in ("hand", "table_id", "room_code", "phase", "player_seat"):
            self.assertEqual(restored[field], before[field])
        self.assertTrue(restored["is_owner"])
        self.assertNotEqual(restored["recovery_code"], key)
        self.assertNotIn(self.tokens[0], self.rooms.sessions)
        outsider = self.rooms.session(None)[0]
        with self.assertRaises(RuleViolation):
            self.recover(outsider, key)
        self.assertNotIn(restored["recovery_code"], str(self.rooms.state(self.tokens[1])))

    def test_invalid_code_and_seated_player_cannot_change_seats(self):
        newcomer = self.rooms.session(None)[0]
        for code in (None, "bad", "x" * 24, "你" * 24):
            with self.assertRaises(RuleViolation):
                self.recover(newcomer, code)
        with self.assertRaises(RuleViolation):
            self.recover(self.tokens[1], self.view["recovery_code"])
        self.assertEqual(self.rooms.state(self.tokens[0])["player_seat"], 0)

    def test_failed_save_rolls_back_ownership_key_and_old_session(self):
        new = self.rooms.session(None)[0]
        with patch.object(self.rooms.store, "write", side_effect=StorageError("disk full")):
            with self.assertRaises(StorageError):
                self.recover(new, self.view["recovery_code"])
        self.assertTrue(self.rooms.state(self.tokens[0])["is_owner"])
        self.assertEqual(self.rooms.state(self.tokens[0])["recovery_code"], self.view["recovery_code"])
        self.assertEqual(self.rooms.state(new)["mode"], "lobby")


if __name__ == "__main__":
    unittest.main()
