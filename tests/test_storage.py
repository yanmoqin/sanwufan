from dataclasses import replace
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from sanwufan.friends import FriendRooms
from sanwufan.game import Game, Phase
from sanwufan.models import RuleViolation
from sanwufan.practice import suggested_cards
from sanwufan.storage import SQLiteStore, StorageError, decode, encode
from tests.test_game import declarations_done, prepared


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "牌局 data.sqlite3"
        self.registry = FriendRooms(seed=7, store=SQLiteStore(self.path))
        self.tokens = [self.registry.session(None)[0] for _ in range(4)]
        view = self.registry.request(self.tokens[0], {"command": "create", "name": "甲"})
        self.code = view["room_code"]
        for p in range(1, 4):
            self.registry.request(self.tokens[p], {"command": "join", "name": f"朋友{p}", "room_code": self.code})
        self.clock = time.monotonic()

    def tearDown(self):
        self.registry.close()
        self.folder.cleanup()

    def restart(self):
        # Abrupt process death: only close the descriptor, do NOT checkpoint RAM.
        self.registry.store.close()
        self.registry = FriendRooms(seed=999, store=SQLiteStore(self.path))
        self.clock = time.monotonic()

    def action(self, p, action, cards=()):
        view = self.registry.state(self.tokens[p])
        return self.registry.request(self.tokens[p], {"action": action, "version": view["version"],
                    "table_id": view["table_id"], "cards": [c.id for c in cards]})

    def room(self):
        return self.registry.rooms[self.code]

    def test_acknowledged_seat_ready_and_owner_survive_without_shutdown_save(self):
        view = self.action(1, "ready")
        self.restart()
        restored = self.registry.state(self.tokens[1])
        for key in ("table_id", "version", "ready_seats", "player_seat", "room_code"):
            self.assertEqual(restored[key], view[key])
        self.assertTrue(self.registry.state(self.tokens[0])["is_owner"])
        self.assertFalse(restored["is_owner"])

    def test_write_failure_rolls_back_ready_and_preserves_disk(self):
        game = self.room().game
        version = self.room().version
        with patch.object(self.registry.store, "write", side_effect=StorageError("disk full")):
            with self.assertRaises(StorageError):
                self.action(1, "ready")
        self.assertIs(self.room().game, game)
        self.assertEqual(self.room().version, version)
        self.restart()
        self.assertNotIn(1, self.registry.state(self.tokens[1])["ready_seats"])

    def test_write_failure_rolls_back_leave_and_owner_transfer(self):
        with patch.object(self.registry.store, "write", side_effect=StorageError("disk full")):
            with self.assertRaises(StorageError):
                self.registry.request(self.tokens[0], {"command": "leave"})
        self.assertTrue(self.registry.state(self.tokens[0])["is_owner"])
        self.assertEqual(self.room().members[0], self.tokens[0])
        self.restart()
        self.assertEqual(self.room().owner, self.tokens[0])

    def test_failed_player_session_is_not_acknowledged_or_kept_in_ram(self):
        original = set(self.registry.sessions)
        with patch.object(self.registry.store, "write", side_effect=StorageError("disk full")):
            with self.assertRaises(StorageError):
                self.registry.session(None)
        self.assertEqual(set(self.registry.sessions), original)

    def test_dealing_restart_waits_for_all_players_and_keeps_stock_order(self):
        for p in range(4):
            self.action(p, "ready")
        for _ in range(12):
            self.clock += 0.2
            self.registry.tick(self.clock)
        game = self.room().game
        self.assertEqual(game.phase, Phase.DEALING)
        self.restart()
        self.assertEqual(self.room().game, game)
        self.assertTrue(self.room().recovering)
        for p in range(3):
            self.registry.state(self.tokens[p])
        self.registry.tick(time.monotonic() + 0.2)
        self.assertEqual(self.room().game, game)
        self.registry.state(self.tokens[3])
        self.registry.tick(time.monotonic() + 0.4)
        self.assertFalse(self.room().recovering)
        self.assertEqual(self.room().game.dealt_count, game.dealt_count + 1)
        self.assertEqual(self.room().game.stock, game.stock[1:])

    def test_crash_after_play_does_not_duplicate_or_leak_action(self):
        g = declarations_done(prepared())
        g = g.take_bottom(g.dealer)
        g = g.discard(g.dealer, tuple(c for c in g.hands[g.dealer] if not c.points)[:6])
        self.room().game = g
        self.registry._save()
        p = g.trick.next_seat
        view = self.registry.state(self.tokens[p])
        card = suggested_cards(g, p, "play")
        self.action(p, "play", card)
        accepted = self.room().game
        self.restart()
        self.assertEqual(self.room().game, accepted)
        with self.assertRaises(RuleViolation):
            self.registry.request(self.tokens[p], {"action": "play", "table_id": view["table_id"],
                                                   "version": view["version"], "cards": [c.id for c in card]})
        for seat in range(4):
            v = self.registry.state(self.tokens[seat])
            self.assertEqual(set(v["hand"]), set(accepted.view_for(seat)["hand"]))
            self.assertNotIn("stock", v)
            self.assertNotIn("hands", v)

    def test_three_complete_deals_survive_restarts_in_every_phase(self):
        completed, restored_phases = 0, set()
        for step in range(1500):
            self.clock += 0.3
            self.registry.tick(self.clock)
            views = [self.registry.state(t) for t in self.tokens]
            g = self.room().game
            if g.phase not in restored_phases or (g.phase == Phase.PLAYING and step % 9 == 0):
                restored_phases.add(g.phase)
                self.restart()
                self.assertEqual(self.room().game, g)
                continue
            if g.phase == Phase.SETTLED:
                completed += 1
                self.assertEqual(sum(g.team_points), 100)
                if completed == 3:
                    break
                self.action(2, "next_deal")
                continue
            performed = False
            for p, v in enumerate(views):
                for action in ("call_trump", "reveal", "ready", "confirm", "draw_trump", "give_tribute",
                               "return_tribute", "take_bottom", "discard", "play"):
                    if action in v["available_actions"]:
                        self.action(p, action, suggested_cards(g, p, action))
                        performed = True
                        break
                if performed:
                    break
        self.assertEqual(completed, 3)
        self.assertIn(Phase.TRIBUTE_RETURN, restored_phases)
        self.assertIn(Phase.DISCARD, restored_phases)
        self.assertEqual(len(self.room().game.history), 2)

    def test_database_cannot_be_owned_by_two_services(self):
        with self.assertRaises(StorageError):
            SQLiteStore(self.path)

    def test_live_backup_contains_last_committed_game(self):
        self.action(2, "ready")
        backup = self.path.with_name("backup.sqlite3")
        self.registry.store.backup(backup)
        copied = FriendRooms(store=SQLiteStore(backup))
        try:
            self.assertIn(2, copied.state(self.tokens[2])["ready_seats"])
        finally:
            copied.close()

    def test_unknown_schema_and_type_fail_without_overwriting_save(self):
        self.registry.store.close()
        payload = json.dumps({"schema": 999, "state": {}})
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE checkpoints SET payload=?", (payload,))
        store = SQLiteStore(self.path)
        try:
            with self.assertRaises(StorageError):
                FriendRooms(store=store)
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute("SELECT payload FROM checkpoints").fetchone()[0], payload)
        finally:
            store.close()
        # Keep tearDown from deliberately writing over this invalid file.
        self.registry.store = None

    def test_json_codec_preserves_game_options_and_rejects_executable_types(self):
        game = prepared()
        self.assertEqual(decode(json.loads(json.dumps(encode(game)))), game)
        with self.assertRaises(KeyError):
            decode({"type": "os.system", "fields": {}})


if __name__ == "__main__":
    unittest.main()
