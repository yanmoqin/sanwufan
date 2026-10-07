import json
import random
import unittest
from dataclasses import FrozenInstanceError

from sanwufan import (
    Card, Lead, PlayKind, Reflection, RuleContext, RuleViolation, Suit, Trick,
    declare_reflection, deck, group, is_false_gang, is_true_gang, is_trump,
    prepare_lead, settle, strength, validate_bottom, validate_follow,
)


def cards(*ids):
    return tuple(Card.from_id(x) for x in ids)


def gang(rank):
    return cards(*(f"{s}:{rank}" for s in ("S", "H", "C", "D")))


class RuleFixture:
    def setUp(self):
        self.ctx = RuleContext(Suit.CLUBS)

    def violation(self, code, fn, *args, **kwargs):
        with self.assertRaises(RuleViolation) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)


class RuleTest(RuleFixture, unittest.TestCase):

    def test_deck_and_wire_ids(self):
        self.assertEqual(len(deck()), 54)
        self.assertEqual(len(set(deck())), 54)
        self.assertEqual(sum(c.points for c in deck()), 100)
        for c in deck():
            self.assertEqual(Card.from_id(c.id), c)

    def test_bad_card_ids_are_rejected(self):
        for value in ("S:big", "J:A", "Z:A", "S:1", "S:A:4", "SA", "S:a", 5):
            with self.subTest(value=value):
                self.violation("INVALID_CARD", Card.from_id, value)

    def test_context_rejects_joker_trump(self):
        self.violation("INVALID_TRUMP", RuleContext, Suit.JOKER)

    def test_common_trumps_for_every_suit(self):
        common = cards("D:5", "J:big", "J:small", "S:Q") + gang("J") + gang("2")
        for suit in (Suit.SPADES, Suit.HEARTS, Suit.CLUBS, Suit.DIAMONDS):
            ctx = RuleContext(suit)
            for c in deck():
                self.assertEqual(is_trump(c, ctx), c in common or c.suit == suit)
                self.assertEqual(group(c, ctx), "T" if is_trump(c, ctx) else c.suit.value)

    def test_full_trump_hierarchy(self):
        ctx = RuleContext(Suit.CLUBS, (
            Reflection(0, cards("S:5", "H:5", "C:5")),
            Reflection(1, cards("S:3", "H:3", "D:3")),
        ))
        descending = cards("D:5", "H:5", "H:3", "J:big", "J:small", "S:Q", "C:J", "H:J", "C:2", "H:2", "C:A", "C:K", "C:Q", "C:10", "C:9", "C:8", "C:7", "C:6", "C:4", "C:3")
        for upper, lower in zip(descending, descending[1:]):
            self.assertGreater(strength(upper, ctx), strength(lower, ctx))

    def test_equal_nontrump_jacks_and_twos(self):
        for rank in ("J", "2"):
            self.assertEqual(strength(cards(f"H:{rank}")[0], self.ctx), strength(cards(f"S:{rank}")[0], self.ctx))

    def test_only_declared_reflection_cards_are_promoted(self):
        hand = cards("H:3", "S:3", "D:3", "H:A")
        ctx = declare_reflection(self.ctx, 0, hand, hand[:3])
        self.assertTrue(is_trump(hand[0], ctx))
        self.assertFalse(is_trump(hand[0], self.ctx))
        self.assertEqual(strength(cards("C:3")[0], ctx), (1, 0))
        self.assertEqual(strength(hand[0], ctx), strength(hand[1], ctx))

    def test_bad_reflections_are_rejected(self):
        for selected in (cards("H:3", "S:3"), cards("H:4", "S:4", "D:4"), cards("H:3", "S:3", "D:5")):
            self.violation("INVALID_REFLECTION", Reflection, 0, selected)
        self.violation("DUPLICATE_CARD", Reflection, 0, cards("H:3", "H:3", "D:3"))
        self.violation("NOT_OWNED", declare_reflection, self.ctx, 0, cards("H:A"), cards("H:3", "S:3", "D:3"))

    def test_exchanged_cards_cannot_form_reflection(self):
        hand = cards("H:3", "S:3", "D:3")
        ctx = RuleContext(Suit.CLUBS, exchanged_cards=frozenset(hand[:1]))
        self.violation("EXCHANGED_REFLECTION", declare_reflection, ctx, 0, hand, hand)

    def test_diamond_five_in_reflection_stays_highest_and_scores(self):
        ctx = RuleContext(Suit.CLUBS, (Reflection(0, cards("D:5", "H:5", "S:5")),))
        self.assertGreater(strength(cards("D:5")[0], ctx), strength(cards("H:5")[0], ctx))
        self.assertEqual(cards("D:5")[0].points, 5)
        self.assertEqual(cards("H:5")[0].points, 5)

    def test_single_follow_requires_leading_group(self):
        lead = Lead(cards("H:K"), PlayKind.SINGLE)
        self.violation("MUST_FOLLOW", validate_follow, cards("H:4", "J:big"), cards("J:big"), lead, self.ctx)
        self.assertEqual(validate_follow(cards("H:4", "J:big"), cards("H:4"), lead, self.ctx), cards("H:4"))

    def test_common_trumps_do_not_count_as_original_side_suit(self):
        lead = Lead(cards("H:K"), PlayKind.SINGLE)
        self.assertEqual(validate_follow(cards("H:J", "S:A"), cards("S:A"), lead, self.ctx), cards("S:A"))

    def test_void_side_suit_can_discard_or_trump(self):
        lead = Lead(cards("H:K"), PlayKind.SINGLE)
        for chosen in (cards("S:A"), cards("J:big")):
            self.assertEqual(validate_follow(cards("S:A", "J:big"), chosen, lead, self.ctx), chosen)

    def test_trump_lead_requires_any_trump(self):
        lead = Lead(cards("C:A"), PlayKind.SINGLE)
        self.violation("MUST_FOLLOW", validate_follow, cards("S:Q", "H:A"), cards("H:A"), lead, self.ctx)
        self.assertEqual(validate_follow(cards("S:Q", "H:A"), cards("S:Q"), lead, self.ctx), cards("S:Q"))

    def test_follow_checks_ownership_count_and_duplicate_ids(self):
        lead = Lead(cards("H:K"), PlayKind.SINGLE)
        self.violation("NOT_OWNED", validate_follow, cards("H:A"), cards("S:A"), lead, self.ctx)
        self.violation("WRONG_COUNT", validate_follow, cards("H:A"), (), lead, self.ctx)
        self.violation("DUPLICATE_CARD", validate_follow, cards("H:A", "S:A"), cards("H:A", "H:A"), lead, self.ctx)

    def test_valid_throw_uses_all_opponents(self):
        chosen = cards("H:K", "H:Q", "H:9")
        lead = prepare_lead(chosen, chosen, [cards("H:8"), cards("S:A"), cards("H:6")], self.ctx)
        self.assertEqual(lead.kind, PlayKind.THROW)
        self.assertFalse(lead.throw_failed)

    def test_throw_failure_falls_back_to_smallest(self):
        chosen = cards("S:K", "S:10", "S:8")
        lead = prepare_lead(chosen, chosen, [cards("H:A"), cards("S:9"), cards("D:A")], self.ctx)
        self.assertEqual(lead.cards, cards("S:8"))
        self.assertEqual(lead.kind, PlayKind.SINGLE)
        self.assertTrue(lead.throw_failed)

    def test_side_throw_does_not_compare_constant_trumps_as_side_cards(self):
        selected = cards("S:K", "S:10", "S:9")
        lead = prepare_lead(selected, selected, [cards("S:Q"), cards("S:J"), cards("S:2")], self.ctx)
        self.assertFalse(lead.throw_failed)

    def test_throw_rejects_mixed_suits_and_multiple_trumps(self):
        for chosen in (cards("H:K", "S:K"), cards("C:K", "C:Q")):
            self.violation("INVALID_THROW", prepare_lead, chosen, chosen, [(), (), ()], self.ctx)

    def test_partial_follow_must_exhaust_suit(self):
        lead = Lead(cards("H:K", "H:Q", "H:9"), PlayKind.THROW)
        hand = cards("H:4", "S:A", "D:A", "J:big")
        self.violation("MUST_FOLLOW", validate_follow, hand, cards("S:A", "D:A", "J:big"), lead, self.ctx)
        validate_follow(hand, cards("H:4", "S:A", "J:big"), lead, self.ctx)

    def test_gang_construction_and_inherited_cards(self):
        self.assertTrue(is_true_gang(gang("8"), self.ctx))
        fake = cards("S:Q", "H:7", "S:7", "D:7")
        self.assertTrue(is_false_gang(fake, self.ctx))
        ctx = RuleContext(Suit.CLUBS, exchanged_cards=frozenset(cards("S:Q", "H:8")))
        self.assertFalse(is_true_gang(gang("8"), ctx))
        self.assertFalse(is_false_gang(fake, ctx))
        reflected = RuleContext(Suit.CLUBS, (Reflection(0, cards("H:3", "S:3", "D:3")),))
        self.assertFalse(is_true_gang(gang("3"), reflected))
        self.assertFalse(is_false_gang(cards("S:Q", "H:3", "S:3", "D:3"), reflected))

    def test_four_queens_require_explicit_lead_kind(self):
        self.violation("AMBIGUOUS_Q_GANG", prepare_lead, gang("Q"), gang("Q"), [(), (), ()], self.ctx)
        for kind in (PlayKind.TRUE_GANG, PlayKind.FALSE_GANG):
            self.assertEqual(prepare_lead(gang("Q"), gang("Q"), [(), (), ()], self.ctx, kind).kind, kind)

    def test_wrong_declared_kind_is_rejected(self):
        self.violation("INVALID_GANG", prepare_lead, cards("H:A", "S:K"), cards("H:A", "S:K"), [(), (), ()], self.ctx, PlayKind.TRUE_GANG)
        self.violation("INVALID_KIND", prepare_lead, cards("H:A"), cards("H:A"), [(), (), ()], self.ctx, "single")

    def test_true_gang_follow_priority_and_larger_gang_exception(self):
        lead = Lead(gang("8"), PlayKind.TRUE_GANG)
        hand = cards("J:big", "H:4", "S:4", "D:4", "H:6")
        self.violation("MUST_FOLLOW", validate_follow, hand, cards("H:4", "S:4", "D:4", "H:6"), lead, self.ctx)
        validate_follow(hand, hand[:4], lead, self.ctx)
        validate_follow(gang("9") + cards("J:big"), gang("9"), lead, self.ctx)

    def test_false_gang_follow_priority_and_true_gang_exception(self):
        lead = Lead(cards("S:Q", "H:7", "S:7", "D:7"), PlayKind.FALSE_GANG)
        hand = cards("H:4", "J:big", "J:small", "C:8", "C:9")
        self.violation("MUST_FOLLOW", validate_follow, hand, hand[1:], lead, self.ctx)
        validate_follow(hand, hand[:4], lead, self.ctx)
        validate_follow(gang("2") + cards("H:A"), gang("2"), lead, self.ctx)

    def test_bottom_allows_zero_point_trumps_and_exchanged_cards(self):
        chosen = cards("J:big", "J:small", "S:Q", "H:2", "S:3", "D:4")
        hand = chosen + tuple(c for c in deck() if c not in chosen)[:12]
        self.assertEqual(validate_bottom(hand, chosen), chosen)

    def test_bottom_rejects_every_five_ten_and_king(self):
        fillers = cards("H:3", "H:4", "H:6", "H:7", "H:8")
        for bad in (c for c in deck() if c.points):
            chosen = fillers + (bad,)
            hand = chosen + tuple(c for c in deck() if c not in chosen)[:12]
            with self.subTest(card=bad.id):
                self.violation("POINTS_IN_BOTTOM", validate_bottom, hand, chosen)
        self.violation("WRONG_HAND_COUNT", validate_bottom, fillers, fillers)


class TrickTest(RuleFixture, unittest.TestCase):
    """Turn ordering and integration of independently tested rules."""

    def test_out_of_turn_action_and_failure_do_not_mutate_state(self):
        hands = (cards("H:K", "S:A"), cards("H:4", "J:big"), cards("S:J", "D:A"), cards("J:small", "S:3"))
        initial = Trick(self.ctx, hands, 0)
        self.violation("NOT_YOUR_TURN", initial.play, 1, cards("H:4"))
        opened = initial.play(0, cards("H:K"))
        self.violation("MUST_FOLLOW", opened.play, 1, cards("J:big"))
        self.assertEqual(initial.hands, hands)
        self.assertEqual(opened.hands[1], hands[1])
        self.assertEqual(opened.next_seat, 1)
        with self.assertRaises(FrozenInstanceError):
            initial.leader = 2

    def test_single_trump_and_overtrump_collect_points(self):
        hands = tuple(cards(c) for c in ("H:K", "H:4", "S:J", "J:big"))
        trick = Trick(self.ctx, hands, 0)
        for seat, hand in enumerate(hands):
            trick = trick.play(seat, hand)
        result = trick.result()
        self.assertEqual((result.winner, result.points, result.next_leader), (3, 10, 3))
        self.assertEqual(trick.hands, ((), (), (), ()))
        self.violation("TRICK_FINISHED", trick.play, 0, cards("H:K"))

    def test_off_suit_ace_cannot_win(self):
        hands = tuple(cards(c) for c in ("H:4", "S:A", "H:K", "D:A"))
        trick = Trick(self.ctx, hands, 0)
        for seat, hand in enumerate(hands):
            trick = trick.play(seat, hand)
        self.assertEqual(trick.result().winner, 2)

    def test_equal_trump_first_in_action_order_wins(self):
        hands = tuple(cards(c) for c in ("H:J", "S:J", "D:J", "C:3"))
        trick = Trick(self.ctx, hands, 2)
        for seat in (2, 3, 0, 1):
            trick = trick.play(seat, hands[seat])
        self.assertEqual(trick.result().winner, 2)

    def test_unequal_hand_counts_and_duplicate_card_ownership_rejected(self):
        self.violation("WRONG_HAND_COUNT", Trick, self.ctx, (cards("H:A"), (), (), ()), 0)
        self.violation("DUPLICATE_CARD", Trick, self.ctx, (cards("H:A"), cards("H:A"), cards("S:A"), cards("D:A")), 0)
        self.violation("INVALID_SEAT", Trick, self.ctx, tuple(cards(c) for c in ("H:A", "S:A", "D:A", "C:A")), True)

    def test_cannot_get_result_before_four_actions(self):
        trick = Trick(self.ctx, tuple(cards(c) for c in ("H:A", "S:A", "D:A", "C:A")), 0)
        self.violation("INCOMPLETE_TRICK", trick.result)

    def test_failed_throw_keeps_unplayed_cards_in_hand(self):
        hands = (
            cards("S:K", "S:10", "S:8"), cards("S:9", "H:3", "H:4"),
            cards("H:A", "D:3", "D:4"), cards("D:A", "D:6", "D:7"),
        )
        trick = Trick(self.ctx, hands, 0).play(0, hands[0])
        self.assertEqual(trick.hands[0], cards("S:K", "S:10"))
        self.assertEqual(trick.plays[0].cards, cards("S:8"))
        self.assertTrue(trick.lead.throw_failed)
        trick = trick.play(1, cards("S:9")).play(2, cards("H:A")).play(3, cards("D:A"))
        self.assertEqual(trick.result().winner, 1)

    def test_throw_requires_all_trump_for_kill(self):
        hands = (
            cards("H:K", "H:Q"), cards("J:big", "S:A"),
            cards("C:A", "C:K"), cards("H:4", "D:A"),
        )
        trick = Trick(self.ctx, hands, 0)
        for seat, hand in enumerate(hands):
            trick = trick.play(seat, hand)
        self.assertEqual(trick.result().winner, 2)
        self.assertEqual(trick.result().points, 20)

    def test_multi_card_overtrump_uses_largest_single_not_sum(self):
        hands = (cards("H:K", "H:Q"), cards("C:A", "C:K"), cards("J:small", "C:3"), cards("J:big", "C:4"))
        trick = Trick(self.ctx, hands, 0)
        for seat, hand in enumerate(hands):
            trick = trick.play(seat, hand)
        self.assertEqual(trick.result().winner, 3)

    def test_true_gang_beats_padding_with_biggest_trump(self):
        hands = (gang("8"), cards("D:5", "J:big", "J:small", "S:Q"), gang("9"), cards("C:A", "C:K", "C:10", "C:3"))
        trick = Trick(self.ctx, hands, 0)
        for seat, hand in enumerate(hands):
            trick = trick.play(seat, hand)
        self.assertEqual(trick.result().winner, 2)
        self.assertEqual(trick.result().points, 25)

    def test_smallest_true_gang_beats_four_queens_declared_false(self):
        hands = (gang("Q"), gang("2"), cards("H:4", "S:4", "D:4", "H:6"), cards("H:8", "S:8", "D:8", "S:6"))
        trick = Trick(self.ctx, hands, 0).play(0, hands[0], PlayKind.FALSE_GANG)
        for seat in (1, 2, 3):
            trick = trick.play(seat, hands[seat])
        self.assertEqual(trick.result().winner, 1)

    def test_false_gang_wins_against_only_padding(self):
        hands = (
            cards("S:Q", "H:7", "S:7", "D:7"), cards("H:4", "S:4", "D:4", "H:6"),
            cards("J:big", "J:small", "C:3", "C:4"), cards("H:A", "S:A", "D:A", "H:10"),
        )
        trick = Trick(self.ctx, hands, 0)
        for seat, hand in enumerate(hands):
            trick = trick.play(seat, hand)
        self.assertEqual(trick.result().winner, 0)
        self.assertEqual(trick.result().points, 10)

    def test_multiple_complete_deals_conserve_all_cards_and_points(self):
        # Fixed seeds, all four trump suits; each deal includes six legal bottom cards.
        for suit in (Suit.SPADES, Suit.HEARTS, Suit.CLUBS, Suit.DIAMONDS):
            for seed in range(6):
                rng = random.Random(seed)
                ctx = RuleContext(suit)
                shuffled = list(deck())
                rng.shuffle(shuffled)
                bottom = tuple(c for c in shuffled if not c.points)[:6]
                live = tuple(c for c in shuffled if c not in bottom)
                hands = tuple(live[s*12:(s+1)*12] for s in range(4))
                leader = seed % 4
                captured = []
                total = 0
                for _ in range(12):
                    trick = Trick(ctx, hands, leader)
                    for _ in range(4):
                        seat = trick.next_seat
                        hand = trick.hands[seat]
                        if trick.lead is None:
                            chosen = rng.choice(hand)
                        else:
                            required = group(trick.lead.cards[0], ctx)
                            legal = tuple(c for c in hand if group(c, ctx) == required)
                            chosen = rng.choice(legal or hand)
                        trick = trick.play(seat, (chosen,))
                    result = trick.result()
                    captured.extend(result.cards)
                    total += result.points
                    hands, leader = trick.hands, result.next_leader
                self.assertEqual(hands, ((), (), (), ()))
                self.assertEqual(total, 100)
                self.assertEqual(len(set(captured)), 48)
                self.assertEqual(set(captured) | set(bottom), set(deck()))


class SettlementTest(unittest.TestCase):
    def test_all_scores_for_all_dealers(self):
        for dealer in range(4):
            for score in range(0, 101, 5):
                with self.subTest(dealer=dealer, score=score):
                    result = settle(dealer, score)
                    self.assertEqual(result.next_dealer, dealer if score < 40 else (dealer+1) % 4)
                    self.assertEqual(result.winning_team, dealer % 2 if score < 40 else 1-dealer % 2)
                    expected = 2 if score == 0 or score >= 80 else 1 if score >= 60 else 0
                    self.assertEqual(len(result.tribute_obligations), expected)
                    for tribute in result.tribute_obligations:
                        self.assertEqual(tribute.receiver, (tribute.giver+1) % 4)
                        self.assertNotEqual(tribute.giver % 2, result.winning_team)
                        self.assertEqual(tribute.receiver % 2, result.winning_team)

    def test_exact_thresholds_regress_old_unreachable_branches(self):
        expected = {
            0: (0, [(1, 2), (3, 0)]), 35: (0, []), 40: (1, []),
            55: (1, []), 60: (1, [(0, 1)]), 75: (1, [(0, 1)]),
            80: (1, [(0, 1), (2, 3)]), 100: (1, [(0, 1), (2, 3)]),
        }
        for score, (dealer, tribute) in expected.items():
            result = settle(0, score)
            self.assertEqual(result.next_dealer, dealer)
            self.assertEqual([(x.giver, x.receiver) for x in result.tribute_obligations], tribute)

    def test_rejects_impossible_scores_and_seats(self):
        for score in (-5, 105, 41, True, "40", 40.0):
            with self.assertRaises(RuleViolation):
                settle(0, score)
        for seat in (-1, 4, True, "0"):
            with self.assertRaises(RuleViolation):
                settle(seat, 40)

    def test_result_is_json_serializable(self):
        result = json.loads(json.dumps(settle(3, 80).to_dict()))
        self.assertEqual(result["next_dealer"], 0)
        self.assertEqual(result["tribute_obligations"], [{"giver": 1, "receiver": 2}, {"giver": 3, "receiver": 0}])
