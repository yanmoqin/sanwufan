import unittest

from sanwufan.friends import FriendRooms
from sanwufan.game import Game, Phase
from sanwufan.models import RuleViolation
from sanwufan.settlement import Tribute
from sanwufan.practice import PracticeRoom
from tests.test_game import cards, declarations_done, fixed_order, prepared


class DealAndReflectionTests(unittest.TestCase):
    def registry(self):
        registry = FriendRooms(seed=7)
        tokens = [registry.session(None)[0] for _ in range(4)]
        view = registry.request(tokens[0], {'command': 'create', 'name': '庄家'})
        for p in range(1, 4):
            registry.request(tokens[p], {'command': 'join', 'name': f'朋友{p}', 'room_code': view['room_code']})
        return registry, tokens, registry.rooms[view['room_code']]

    def test_manual_trump_immediately_finishes_in_order_and_keeps_bottom_private(self):
        registry, tokens, room = self.registry()
        order = fixed_order((cards('H:2'), cards('C:2'), (), ()))
        room.game = Game(number=1, dealer=2, ready_seats=frozenset(range(4))).start_deal(order).deal_one()
        old = registry.state(tokens[0])
        result = registry.request(tokens[0], {'action': 'call_trump', 'table_id': old['table_id'],
                    'version': old['version'], 'cards': ['H:2']})
        self.assertEqual(result['phase'], 'declaring')
        self.assertEqual(result['hand_counts'], [12] * 4)
        self.assertEqual(room.game.dealer, 2)
        self.assertEqual(room.game.caller, 0)
        self.assertEqual(room.game.bottom, order[48:])
        for p in range(4):
            state = registry.state(tokens[p])
            self.assertEqual(set(state['hand']), {c.id for c in order[p:48:4]})
            self.assertEqual(state['bottom_cards'], [])
            self.assertNotIn('stock', state)
        with self.assertRaises(RuleViolation):
            room._perform(1, 'call_trump', cards('C:2'))
        self.assertEqual(room.game.caller, 0)

    def test_practice_manual_claim_and_bot_claim_both_skip_remaining_animation(self):
        order = fixed_order((cards('H:2'), cards('C:2'), (), ()))
        for automatic in (False, True):
            with self.subTest(bot=automatic):
                room = PracticeRoom()
                room.game = Game(number=1, dealer=0, ready_seats=frozenset(range(4))).start_deal(order).deal_one().deal_one()
                if automatic:
                    room.tick(10)
                    self.assertEqual(room.game.phase, Phase.DEALING)
                    room.tick(12)
                else:
                    room._perform(0, 'call_trump', cards('H:2'))
                self.assertEqual(room.game.phase, Phase.DECLARING)
                self.assertEqual(room.game.dealt_count, 48)
                self.assertIsNone(room.claim_deadline)

    def test_bottom_completes_both_automatic_reflections_after_confirmation(self):
        registry, tokens, room = self.registry()
        bottom = cards('D:3', 'D:5', 'H:4', 'C:4', 'D:4', 'D:6')
        order = fixed_order((cards('H:2', 'S:3', 'H:3', 'S:5', 'H:5'), cards('C:3', 'C:5'), (), ()), bottom)
        room.game = declarations_done(prepared(order=order))
        self.assertFalse(room.game.reflections)
        room._perform(0, 'take_bottom')
        own = registry.state(tokens[0])
        self.assertEqual(own['phase'], 'discard')
        self.assertEqual({r['cards'][0].split(':')[1] for r in own['reflections'] if r['seat'] == 0}, {'3', '5'})
        for p in (1, 2, 3):
            view = registry.state(tokens[p])
            self.assertEqual(len(view['reflections']), 2)
            self.assertEqual(view['bottom_cards'], [])
            self.assertEqual(len(view['hand']), 12)

    def test_bottom_four_of_each_requires_choice_with_recommended_kept_cards(self):
        registry, tokens, room = self.registry()
        bottom = cards('C:3', 'D:3', 'C:5', 'D:5', 'H:4', 'D:4')
        order = fixed_order((cards('H:2', 'S:3', 'H:3', 'S:5', 'H:5'), (), (), ()), bottom)
        room.game = declarations_done(prepared(order=order))
        room._perform(0, 'take_bottom')
        own = registry.state(tokens[0])
        self.assertFalse(room.game.reflections)
        groups = {r['rank']: r for r in own['special_hints']['reflections']}
        self.assertEqual(groups['3']['choices'][0]['keep'], 'H:3')
        self.assertEqual(groups['5']['choices'][0]['keep'], 'D:5')
        for rank in ('3', '5'):
            room._perform(0, 'reveal', cards(*groups[rank]['choices'][0]['cards']))
        self.assertEqual(len(room.game.reflections), 2)
        self.assertIn('H:3', room.snapshot()['hand'])
        self.assertIn('D:5', room.snapshot()['hand'])

    def test_non_dealer_can_reveal_after_own_confirmation_until_actual_discard(self):
        registry, tokens, room = self.registry()
        wanted = (cards('H:2'), cards('S:3', 'H:3', 'C:3', 'D:3'), (), ())
        room.game = prepared(order=fixed_order(wanted))
        room._perform(1, 'confirm')
        old = registry.state(tokens[1])
        for p in (0, 2, 3):
            room._perform(p, 'confirm')
        room._perform(0, 'take_bottom')
        self.assertIn('reveal', room.available(1))
        registry.request(tokens[1], {'action': 'reveal', 'version': old['version'],
                        'table_id': old['table_id'], 'cards': ['S:3', 'C:3', 'D:3']})
        room._perform(0, 'discard', tuple(c for c in room.game.hands[0] if not c.points)[:6])
        self.assertNotIn('reveal', room.available(1))
        self.assertFalse(room.snapshot(1)['special_hints']['reflections'])
        with self.assertRaises(RuleViolation) as error:
            room.game.reveal(1, cards('S:3', 'C:3', 'D:3'))
        self.assertEqual(error.exception.code, 'WRONG_PHASE')

    def test_reflection_during_tribute_cancels_all_uncommitted_gifts(self):
        wanted = (cards('D:5', 'H:2', 'S:3', 'H:3', 'C:3', 'D:3'), cards('H:J'), (), ())
        start = declarations_done(prepared(Game(dealer=1, obligations=(Tribute(0, 1),)), fixed_order(wanted)))
        self.assertEqual(start.phase, Phase.TRIBUTE_GIVE)
        for game in (start, start.give_tribute(0, cards('D:5')[0])):
            with self.subTest(phase=game.phase):
                result = game.reveal(0, cards('S:3', 'C:3', 'D:3'))
                self.assertEqual(result.phase, Phase.TAKE_BOTTOM)
                self.assertEqual(result.exemption, 'reflection')
                self.assertFalse(result.exchanges)
                self.assertEqual(result.hands, start.hands)
                self.assertEqual(result.view_for(0)['hand_counts'], [12] * 4)
                result.check_invariants()

    def test_receiver_late_reflection_with_no_legal_return_exempts_all_tribute(self):
        receiver = cards('H:3', 'S:3', 'C:3', 'S:4', 'S:6', 'S:7', 'S:8', 'S:9', 'S:10', 'S:K', 'S:A', 'C:4')
        game = declarations_done(prepared(Game(dealer=1, obligations=(Tribute(0, 1),)),
                    fixed_order((cards('H:2', 'D:5'), receiver, (), ()))))
        game = game.give_tribute(0, cards('D:5')[0])
        game = game.reveal(1, cards('H:3', 'S:3', 'C:3'))
        self.assertEqual(game.phase, Phase.TAKE_BOTTOM)
        self.assertEqual(game.exemption, 'no_eligible_return')
        self.assertFalse(game.exchanges)
        game.check_invariants()

    def test_a_staged_transfer_is_not_offered_for_reflection(self):
        wanted = (cards('H:3', 'S:3', 'C:3', 'D:3', 'S:4', 'S:6', 'S:7', 'S:8', 'S:9', 'S:10', 'S:K', 'S:A'), cards('H:2'), (), ())
        game = declarations_done(prepared(Game(dealer=1, obligations=(Tribute(0, 1),)), fixed_order(wanted)))
        game = game.give_tribute(0, cards('H:3')[0])
        room = PracticeRoom()
        room.game = game
        choices = room.snapshot(0)['special_hints']['reflections'][0]['choices']
        self.assertFalse(any('H:3' in choice['cards'] for choice in choices))
        with self.assertRaises(RuleViolation):
            game.reveal(0, cards('H:3', 'S:3', 'C:3'))


if __name__ == '__main__':
    unittest.main()
