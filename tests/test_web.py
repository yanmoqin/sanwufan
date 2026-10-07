from http.client import HTTPConnection
from dataclasses import replace
from http.cookies import SimpleCookie
import json
from threading import Thread
import unittest

from sanwufan import Game, Phase, PlayKind, RuleViolation, Tribute
from sanwufan.practice import PracticeRoom, legal_play
from sanwufan.web import COOKIE, PracticeServer
from tests.test_game import cards, declarations_done, fixed_order, prepared


def perform(room, action, selected=(), **extra):
    return room.action({"action": action, "version": room.version,
                        "table_id": room.table_id,
                        "cards": [c.id for c in selected], **extra})


class PracticeTest(unittest.TestCase):
    def assert_violation(self, code, fn, *args):
        with self.assertRaises(RuleViolation) as caught:
            fn(*args)
        self.assertEqual(caught.exception.code, code)

    def test_bots_prepare_but_wait_for_the_human(self):
        room = PracticeRoom(7)
        for now in range(10):
            room.tick(now)
        self.assertEqual(room.game.phase, Phase.WAITING)
        self.assertEqual(room.game.ready_seats, frozenset((1, 2, 3)))
        perform(room, "ready")
        room.tick(20)
        self.assertEqual(room.game.phase, Phase.DEALING)

    def test_manual_human_and_bots_complete_three_connected_deals(self):
        room = PracticeRoom(7)
        now = 0
        phases = set()
        for number in range(1, 4):
            for _ in range(400):
                now += 1
                view = room.snapshot()
                phases.add(view["phase"])
                if room.game.phase == Phase.SETTLED:
                    break
                choices = room.available()
                action = next((a for a in ("ready", "call_trump", "reveal", "confirm", "draw_trump",
                                           "give_tribute", "return_tribute", "take_bottom", "discard", "play")
                               if a in choices), None)
                if action:
                    hint = view["hint"]
                    selected = cards(*hint["cards"]) if hint and hint["action"] == action else ()
                    perform(room, action, selected)
                room.tick(now)
            self.assertEqual(room.game.phase, Phase.SETTLED)
            self.assertEqual(room.game.number, number)
            self.assertEqual(sum(room.game.team_points), 100)
            self.assertFalse(room.auto)
            room.game.check_invariants()
            if number < 3:
                perform(room, "next_deal")
        self.assertTrue({"waiting", "dealing", "declaring", "take_bottom", "discard", "playing"} <= phases)
        self.assertEqual(len(room.game.history), 2)

    def test_auto_stops_at_settlement_and_can_be_taken_over(self):
        room = PracticeRoom(3)
        perform(room, "auto")
        for now in range(30):
            room.tick(now)
        perform(room, "auto")
        self.assertFalse(room.auto)
        perform(room, "auto")
        for now in range(30, 300):
            room.tick(now)
            if room.game.phase == Phase.SETTLED:
                break
        self.assertEqual(room.game.phase, Phase.SETTLED)
        self.assertFalse(room.snapshot()["auto"])
        self.assertEqual(sum(room.game.team_points), 100)
        old = room.game
        room.tick(400)
        self.assertIs(room.game, old)

    def test_human_dealer_takes_bottom_rejects_points_then_discards_and_leads(self):
        room = PracticeRoom(7)
        room.game = declarations_done(prepared(order=fixed_order((cards("C:2"), (), (), ()))))
        self.assertEqual(room.game.dealer, 0)
        view = perform(room, "take_bottom")
        self.assertEqual(len(view["hand"]), 18)
        self.assertEqual(view["hand_counts"], [18, 12, 12, 12])
        hint = cards(*view["hint"]["cards"])
        self.assertEqual(len(hint), 6)
        self.assertFalse(any(c.points for c in hint))
        point = next(c for c in room.game.hands[0] if c.points)
        before = room.game
        self.assert_violation("POINTS_IN_BOTTOM", perform, room, "discard", hint[:5] + (point,))
        self.assertIs(room.game, before)
        view = perform(room, "discard", hint)
        self.assertEqual(view["phase"], "playing")
        self.assertEqual(view["hand_counts"], [12] * 4)
        self.assertEqual(set(view["bottom_cards"]), {c.id for c in hint})
        self.assertEqual(view["available_actions"], ["play"])
        perform(room, "play", cards(*view["hint"]["cards"]))
        self.assertEqual(room.game.trick.next_seat, 1)

    def test_restored_called_deal_finishes_without_last_card_delay(self):
        room = PracticeRoom(7)
        room.game = prepared(call=False).redeal(fixed_order())
        for _ in range(47):
            room.game = room.game.deal_one()
        # An older checkpoint may still be dealing after a valid claim.
        caller = next(s for s in range(4) if any(c.rank == "2" for c in room.game.hands[s]))
        two = next(c for c in room.game.hands[caller] if c.rank == "2")
        room.game = room.game.call_trump(caller, two)
        room.tick(100)
        self.assertEqual(room.game.dealt_count, 48)
        self.assertEqual(room.game.phase, Phase.DECLARING)
        self.assertIsNone(room.claim_deadline)

    def test_bot_trump_claim_is_delayed_to_allow_manual_claims(self):
        room = PracticeRoom(7)
        order = fixed_order(((), cards("H:2"), (), ()))
        room.game = Game()
        for seat in range(4):
            room.game = room.game.ready(seat)
        room.game = room.game.start_deal(order).deal_one().deal_one()
        room.game = replace(room.game, number=2, dealer=0)
        room.tick(10)
        self.assertIsNone(room.game.called_two)
        room.tick(11)
        self.assertIsNone(room.game.called_two)
        room.tick(12)
        self.assertEqual(room.game.caller, 1)

    def test_bots_can_follow_human_true_and_false_gangs(self):
        cases = ((cards("S:8", "H:8", "C:8", "D:8"), PlayKind.TRUE_GANG),
                 (cards("S:Q", "H:Q", "C:Q", "D:Q"), PlayKind.FALSE_GANG))
        bottom = cards("H:3", "S:3", "D:3", "H:4", "S:4", "D:4")
        for gang, kind in cases:
            with self.subTest(kind=kind):
                room = PracticeRoom(7)
                room.game = declarations_done(prepared(Game(dealer=0),
                    fixed_order((gang + cards("C:2"), (), (), ()), bottom)))
                room.game = room.game.take_bottom(0).discard(0, bottom)
                perform(room, "play", gang, kind=kind.value)
                for now in range(1, 4):
                    room.tick(now)
                self.assertEqual(len(room.game.tricks), 1)
                self.assertEqual([len(h) for h in room.game.hands], [8] * 4)
                room.game.check_invariants()

    def test_tribute_hint_never_returns_a_revealed_three(self):
        wanted = (cards("H:3", "S:3", "D:3", "C:2"), cards("D:5"), (), ())
        room = PracticeRoom(7)
        room.game = prepared(Game(dealer=0, obligations=(Tribute(1, 0),)), fixed_order(wanted))
        room.game = room.game.reveal(0, wanted[0][:3])
        room.game = declarations_done(room.game).give_tribute(1, cards("D:5")[0])
        hint = room.snapshot()["hint"]
        self.assertEqual(hint["action"], "return_tribute")
        self.assertFalse(set(cards(*hint["cards"])) & set(wanted[0][:3]))
        perform(room, "return_tribute", cards(*hint["cards"]))
        self.assertEqual(room.game.phase, Phase.TAKE_BOTTOM)

    def test_bad_action_and_stale_request_do_not_mutate_game(self):
        room = PracticeRoom(7)
        before = room.game
        self.assert_violation("INVALID_REQUEST", room.action, {"action": "ready", "version": 0, "seat": 1})
        self.assert_violation("UNKNOWN_ACTION", room.action, {"action": "deal_one", "version": 0, "table_id": room.table_id})
        self.assert_violation("WRONG_PHASE", room.action, {"action": "play", "version": 0, "table_id": room.table_id, "cards": ["D:5"]})
        self.assertIs(room.game, before)
        perform(room, "ready")
        self.assert_violation("STATE_CHANGED", room.action, {"action": "reset", "version": 0, "table_id": room.table_id})
        self.assertIn(0, room.game.ready_seats)

    def test_snapshot_and_event_log_keep_bot_private_cards_hidden(self):
        room = PracticeRoom(7)
        room.game = prepared(Game(dealer=1, obligations=(Tribute(0, 1),)),
                             fixed_order((cards("D:5"), cards("H:2"), (), ())))
        room.game = declarations_done(room.game)
        view = room.snapshot()
        self.assertNotIn("hands", view)
        self.assertNotIn("stock", view)
        self.assertEqual(set(view["hand"]), {c.id for c in room.game.hands[0]})
        perform(room, "give_tribute", cards("D:5"))
        room.tick(10)
        # Events report the choice, without disclosing the receiver's private return.
        self.assertTrue(all("贡牌" not in e or "已选择" in e for e in room.events))
        json.dumps(room.snapshot(), ensure_ascii=False)

    def test_reset_keeps_ui_versions_monotonic(self):
        room = PracticeRoom(7)
        perform(room, "ready")
        version = room.version
        perform(room, "reset")
        self.assertGreater(room.version, version)
        self.assertEqual(room.game.phase, Phase.WAITING)
        self.assertEqual(room.game.hands, ((), (), (), ()))

    def test_requests_from_an_old_table_cannot_apply_to_a_new_table(self):
        old = PracticeRoom(7)
        new = PracticeRoom(7)
        self.assertEqual(old.version, new.version)
        self.assert_violation("STATE_CHANGED", new.action,
                              {"action": "ready", "version": old.version, "table_id": old.table_id})
        self.assertEqual(new.game.ready_seats, frozenset())


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = PracticeServer(("127.0.0.1", 0), seed=7)
        cls.server.stop_ticks.set()
        cls.server.ticker.join(timeout=2)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method="GET", path="/api/state", payload=None, cookie=None, headers=None, raw=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        sent = dict(headers or {})
        if cookie:
            sent["Cookie"] = cookie
        body = raw
        if payload is not None:
            if isinstance(payload, dict) and cookie:
                parsed_cookie = SimpleCookie(cookie)
                token = parsed_cookie[COOKIE].value
                room = self.server.rooms.get(token)
                if room:
                    payload = {"table_id": room.table_id, **payload}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            sent.setdefault("Content-Type", "application/json")
        connection.request(method, path, body, sent)
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        return result

    def session(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        cookie = SimpleCookie(headers["Set-Cookie"])
        token = cookie[COOKIE].value
        return f"{COOKIE}={token}", json.loads(body), self.server.rooms[token]

    def test_static_assets_are_served_without_third_party_requests(self):
        for path, content in (("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css")):
            status, headers, body = self.request(path=path)
            self.assertEqual(status, 200)
            self.assertTrue(headers["Content-Type"].startswith(content))
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
            self.assertGreater(len(body), 100)
        status, _, _ = self.request(path="/../sanwufan/game.py")
        self.assertEqual(status, 404)

    def test_practice_emote_http_round_trip_and_cooldown(self):
        cookie, view, room = self.session()
        payload = {'command': 'emote', 'target': 2, 'item': 'flower', 'table_id': view['table_id']}
        status, _, body = self.request('POST', '/api/action', payload, cookie)
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result['version'], view['version'])
        self.assertEqual(result['social_events'][0]['to'], 2)
        self.assertEqual(result['social_events'][0]['item'], 'flower')
        status, _, body = self.request('POST', '/api/action', payload, cookie)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)['error']['code'], 'EMOTE_TOO_FAST')
        self.assertEqual(room.social_sequence, 1)

    def test_refresh_resumes_same_practice_table(self):
        cookie, view, room = self.session()
        self.request("POST", "/api/action", {"action": "ready", "version": view["version"]}, cookie)
        status, headers, body = self.request(cookie=cookie)
        next_view = json.loads(body)
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(next_view["version"], room.version)
        self.assertIn(0, next_view["ready_seats"])
        _, first_headers, _ = self.request()
        self.assertIn("HttpOnly", first_headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", first_headers["Set-Cookie"])

    def test_each_browser_session_has_a_separate_table(self):
        cookie_a, view_a, room_a = self.session()
        cookie_b, view_b, room_b = self.session()
        self.assertIsNot(room_a, room_b)
        self.request("POST", "/api/action", {"action": "ready", "version": view_a["version"]}, cookie_a)
        _, _, body = self.request(cookie=cookie_b)
        self.assertNotIn(0, json.loads(body)["ready_seats"])

    def test_actions_require_existing_session(self):
        status, _, body = self.request("POST", "/api/action", {"action": "ready", "version": 0})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "SESSION_EXPIRED")

    def test_stale_action_returns_latest_state_without_executing_it(self):
        cookie, view, room = self.session()
        room.tick(10)
        before = room.game
        status, _, body = self.request("POST", "/api/action", {"action": "reset", "version": view["version"]}, cookie)
        result = json.loads(body)
        self.assertEqual(status, 409)
        self.assertEqual(result["error"]["code"], "STATE_CHANGED")
        self.assertEqual(result["state"]["version"], room.version)
        self.assertIs(room.game, before)

    def test_cannot_submit_a_different_seat_or_server_only_action(self):
        cookie, view, room = self.session()
        for extra in ({"seat": 1}, {}):
            action = "ready" if extra else "start_deal"
            status, _, _ = self.request("POST", "/api/action", {"action": action, "version": room.version, **extra}, cookie)
            self.assertEqual(status, 400)
        self.assertEqual(room.game.phase, Phase.WAITING)
        self.assertEqual(room.version, 0)

    def test_invalid_json_card_type_and_large_bodies_are_rejected(self):
        cookie, view, room = self.session()
        for raw in (b"invalid", b"x" * 8193):
            status, _, _ = self.request("POST", "/api/action", cookie=cookie,
                                       headers={"Content-Type": "application/json"}, raw=raw)
            self.assertEqual(status, 400)
        for value in (None, [], {"action": "play", "version": 0, "cards": [42]},
                      {"action": "play", "version": 0, "cards": "D:5"}):
            if isinstance(value, dict):
                value["table_id"] = room.table_id
            status, _, _ = self.request("POST", "/api/action", cookie=cookie,
                                       headers={"Content-Type": "application/json"},
                                       raw=json.dumps(value).encode())
            self.assertEqual(status, 400)
        self.assertEqual(room.version, 0)

    def test_expired_session_gets_a_new_table_and_old_actions_fail(self):
        cookie, view, room = self.session()
        perform(room, "ready")
        token = SimpleCookie(cookie)[COOKIE].value
        del self.server.rooms[token]
        status, headers, body = self.request(cookie=cookie)
        fresh = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(fresh["version"], 0)
        self.assertNotEqual(fresh["table_id"], view["table_id"])
        new_token = SimpleCookie(headers["Set-Cookie"])[COOKIE].value
        new_cookie = f"{COOKIE}={new_token}"
        status, _, _ = self.request("POST", "/api/action",
                                   {"action": "ready", "version": 0, "table_id": view["table_id"]}, new_cookie)
        self.assertEqual(status, 409)

    def test_wrong_host_origin_and_content_type_are_rejected(self):
        cookie, view, room = self.session()
        status, _, _ = self.request(headers={"Host": "example.com"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/action", {"action": "ready", "version": 0}, cookie,
                                   headers={"Origin": "http://example.com"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/action", cookie=cookie, raw=b"{}",
                                   headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(room.version, 0)


if __name__ == "__main__":
    unittest.main()
