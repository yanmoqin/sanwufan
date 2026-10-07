from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sanwufan.friends import FriendRooms
from sanwufan.game import Game, Phase
from sanwufan.models import RuleContext, RuleViolation
from sanwufan.practice import DEAL_INTERVAL, PracticeRoom
from sanwufan.storage import SQLiteStore, StorageError
from sanwufan.suggestions import special_hints
from tests.test_game import cards, fixed_order, prepared, play_entire_deal


class TableEnhancementTests(unittest.TestCase):
    def setUp(self):
        self.registry = FriendRooms(seed=7)
        self.tokens = [self.registry.session(None)[0] for _ in range(5)]
        view = self.registry.request(self.tokens[0], {'command': 'create', 'name': '房主'})
        self.code = view['room_code']
        self.room = self.registry.rooms[self.code]
        for p in range(1, 4):
            self.join(p)

    def join(self, p):
        return self.registry.request(self.tokens[p], {'command': 'join', 'name': f'朋友{p}', 'room_code': self.code})

    def command(self, p, command, target=1, **extra):
        v = self.registry.state(self.tokens[p])
        payload = {'command': command, 'target': target, 'target_id': v['players'][target]['occupant_id'],
                   'table_id': v['table_id']}
        if command == 'kick':
            payload['version'] = v['version']
        else:
            payload['item'] = 'egg'
        payload.update(extra)
        return self.registry.request(self.tokens[p], payload)

    def test_first_two_claims_in_deal_order_and_card_cadence(self):
        order = fixed_order((cards('H:4'), cards('D:4'), cards('C:2'), cards('H:2')))
        self.room.game = Game(ready_seats=frozenset(range(4))).start_deal(order)
        self.room.tick(0)
        self.assertEqual(self.room.game.dealt_count, 1)
        self.room.tick(DEAL_INTERVAL / 2)
        self.assertEqual(self.room.game.dealt_count, 1)
        self.room.tick(DEAL_INTERVAL + .01)
        self.room.tick(DEAL_INTERVAL * 2 + .02)
        self.assertEqual(self.room.game.caller, 2)
        self.assertEqual(self.room.game.called_two.id, 'C:2')
        self.assertEqual(self.room.game.phase, Phase.DECLARING)
        self.assertEqual(self.room.game.dealt_count, 48)
        self.assertEqual([len(h) for h in self.room.game.hands], [12] * 4)
        self.assertIsNone(self.room.claim_deadline)
        self.room.tick(DEAL_INTERVAL * 3 + .03)
        self.assertEqual(self.room.game.caller, 2)

    def test_later_deal_requires_manual_claim(self):
        order = fixed_order((cards('H:2'), (), (), ()))
        self.room.game = Game(number=1, dealer=2, ready_seats=frozenset(range(4))).start_deal(order)
        self.room.tick(0)
        self.assertIsNone(self.room.game.called_two)
        self.assertIn('call_trump', self.room.available(0))

    def test_three_automatically_reveals_before_tribute_without_duplicate(self):
        order = fixed_order((cards('H:2', 'S:3', 'C:3', 'D:3', 'S:5', 'H:5', 'C:5'), cards('H:3', 'D:5'), (), ()))
        self.room.game = prepared(order=order)
        self.room._auto_reflections()
        own = [r for r in self.room.game.reflections if r.seat == 0]
        self.assertEqual({r.cards[0].rank for r in own}, {'3', '5'})
        before = self.room.game
        self.room._auto_reflections()
        self.assertIs(self.room.game, before)
        self.assertEqual(len(self.registry.state(self.tokens[0])['special_hints']['revealed']), 2)
        for p in range(4):
            self.room._perform(p, 'confirm')
        self.assertEqual(self.room.game.phase, Phase.TAKE_BOTTOM)

    def test_four_waits_for_choice_with_both_recommended_kept_cards(self):
        wanted = cards('H:2', 'S:3', 'H:3', 'C:3', 'D:3', 'S:5', 'H:5', 'C:5', 'D:5')
        self.room.game = prepared(order=fixed_order((wanted, (), (), ())))
        self.room._auto_reflections()
        self.assertFalse(self.room.game.reflections)
        own = self.registry.state(self.tokens[0])['special_hints']
        groups = {r['rank']: r for r in own['reflections']}
        self.assertEqual(groups['3']['choices'][0]['keep'], 'H:3')
        self.assertEqual(groups['5']['choices'][0]['keep'], 'D:5')
        self.assertEqual(len(groups['3']['choices']), 4)
        self.assertEqual({g['rank'] for g in own['gangs'] if g['kind'] == 'true_gang'}, {'3', '5'})
        for rank in ('3', '5'):
            self.room._perform(0, 'reveal', cards(*groups[rank]['choices'][0]['cards']))
        self.assertNotIn(cards('H:3')[0], self.room.game.context.reflected_cards)
        self.assertNotIn(cards('D:5')[0], self.room.game.context.reflected_cards)
        self.assertFalse(special_hints(self.room.game, 0)['gangs'])

    def test_taking_bottom_can_complete_an_automatic_reflection(self):
        bottom = cards('D:3', 'H:4', 'D:4', 'C:4', 'D:6', 'C:6')
        order = fixed_order((cards('H:2', 'S:3', 'H:3'), cards('C:3'), (), ()), bottom)
        self.room.game = prepared(order=order)
        self.room._auto_reflections()
        self.assertFalse(any(r.seat == 0 and r.cards[0].rank == '3' for r in self.room.game.reflections))
        for p in range(4):
            self.room._perform(p, 'confirm')
        self.room._perform(0, 'take_bottom')
        own = [r for r in self.room.game.reflections if r.seat == 0 and r.cards[0].rank == '3']
        self.assertEqual(len(own), 1)
        self.assertEqual(set(own[0].cards), set(cards('S:3', 'H:3', 'D:3')))

    def test_reflections_wait_for_full_hand_so_fourth_card_can_be_kept(self):
        wanted = cards('H:2', 'S:3', 'H:3', 'C:3', 'D:3')
        order = fixed_order((wanted, (), (), ()))
        self.room.game = Game(ready_seats=frozenset(range(4))).start_deal(order)
        for n in range(49):
            self.room.tick(n)
        self.assertEqual(self.room.game.dealt_count, 48)
        self.assertFalse(self.room.game.reflections)
        self.room.tick(60)
        self.assertEqual(self.room.game.phase, Phase.DECLARING)
        self.assertFalse(any(r.seat == 0 and r.cards[0].rank == '3' for r in self.room.game.reflections))

    def test_another_players_confirmation_does_not_cancel_an_owned_reflection_choice(self):
        wanted = cards('H:2', 'S:3', 'H:3', 'C:3', 'D:3')
        self.room.game = prepared(order=fixed_order((wanted, (), (), ())))
        old = self.registry.state(self.tokens[0])
        self.room._perform(1, 'confirm')
        choice = next(r for r in old['special_hints']['reflections'] if r['rank'] == '3')['choices'][0]
        payload = {'action': 'reveal', 'version': old['version'], 'table_id': old['table_id'], 'cards': choice['cards']}
        self.registry.request(self.tokens[0], payload)
        self.assertEqual(len([r for r in self.room.game.reflections if r.seat == 0 and r.cards[0].rank == '3']), 1)
        with self.assertRaises(RuleViolation):
            self.registry.request(self.tokens[0], payload)

    def test_gangs_are_private_and_exchange_cards_cannot_form_them(self):
        wanted = cards('H:2', 'S:Q', 'H:Q', 'C:Q', 'D:Q', 'S:A', 'H:A', 'C:A', 'D:A')
        self.room.game = prepared(order=fixed_order((wanted, (), (), ())))
        hints = self.registry.state(self.tokens[0])['special_hints']['gangs']
        self.assertEqual({g['kind'] for g in hints if g['rank'] == 'Q'}, {'true_gang', 'false_gang'})
        for p in range(1, 4):
            others = self.registry.state(self.tokens[p])['special_hints']['gangs']
            self.assertFalse(any(g['rank'] in ('Q', 'A') for g in others))
        ctx = replace(self.room.game.context, exchanged_cards=frozenset(cards('S:Q', 'H:A')))
        self.room.game = replace(self.room.game, context=ctx)
        self.assertFalse(special_hints(self.room.game, 0)['gangs'])
        self.assertNotIn(self.tokens[0], json.dumps(self.registry.state(self.tokens[1])))

    def test_owner_kick_clears_ready_revokes_recovery_and_blocks_same_identity(self):
        old_key = self.registry.state(self.tokens[1])['recovery_code']
        self.room.game = self.room.game.ready(0).ready(1)
        result = self.command(0, 'kick')
        self.assertIsNone(self.room.members[1])
        self.assertEqual(result['ready_seats'], [])
        self.assertEqual(self.registry.state(self.tokens[1])['mode'], 'lobby')
        self.assertIn('请', self.registry.state(self.tokens[1])['notice'])
        with self.assertRaises(RuleViolation):
            self.join(1)
        with self.assertRaises(RuleViolation):
            self.registry.request(self.tokens[4], {'command': 'recover', 'recovery_code': old_key})
        self.room.action_for(self.tokens[0], {'action': 'reset', 'table_id': result['table_id'], 'version': result['version']})
        self.assertEqual(self.join(1)['player_seat'], 1)

    def test_kick_rejects_nonowner_stale_target_self_and_active_deal(self):
        for p, target in ((1, 2), (0, 0)):
            with self.assertRaises(RuleViolation):
                self.command(p, 'kick', target)
        for extra in ({'version': -1}, {'target_id': 'old'}, {'target': True}):
            with self.assertRaises(RuleViolation):
                self.command(0, 'kick', **extra)
        self.room.game = prepared()
        before = self.room.game
        with self.assertRaisesRegex(RuleViolation, '进行中'):
            self.command(0, 'kick')
        self.assertIs(self.room.game, before)
        self.assertEqual(len([m for m in self.room.members if m]), 4)

    def test_kick_is_allowed_after_settlement(self):
        self.room.game = play_entire_deal(prepared())
        self.command(0, 'kick')
        self.assertEqual(self.room.game.phase, Phase.WAITING)
        self.assertIsNone(self.room.members[1])

    def test_interaction_does_not_change_game_version_and_cooldown_is_per_sender(self):
        before = self.room.game
        version = self.room.version
        with patch('sanwufan.friends.time.time', return_value=2000000000):
            self.command(0, 'emote')
            with self.assertRaisesRegex(RuleViolation, '3秒'):
                self.command(0, 'emote', item='tomato')
            other = self.command(2, 'emote', target=0, item='flower')
            self.assertEqual([e['item'] for e in other['social_events']], ['egg', 'flower'])
        self.assertIs(self.room.game, before)
        self.assertEqual(self.room.version, version)
        with patch('sanwufan.friends.time.time', return_value=2000000004):
            self.command(0, 'emote', item='tomato')
        with patch('sanwufan.friends.time.time', return_value=2000000015):
            self.assertFalse(self.registry.state(self.tokens[0])['social_events'])

    def test_invalid_interactions_do_not_emit_or_consume_cooldown(self):
        for extra in ({'target': 0}, {'target': True}, {'target': -1}, {'item': 'script'}, {'item': []}, {'target_id': 'old'}):
            with self.assertRaises(RuleViolation):
                self.command(0, 'emote', **extra)
        self.assertEqual(self.room.social_sequence, 0)
        self.assertFalse(self.room.emote_last)


class EnhancementPersistenceTests(unittest.TestCase):
    def test_old_checkpoint_without_new_fields_remains_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'old.sqlite3'
            registry = FriendRooms(store=SQLiteStore(path))
            token = registry.session(None)[0]
            v = registry.request(token, {'command': 'create', 'name': '旧房主'})
            registry.close()
            store = SQLiteStore(path)
            try:
                old = store.read()
                for room in old['rooms']:
                    for key in ('blocked', 'social_sequence', 'social_events', 'emote_last'):
                        room.pop(key)
                store.write(old)
            finally:
                store.close()
            restored = FriendRooms(store=SQLiteStore(path))
            try:
                view = restored.state(token)
                self.assertEqual(view['room_code'], v['room_code'])
                self.assertEqual(view['social_sequence'], 0)
                self.assertTrue(view['can_kick'])
            finally:
                restored.close()

    def test_kick_rollback_and_restart_restore_blocked_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.sqlite3'
            registry = FriendRooms(store=SQLiteStore(path))
            tokens = [registry.session(None)[0] for _ in range(2)]
            v = registry.request(tokens[0], {'command': 'create', 'name': '房主'})
            registry.request(tokens[1], {'command': 'join', 'name': '朋友', 'room_code': v['room_code']})
            v = registry.state(tokens[0])
            payload = {'command': 'kick', 'target': 1, 'target_id': v['players'][1]['occupant_id'],
                       'version': v['version'], 'table_id': v['table_id']}
            before_key = registry.state(tokens[1])['recovery_code']
            try:
                with patch.object(registry.store, 'write', side_effect=StorageError('test')):
                    with self.assertRaises(StorageError):
                        registry.request(tokens[0], payload)
                self.assertEqual(registry.state(tokens[1])['recovery_code'], before_key)
                self.assertEqual(registry.state(tokens[1])['player_seat'], 1)
                self.assertFalse(registry.rooms[v['room_code']].blocked)
                registry.request(tokens[0], payload)
            finally:
                registry.close()
            restored = FriendRooms(store=SQLiteStore(path))
            try:
                self.assertEqual(restored.state(tokens[1])['mode'], 'lobby')
                with self.assertRaises(RuleViolation):
                    restored.request(tokens[1], {'command': 'join', 'name': '朋友', 'room_code': v['room_code']})
            finally:
                restored.close()
