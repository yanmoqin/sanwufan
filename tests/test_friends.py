import concurrent.futures
from dataclasses import replace
import http.client
from http.cookies import SimpleCookie
import json
from threading import Thread
import time
import unittest

from sanwufan.friends import FriendRooms
from sanwufan.game import Phase
from sanwufan.models import RuleViolation
from sanwufan.web import FRIEND_COOKIE, PracticeServer


class FriendRoomTests(unittest.TestCase):
    def setUp(self):
        self.registry = FriendRooms(seed=7)
        self.tokens = [self.registry.session(None)[0] for _ in range(5)]
        self.view = self.registry.request(self.tokens[0], {"command": "create", "name": "房主"})
        self.code = self.view["room_code"]
        self.room = self.registry.rooms[self.code]

    def join(self, player):
        return self.registry.request(self.tokens[player], {"command": "join", "name": f"朋友{player}",
                                                          "room_code": self.code})

    def action(self, player, action, **extra):
        v = self.registry.state(self.tokens[player])
        return self.registry.request(self.tokens[player], {"action": action, "version": v["version"],
                                                            "table_id": v["table_id"], **extra})

    def test_four_seats_and_fifth_player_rejected(self):
        for player in range(1, 4):
            self.assertEqual(self.join(player)["player_seat"], player)
        with self.assertRaisesRegex(RuleViolation, "座位"):
            self.join(4)
        self.assertEqual(self.registry.state(self.tokens[4])["mode"], "lobby")

    def test_no_bots_and_only_all_four_ready_starts(self):
        for player in range(1, 4):
            self.join(player)
        for player in range(3):
            self.action(player, "ready")
        self.room.tick(10)
        self.assertEqual(self.room.game.phase, Phase.WAITING)
        self.action(2, "unready")
        self.assertNotIn(2, self.room.game.ready_seats)
        self.action(2, "ready")
        self.action(3, "ready")
        self.assertTrue(self.room.tick(11))
        self.assertEqual(self.room.game.phase, Phase.DEALING)
        self.assertTrue(all(not p["bot"] for p in self.registry.state(self.tokens[0])["players"]))

    def test_seat_change_and_lock_after_ready(self):
        view = self.action(0, "change_seat")
        self.assertEqual(view["player_seat"], 1)
        self.assertTrue(view["is_owner"])
        self.assertEqual(self.join(1)["player_seat"], 0)
        self.action(1, "ready")
        with self.assertRaises(RuleViolation):
            self.action(0, "change_seat")

    def test_reconnect_restores_identity_and_marks_presence(self):
        self.join(1)
        self.action(1, "ready")
        token, _, created = self.registry.session(self.tokens[1])
        self.assertFalse(created)
        self.room.seen[token] = time.monotonic() - 30
        self.assertFalse(self.registry.state(self.tokens[0])["players"][1]["online"])
        v = self.registry.state(token)
        self.assertEqual(v["player_seat"], 1)
        self.assertIn(1, v["ready_seats"])
        self.assertTrue(v["players"][1]["online"])

    def test_owner_only_reset_and_owner_transfer_on_leave(self):
        self.join(1)
        with self.assertRaises(RuleViolation):
            self.action(1, "reset")
        self.registry.request(self.tokens[0], {"command": "leave"})
        self.assertTrue(self.registry.state(self.tokens[1])["is_owner"])
        self.action(1, "reset")
        self.registry.request(self.tokens[1], {"command": "leave"})
        self.assertNotIn(self.code, self.registry.rooms)

    def test_started_match_cannot_leave_reset_or_replace_players(self):
        for player in range(1, 4):
            self.join(player)
        for player in range(4):
            self.action(player, "ready")
        self.room.tick(100)
        for payload in ({"command": "leave"}, {"command": "create", "name": "新桌"}):
            with self.assertRaises(RuleViolation):
                self.registry.request(self.tokens[0], payload)
        with self.assertRaises(RuleViolation):
            self.action(0, "reset")
        self.assertEqual(len([m for m in self.room.members if m]), 4)

    def test_bad_names_codes_and_client_seats_are_rejected(self):
        for name in (None, "", "a" * 17, "a\nb"):
            with self.assertRaises(RuleViolation):
                self.registry.request(self.tokens[1], {"command": "create", "name": name})
        for code in (None, 123456, "wrong"):
            with self.assertRaises(RuleViolation):
                self.registry.request(self.tokens[1], {"command": "join", "name": "新朋友", "room_code": code})
        for extra in ({"seat": 1}, {"table_id": "other"}, {"cards": [42]}, {"kind": "fake"}):
            with self.assertRaises(RuleViolation):
                self.action(0, "ready", **extra)
        self.assertEqual(self.room.game.ready_seats, frozenset())

    def test_idle_rooms_expire_and_old_table_actions_cannot_resume(self):
        self.room.last_active = time.monotonic() - 7201
        self.registry.session(self.tokens[0])
        self.assertEqual(self.registry.state(self.tokens[0])["mode"], "lobby")
        with self.assertRaises(RuleViolation):
            self.registry.request(self.tokens[0], {"action": "ready", "table_id": self.view["table_id"], "version": 0})

    def test_absent_member_identity_survives_while_room_is_active(self):
        self.join(1)
        self.registry.sessions[self.tokens[1]]["seen"] = time.monotonic() - 7201
        token, _, created = self.registry.session(self.tokens[1])
        self.assertFalse(created)
        self.assertEqual(self.registry.state(token)["player_seat"], 1)

    def test_removed_room_returns_lobby_and_allows_new_room(self):
        del self.registry.rooms[self.code]
        with self.assertRaises(RuleViolation):
            self.registry.request(self.tokens[0], {"action": "ready", "table_id": self.view["table_id"], "version": 0})
        self.assertEqual(self.registry.state(self.tokens[0])["mode"], "lobby")
        result = self.registry.request(self.tokens[0], {"command": "create", "name": "房主"})
        self.assertEqual(result["mode"], "friends")
        self.assertEqual(self.registry.state("unknown")["mode"], "lobby")

    def test_dealing_updates_do_not_block_a_valid_earlier_two_selection(self):
        for player in range(1, 4):
            self.join(player)
        for player in range(4):
            self.action(player, "ready")
        self.room.tick(0)
        self.room.game = replace(self.room.game, number=2, dealer=0)
        for n in range(100):
            self.room.tick(n)
            choice = next(((p, c.id) for p, h in enumerate(self.room.game.hands)
                           for c in h if c.rank == "2"), None)
            if choice:
                break
        player, card_id = choice
        old = self.registry.state(self.tokens[player])
        self.room.tick(101)
        payload = {"action": "call_trump", "version": old["version"],
                   "table_id": old["table_id"], "cards": [card_id]}
        result = self.registry.request(self.tokens[player], payload)
        self.assertEqual(result["caller"], player)
        before = self.room.game
        with self.assertRaises(RuleViolation):
            self.registry.request(self.tokens[player], payload)
        self.assertIs(self.room.game, before)


class FriendHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = PracticeServer(("127.0.0.1", 0), seed=11)
        self.server.stop_ticks.set()
        self.server.ticker.join(2)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookies = []
        for _ in range(5):
            status, headers, state = self.request()
            self.assertEqual(status, 200)
            token = SimpleCookie(headers["Set-Cookie"])[FRIEND_COOKIE].value
            self.cookies.append(f"{FRIEND_COOKIE}={token}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, player=None, payload=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        request_headers = dict(headers or {})
        if player is not None:
            request_headers["Cookie"] = self.cookies[player]
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request("POST" if payload is not None else "GET",
                           "/api/friends/action" if payload is not None else "/api/friends/state",
                           json.dumps(payload) if payload is not None else None, request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), json.loads(response.read())
        connection.close()
        return result

    def create_table(self):
        status, _, v = self.request(0, {"command": "create", "name": "甲"})
        self.assertEqual(status, 200)
        for p in range(1, 4):
            status, _, _ = self.request(p, {"command": "join", "name": f"朋友{p}", "room_code": v["room_code"]})
            self.assertEqual(status, 200)
        return self.server.friends.rooms[v["room_code"]]

    def act(self, p, action, v=None, **extra):
        if v is None:
            _, _, v = self.request(p)
        return self.request(p, {"action": action, "table_id": v["table_id"], "version": v["version"], **extra})

    def test_four_clients_complete_three_deals_and_restore_private_hands(self):
        room = self.create_table()
        now = 0
        completed = 0
        saw_private_hands = False
        for _ in range(1800):
            now += 0.3
            room.tick(now)
            views = [self.request(p)[2] for p in range(4)]
            self.assertEqual(len({v["table_id"] for v in views}), 1)
            self.assertEqual([v["player_seat"] for v in views], list(range(4)))
            for p, v in enumerate(views):
                self.assertNotIn("stock", v)
                self.assertNotIn("hands", v)
                self.assertNotIn(self.cookies[p].split("=", 1)[1], json.dumps(v))
                self.assertEqual(set(v["hand"]), set(room.game.view_for(p)["hand"]))
            if room.game.phase == Phase.PLAYING and not saw_private_hands:
                hands = [set(v["hand"]) for v in views]
                self.assertEqual(sum(map(len, hands)), 48)
                self.assertEqual(len(set.union(*hands)), 48)
                self.assertEqual([len(v["bottom_cards"]) for v in views],
                                 [6] * 4)
                # A refresh returns the same identity, cards and seat.
                self.assertEqual(self.request(3)[2]["hand"], views[3]["hand"])
                saw_private_hands = True
            if room.game.phase == Phase.SETTLED:
                completed += 1
                self.assertEqual(sum(views[0]["team_points"]), 100)
                self.assertTrue(all(not v["hand"] for v in views))
                if completed == 3:
                    break
                self.assertEqual(self.act(2, "next_deal")[0], 200)
                continue
            performed = False
            for p, v in enumerate(views):
                for action in ("call_trump", "reveal", "ready", "confirm", "draw_trump", "give_tribute",
                               "return_tribute", "take_bottom", "discard", "play"):
                    if action in v["available_actions"]:
                        # The client submits only its own server-generated legal hint.
                        ids = v["hint"]["cards"] if v["hint"] and v["hint"]["action"] == action else []
                        status, _, result = self.act(p, action, v, cards=ids)
                        self.assertEqual(status, 200, result)
                        performed = True
                        break
                if performed:
                    break
        self.assertEqual(completed, 3)
        self.assertTrue(saw_private_hands)

    def test_concurrent_ready_same_version_accepts_exactly_one(self):
        room = self.create_table()
        views = [self.request(p)[2] for p in range(2)]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.act, p, "ready", views[p]) for p in range(2)]
            results = [f.result() for f in futures]
        self.assertEqual(sorted(r[0] for r in results), [200, 409])
        self.assertEqual(len(room.game.ready_seats), 1)
        losing_player = next(p for p, r in enumerate(results) if r[0] == 409)
        self.assertEqual(self.act(losing_player, "ready")[0], 200)
        self.assertEqual(len(room.game.ready_seats), 2)

    def test_chat_is_shared_only_in_room_and_does_not_stale_a_card_action(self):
        room = self.create_table()
        before = self.request(0)[2]
        payload = {'command': 'chat', 'room_code': room.code, 'text': '一起打牌'}
        self.assertEqual(self.request(1, payload)[0], 200)
        for p in range(4):
            _, _, view = self.request(p)
            self.assertEqual(view['chat_messages'][0]['text'], '一起打牌')
            self.assertEqual(view['chat_messages'][0]['seat'], 1)
        self.assertNotIn('chat_messages', self.request(4)[2])
        self.assertEqual(self.request(payload=payload)[0], 401)
        self.assertEqual(self.request(4, payload)[0], 400)
        self.assertEqual(self.act(0, 'ready', before)[0], 200)

    def test_revolution_refreshes_all_clients_and_rejects_replays(self):
        from tests.test_revolution_chat import scoreless_game
        room = self.create_table()
        room.game = scoreless_game()
        before = self.request(0)[2]
        status, _, view = self.act(0, 'revolution', before)
        self.assertEqual(status, 200)
        self.assertEqual(view['deal_number'], 1)
        self.assertEqual(view['next_deal_seat'], 3)
        self.assertNotEqual(view['table_id'], before['table_id'])
        for p in range(4):
            fresh = self.request(p)[2]
            self.assertEqual(fresh['table_id'], view['table_id'])
            self.assertEqual(fresh['tribute_obligations'], [])
            self.assertEqual(fresh['hand'], [])
        self.assertEqual(self.act(0, 'confirm', before)[0], 409)

    def test_no_session_foreign_seat_wrong_origin_and_server_commands(self):
        room = self.create_table()
        self.assertEqual(self.request(payload={"action": "ready"})[0], 401)
        self.assertEqual(self.act(0, "ready", seat=1)[0], 400)
        self.assertEqual(self.act(0, "start_deal")[0], 400)
        self.assertEqual(self.request(0, {"command": "leave"}, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request(0, headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(len(room.game.ready_seats), 0)

    def test_identical_tabs_share_seat_and_practice_remains_separate(self):
        room = self.create_table()
        original = self.request(1)[2]
        refresh = self.request(1)[2]
        self.assertEqual(original, refresh)
        connection = http.client.HTTPConnection(*self.server.server_address)
        connection.request("GET", "/api/state", headers={"Cookie": self.cookies[1]})
        result = json.loads(connection.getresponse().read())
        connection.close()
        self.assertNotEqual(result["table_id"], room.table_id)
        self.assertEqual(self.request(1)[2]["player_seat"], 1)

    def test_cannot_play_another_players_card_or_play_out_of_turn(self):
        from tests.test_game import declarations_done, prepared
        room = self.create_table()
        g = declarations_done(prepared())
        g = g.take_bottom(g.dealer)
        g = g.discard(g.dealer, tuple(c for c in g.hands[g.dealer] if not c.points)[:6])
        room.game = g
        dealer = g.dealer
        foreign = (dealer + 1) % 4
        status, _, result = self.act(dealer, "play", cards=[g.hands[foreign][0].id])
        self.assertEqual(status, 400)
        self.assertEqual(result["state"]["hand"], room.snapshot_for(room.members[dealer])["hand"])
        self.assertEqual(self.act(foreign, "play", cards=[g.hands[foreign][0].id])[0], 400)
        self.assertIs(room.game, g)

    def test_explicit_lan_host_allowlist_and_origin_checks(self):
        self.assertEqual(self.request(0, headers={"Host": "192.168.1.23:8765"})[0], 403)
        self.server.allowed_hosts.add("192.168.1.23")
        host = f"192.168.1.23:{self.server.server_address[1]}"
        self.assertEqual(self.request(0, headers={"Host": host, "Origin": f"http://{host}"})[0], 200)
        self.assertEqual(self.request(0, headers={"Host": host, "Origin": "http://evil.example"})[0], 403)


if __name__ == "__main__":
    unittest.main()
