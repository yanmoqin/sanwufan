import unittest
from unittest.mock import patch

from sanwufan.game import Game, Phase
from sanwufan.models import Reflection, RuleContext, RuleViolation, Suit, deck
from sanwufan.practice import PracticeRoom, legal_play
from sanwufan.rules import is_trump
from sanwufan.trick import Trick
from tests.test_game import cards, declarations_done, fixed_order, prepared


class TableReviewTests(unittest.TestCase):
    def test_three_reflection_beats_every_other_trump_except_five_and_diamond_five(self):
        for suit in (Suit.SPADES, Suit.HEARTS, Suit.CLUBS, Suit.DIAMONDS):
            reflected = cards('S:3', 'C:3', 'D:3')
            fives = cards('S:5', 'H:5', 'C:5')
            ctx = RuleContext(suit, (Reflection(0, reflected), Reflection(1, fives)))
            for shown in reflected:
                for challenger in deck():
                    if challenger in reflected or not is_trump(challenger, ctx):
                        continue
                    with self.subTest(trump=suit, shown=shown.id, challenger=challenger.id):
                        fillers = [c for c in deck() if c.suit == suit and c.rank in ('4', '6', '7')
                                   and c not in (shown, challenger)][:2]
                        for leader in (0, 1):
                            trick = Trick(ctx, ((shown,), (challenger,), (fillers[0],), (fillers[1],)), leader)
                            for turn in range(4):
                                seat = (leader + turn) % 4
                                trick = trick.play(seat, trick.hands[seat])
                            expected = 1 if challenger.id == 'D:5' or challenger in fives else 0
                            self.assertEqual(trick.result().winner, expected)

    def test_three_reflection_retains_strength_before_trump_and_after_taking_bottom(self):
        for stage in ('before_trump', 'declaring', 'after_bottom'):
            with self.subTest(stage=stage):
                bottom = cards('D:3', 'D:4', 'D:6', 'D:7', 'D:8', 'D:9') if stage == 'after_bottom' else ()
                wanted = cards('H:2', 'S:3', 'H:3') + (() if bottom else cards('D:3'))
                order = fixed_order((wanted, cards('J:big'), cards('S:Q', 'C:3'), cards('H:J')), bottom)
                if stage == 'before_trump':
                    g = Game(ready_seats=frozenset(range(4))).start_deal(order)
                    for _ in range(48):
                        g = g.deal_one()
                    g = g.reveal(0, cards('S:3', 'H:3', 'D:3'))
                    g = g.call_trump(0, cards('H:2')[0]).finish_dealing()
                else:
                    g = prepared(order=order)
                    if stage == 'declaring':
                        g = g.reveal(0, cards('S:3', 'H:3', 'D:3'))
                g = declarations_done(g).take_bottom(0)
                if stage == 'after_bottom':
                    g = g.reveal(0, cards('S:3', 'H:3', 'D:3'))
                discard = tuple(c for c in g.hands[0] if not c.points and c not in g.context.reflected_cards)[:6]
                g = g.discard(0, discard)
                for seat, selected in enumerate(('S:3', 'J:big', 'S:Q', 'H:J')):
                    g = g.play(seat, cards(selected))
                self.assertEqual(g.tricks[-1].winner, 0)
                self.assertEqual(g.trick.context, g.context)
                for seat in range(4):
                    self.assertEqual(g.view_for(seat)['reflections'][0]['cards'], ['S:3', 'H:3', 'D:3'])

    def test_bottom_becomes_public_only_after_discard_and_history_preserves_play_order(self):
        g = prepared()
        for phase in (g, declarations_done(g), declarations_done(g).take_bottom(g.dealer)):
            for seat in range(4):
                self.assertEqual(phase.view_for(seat)['bottom_cards'], [])
        g = declarations_done(g).take_bottom(g.dealer)
        bottom = tuple(c for c in g.hands[g.dealer] if not c.points)[:6]
        g = g.discard(g.dealer, bottom)
        for _ in range(8):
            seat = g.trick.next_seat
            g = g.play(seat, legal_play(g, seat))
        for seat in range(4):
            view = g.view_for(seat)
            self.assertEqual(view['bottom_cards'], [c.id for c in bottom])
            self.assertEqual(len(view['trick_history']), 2)
            for n, t in enumerate(g.tricks):
                record = view['trick_history'][n]
                self.assertEqual(record['round'], n + 1)
                self.assertEqual(record['winner'], t.winner)
                self.assertEqual(record['points'], t.points)
                self.assertEqual(record['plays'], [{'seat': p.seat, 'cards': [c.id for c in p.cards]} for p in t.plays])
                self.assertNotIn('hands', record)
            self.assertNotIn('hands', view)
            self.assertNotIn('stock', view)
        while g.phase != Phase.SETTLED:
            seat = g.trick.next_seat
            g = g.play(seat, legal_play(g, seat))
        self.assertEqual(len(g.view_for(1)['trick_history']), len(g.tricks))
        self.assertEqual(g.next_deal().view_for(1)['trick_history'], [])

    def test_practice_interactions_do_not_change_game_or_auto_and_enforce_cooldown(self):
        room = PracticeRoom(7)
        room.auto = True
        before, version = room.game, room.version
        payload = {'command': 'emote', 'target': 1, 'item': 'egg', 'table_id': room.table_id}
        with patch('sanwufan.practice.time.time', return_value=100):
            view = room.action(payload)
            self.assertEqual(view['social_events'][0]['to'], 1)
            self.assertEqual(view['social_events'][0]['item'], 'egg')
            self.assertEqual(view['social_sequence'], 1)
            with self.assertRaises(RuleViolation) as caught:
                room.action(payload)
            self.assertEqual(caught.exception.code, 'EMOTE_TOO_FAST')
        with patch('sanwufan.practice.time.time', return_value=103):
            self.assertEqual(room.action({**payload, 'target': 3, 'item': 'tomato'})['social_sequence'], 2)
        with patch('sanwufan.practice.time.time', return_value=112):
            self.assertEqual(room.snapshot()['social_events'], [])
        self.assertIs(room.game, before)
        self.assertEqual(room.version, version)
        self.assertTrue(room.auto)

    def test_practice_rejects_wrong_table_invalid_target_and_extra_fields(self):
        room = PracticeRoom(7)
        payload = {'command': 'emote', 'target': 1, 'item': 'egg', 'table_id': room.table_id}
        for updates in ({'table_id': 'old'}, {'target': 0}, {'target': True}, {'target': 4},
                        {'item': 'unknown'}, {'target_id': 'anything'}):
            with self.subTest(updates=updates), self.assertRaises(RuleViolation):
                room.action({**payload, **updates})
        self.assertEqual(room.social_sequence, 0)


if __name__ == '__main__':
    unittest.main()
