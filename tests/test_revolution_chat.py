from dataclasses import replace
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from sanwufan.friends import FriendRooms
from sanwufan.game import Game, Phase
from sanwufan.models import RuleContext, RuleViolation, Suit, deck
from sanwufan.practice import PracticeRoom
from sanwufan.rules import prepare_lead, validate_follow
from sanwufan.settlement import Tribute
from sanwufan.storage import SQLiteStore, decode, encode
from tests.test_game import cards, declarations_done, fixed_order, prepared


def scoreless_game():
    hand = tuple(c for c in deck() if not c.points)[:12]
    return prepared(Game(number=3, dealer=3, obligations=(Tribute(0, 1),)),
                    fixed_order((hand, (), (), ())))


class RevolutionTests(unittest.TestCase):
    def test_scoreless_choice_clears_match_and_deals_to_original_dealer_first(self):
        g = scoreless_game()
        self.assertTrue(g.can_revolution(0))
        self.assertFalse(g.can_revolution(1))
        restart = g.revolution(0, deck())
        self.assertEqual(restart.number, 1)
        self.assertIsNone(restart.dealer)
        self.assertIsNone(restart.context)
        self.assertEqual(restart.deal_start_seat, 3)
        self.assertEqual(restart.view_for(0)['next_deal_seat'], 3)
        self.assertEqual(restart.obligations, ())
        self.assertEqual(restart.history, ())
        self.assertEqual(restart.reflections, ())
        self.assertEqual(restart.confirmed, frozenset())
        self.assertGreater(restart.revision, g.revision)
        first = restart.deal_one()
        self.assertEqual(first.hands[3], (deck()[0],))
        self.assertFalse(first.hands[0])
        first.check_invariants()

    def test_only_complete_original_hand_before_exchange_or_bottom_is_eligible(self):
        g = scoreless_game()
        self.assertFalse(replace(g, phase=Phase.DEALING, dealt_count=47).can_revolution(0))
        with self.assertRaises(RuleViolation):
            g.revolution(1)
        later = declarations_done(g)
        self.assertTrue(later.can_revolution(0))
        # Once any tribute is given the original-hand window is closed for everyone.
        from sanwufan.rules import is_trump, strength
        gift = max((c for c in later.hands[0] if is_trump(c, later.context)),
                   key=lambda c: strength(c, later.context))
        later = later.give_tribute(0, gift)
        self.assertFalse(later.can_revolution(0))
        no_tribute = replace(g, obligations=())
        bottom = declarations_done(no_tribute).take_bottom(3)
        self.assertFalse(bottom.can_revolution(0))

    def test_room_reset_invalidates_old_actions_and_first_two_is_automatic(self):
        room = PracticeRoom(seed=1)
        room.game = scoreless_game()
        old_id, old_version = room.table_id, room.version
        new_order = list(deck())
        two = cards('H:2')[0]
        new_order.remove(two)
        new_order.insert(0, two)
        with patch.object(room, '_order', return_value=new_order):
            view = room.action({'action': 'revolution', 'table_id': old_id, 'version': old_version})
        self.assertNotEqual(view['table_id'], old_id)
        self.assertEqual(view['deal_number'], 1)
        with self.assertRaises(RuleViolation):
            room.action({'action': 'confirm', 'table_id': old_id, 'version': old_version})
        room._deal_card()
        self.assertEqual(room.game.caller, 3)
        self.assertEqual(room.game.dealer, 3)
        self.assertEqual(room.game.phase, Phase.DECLARING)

    def test_v1_checkpoint_and_new_deal_order_round_trip(self):
        old = encode(scoreless_game())
        del old['fields']['deal_start_seat']
        self.assertEqual(decode(old).deal_start_seat, 0)
        g = scoreless_game().revolution(0, deck()).deal_one()
        self.assertEqual(decode(encode(g)), g)

    def test_abstract_draw_identifies_the_player_who_sets_trump(self):
        g = prepared(Game(number=1, dealer=0), call=False)
        index = next(i for i, c in enumerate(g.bottom) if c.suit != Suit.JOKER)
        g = g.draw_trump(1, index)
        self.assertEqual(g.view_for(2)['caller'], 1)
        self.assertIsNone(g.called_two)

    def test_no_trump_can_follow_with_either_side_suit_or_a_mix(self):
        ctx = RuleContext(Suit.HEARTS)
        hand = cards('C:4', 'C:6', 'S:4', 'S:6')
        for lead_cards in (cards('H:4'), cards('S:9', 'H:9', 'C:9', 'D:9')):
            lead = prepare_lead(lead_cards, lead_cards, ((), (), ()), ctx)
            choices = (cards('C:4'), cards('S:4')) if len(lead_cards) == 1 else (hand,)
            for chosen in choices:
                self.assertEqual(validate_follow(hand, chosen, lead, ctx), chosen)


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.registry = FriendRooms(seed=7)
        self.tokens = [self.registry.session(None)[0] for _ in range(3)]
        self.code = self.registry.request(self.tokens[0], {'command': 'create', 'name': '张房主'})['room_code']
        self.registry.request(self.tokens[1], {'command': 'join', 'name': '李朋友', 'room_code': self.code})
        self.room = self.registry.rooms[self.code]

    def send(self, token, text, **extra):
        return self.registry.request(token, {'command': 'chat', 'room_code': self.code, 'text': text, **extra})

    def test_authenticated_room_messages_do_not_change_game_version(self):
        before, version = self.room.game, self.room.version
        view = self.send(self.tokens[1], '<img src=x onerror=alert(1)>')
        message = view['chat_messages'][0]
        self.assertEqual(message['name'], '李朋友')
        self.assertEqual(message['seat'], 1)
        self.assertEqual(message['text'], '<img src=x onerror=alert(1)>')
        self.assertEqual(self.registry.state(self.tokens[0])['chat_messages'], view['chat_messages'])
        self.assertIs(self.room.game, before)
        self.assertEqual(self.room.version, version)
        self.assertEqual(self.registry.state(self.tokens[2])['mode'], 'lobby')
        with self.assertRaises(RuleViolation):
            self.send(self.tokens[2], '不在房间')
        with self.assertRaises(RuleViolation):
            self.send(self.tokens[0], '冒名', name='其他人')

    def test_validation_cooldown_and_bounded_history(self):
        for text in ('', '  ', '字'*201, '\x00', 1, None):
            with self.subTest(text=text), self.assertRaises(RuleViolation):
                self.send(self.tokens[0], text)
        with patch('sanwufan.friends.time.time', return_value=100):
            self.send(self.tokens[0], '  你好\n朋友  ')
            self.assertEqual(self.room.chat_messages[-1]['text'], '你好 朋友')
            with self.assertRaises(RuleViolation):
                self.send(self.tokens[0], '再发')
            self.send(self.tokens[1], '你好')
        for i in range(90):
            with patch('sanwufan.friends.time.time', return_value=102+i):
                self.send(self.tokens[0], f'消息{i}')
        self.assertEqual(len(self.room.chat_messages), 80)
        self.assertEqual(self.room.chat_messages[-1]['id'], 92)

    def test_checkpoint_keeps_chat_and_storage_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'chat.sqlite3'
            registry = FriendRooms(store=SQLiteStore(path))
            token = registry.session(None)[0]
            code = registry.request(token, {'command': 'create', 'name': '张房主'})['room_code']
            payload = {'command': 'chat', 'room_code': code, 'text': '持久化消息'}
            registry.request(token, payload)
            from sanwufan.storage import StorageError
            room = registry.rooms[code]
            with patch.object(registry.store, 'write', side_effect=StorageError('test')):
                with patch('sanwufan.friends.time.time', return_value=room.chat_messages[-1]['at']+2):
                    with self.assertRaises(StorageError):
                        registry.request(token, {**payload, 'text': '不能确认的消息'})
            self.assertEqual(room.chat_sequence, 1)
            registry.close()
            recovered = FriendRooms(store=SQLiteStore(path))
            self.assertEqual(recovered.state(token)['chat_messages'][0]['text'], '持久化消息')
            recovered.close()
