import json
import random
import unittest

from sanwufan import (
    Card, FirstNoTrump, Game, GameOptions, LateReflection, Phase, PlayKind,
    RuleViolation, Suit, Tribute, deck, group, is_trump, settle, strength,
)


def cards(*ids):
    return tuple(Card.from_id(x) for x in ids)


def fixed_order(wanted=((), (), (), ()), bottom=()):
    """Complete partial hands to 12 cards, then interleave into deal order."""
    reserved = tuple(c for h in wanted for c in h) + tuple(bottom)
    assert len(set(reserved)) == len(reserved)
    available = [c for c in deck() if c not in reserved]
    hands = []
    for desired in wanted:
        missing = 12 - len(desired)
        hands.append(tuple(desired) + tuple(available[:missing]))
        del available[:missing]
    remaining = tuple(bottom) + tuple(available)
    assert len(remaining) == 6
    return tuple(hands[seat][i] for i in range(12) for seat in range(4)) + remaining


def prepared(game=None, order=None, call=True):
    game = game or Game()
    for seat in range(4):
        game = game.ready(seat)
    game = game.start_deal(order or fixed_order())
    for _ in range(48):
        game = game.deal_one()
        if call and game.context is None:
            for seat, hand in enumerate(game.hands):
                twos = [c for c in hand if c.rank == "2"]
                if twos:
                    game = game.call_trump(seat, twos[0])
                    break
    return game.finish_dealing()


def declarations_done(game):
    for seat in range(4):
        game = game.confirm_declarations(seat)
    return game


def exchanges_done(game):
    if game.phase == Phase.TRIBUTE_GIVE:
        for t in game.obligations:
            trumps = [c for c in game.hands[t.giver] if is_trump(c, game.context)]
            game = game.give_tribute(t.giver, max(trumps, key=lambda c: strength(c, game.context)))
    if game.phase == Phase.TRIBUTE_RETURN:
        for exchange in game.exchanges:
            seat = exchange.obligation.receiver
            choices = [c for c in game.hands[seat] if is_trump(c, game.context)
                       and not c.points and c not in game.context.reflected_cards]
            game = game.return_tribute(seat, min(choices, key=lambda c: strength(c, game.context)))
    return game


def play_entire_deal(game):
    game = exchanges_done(declarations_done(game))
    game = game.take_bottom(game.dealer)
    bottom = tuple(c for c in game.hands[game.dealer] if not c.points)[:6]
    game = game.discard(game.dealer, bottom)
    while game.phase == Phase.PLAYING:
        seat = game.trick.next_seat
        hand = game.hands[seat]
        if game.trick.lead:
            required = group(game.trick.lead.cards[0], game.context)
            matching = [c for c in hand if group(c, game.context) == required]
            chosen = (matching or list(hand))[0]
        else:
            chosen = hand[0]
        game = game.play(seat, (chosen,))
    return game


class GameTest(unittest.TestCase):
    def violation(self, code, fn, *args, **kwargs):
        with self.assertRaises(RuleViolation) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_requires_four_ready_players(self):
        game = Game()
        self.violation("NOT_ALL_READY", game.start_deal)
        for seat in range(4):
            game = game.ready(seat)
        game = game.ready(2, False)
        self.violation("NOT_ALL_READY", game.start_deal)
        self.violation("INVALID_READY", game.ready, 2, 1)
        self.assertEqual(game.phase, Phase.WAITING)

    def test_rejects_invalid_pack_without_starting(self):
        game = Game()
        for seat in range(4):
            game = game.ready(seat)
        self.violation("INVALID_DECK", game.start_deal, deck()[:-1])
        self.violation("DUPLICATE_CARD", game.start_deal, deck()[:-1] + (deck()[0],))
        self.assertEqual(game.number, 0)

    def test_deal_one_card_and_first_valid_two_sets_dealer(self):
        wanted = (cards("H:2"), cards("S:2"), (), ())
        game = Game()
        for seat in range(4):
            game = game.ready(seat)
        game = game.start_deal(fixed_order(wanted))
        self.violation("NOT_OWNED", game.call_trump, 0, cards("H:2")[0])
        game = game.deal_one().call_trump(0, cards("H:2")[0])
        self.assertEqual(game.dealer, 0)
        self.assertEqual(game.context.trump_suit, Suit.HEARTS)
        game = game.deal_one()
        self.violation("ALREADY_CALLED", game.call_trump, 1, cards("S:2")[0])
        self.assertEqual(game.hands[0], cards("H:2"))
        self.assertEqual(game.hands[1], cards("S:2"))

    def test_last_dealt_card_can_still_call_two(self):
        order = list(deck())
        last = cards("D:2")[0]
        order.remove(last)
        order.insert(47, last)
        game = Game()
        for seat in range(4):
            game = game.ready(seat)
        game = game.start_deal(order)
        for _ in range(48):
            game = game.deal_one()
        self.assertEqual(game.phase, Phase.DEALING)
        game = game.call_trump(3, last).finish_dealing()
        self.assertEqual(game.phase, Phase.DECLARING)
        self.assertEqual(game.dealer, 3)
        self.assertEqual([len(h) for h in game.hands], [12]*4)

    def test_cannot_finish_early_or_deal_bottom_as_hand_cards(self):
        game = Game()
        for seat in range(4):
            game = game.ready(seat)
        game = game.start_deal(fixed_order())
        self.violation("DEAL_NOT_FINISHED", game.finish_dealing)
        for _ in range(48):
            game = game.deal_one()
        self.violation("DEAL_FINISHED", game.deal_one)

    def test_existing_dealer_does_not_change_when_opponent_calls(self):
        game = prepared(Game(dealer=2), fixed_order((cards("H:2"), (), (), ())))
        self.assertEqual(game.caller, 0)
        self.assertEqual(game.dealer, 2)

    def test_first_no_two_redeals_same_deal_number(self):
        game = prepared(call=False)
        self.assertEqual(game.phase, Phase.FIRST_NO_TRUMP)
        revised = game.redeal(fixed_order())
        self.assertEqual(revised.number, 1)
        self.assertEqual(revised.dealt_count, 0)
        self.assertEqual(revised.hands, ((), (), (), ()))
        self.assertIsNone(revised.dealer)
        self.violation("WRONG_FALLBACK", game.choose_first_dealer, 0)

    def test_first_no_two_alternative_random_dealer_policy(self):
        game = prepared(Game(options=GameOptions(first_no_trump=FirstNoTrump.RANDOM_DEALER)), call=False)
        self.violation("WRONG_FALLBACK", game.redeal)
        game = game.choose_first_dealer(2)
        self.assertEqual(game.dealer, 2)
        self.assertEqual(game.phase, Phase.DRAW_TRUMP)

    def test_blackout_joker_is_retried_without_removing_bottom(self):
        bottom = cards("J:big", "J:small", "H:A", "C:A", "D:A", "S:A")
        game = prepared(Game(dealer=0, obligations=(Tribute(1, 2),)), fixed_order(bottom=bottom), call=False)
        self.violation("NOT_DEFENDER", game.draw_trump, 0, 2)
        game = game.draw_trump(1, 0)
        self.assertEqual(game.phase, Phase.DRAW_TRUMP)
        self.assertEqual(game.bottom, bottom)
        self.assertIsNone(game.context)
        game = game.draw_trump(3, 2)
        self.assertEqual(game.context.trump_suit, Suit.HEARTS)
        game = declarations_done(game)
        self.assertEqual(game.exemption, "blackout")
        self.assertEqual(game.phase, Phase.TAKE_BOTTOM)

    def test_blackout_does_not_exempt_dealer_team_tribute(self):
        wanted = (cards("D:5", "J:big"), cards("H:J", "H:2"), (), ())
        game = prepared(Game(dealer=1, obligations=(Tribute(1, 2),)), fixed_order(wanted), call=False)
        game = game.draw_trump(0, next(i for i,c in enumerate(game.bottom) if c.suit != Suit.JOKER))
        game = declarations_done(game)
        self.assertNotEqual(game.exemption, "blackout")

    def test_reveal_before_calling_two_is_preserved_when_trump_is_set(self):
        wanted = (cards("H:3", "S:3", "D:3", "H:2"), (), (), ())
        game = Game()
        for seat in range(4):
            game = game.ready(seat)
        game = game.start_deal(fixed_order(wanted))
        for _ in range(12):
            game = game.deal_one()
        game = game.reveal(0, wanted[0][:3])
        for _ in range(4):
            game = game.deal_one()
        game = game.call_trump(0, wanted[0][3])
        self.assertEqual(game.context.reflected_cards, frozenset(wanted[0][:3]))

    def test_reflection_exempts_both_givers(self):
        wanted = ((), cards("H:3", "S:3", "D:3"), (), ())
        game = prepared(Game(dealer=0, obligations=settle(0, 0).tribute_obligations), fixed_order(wanted))
        game = game.reveal(1, wanted[1])
        game = declarations_done(game)
        self.assertEqual(game.phase, Phase.TAKE_BOTTOM)
        self.assertEqual(game.exemption, "reflection")
        self.assertEqual(game.exchanges, ())

    def test_confirmation_allows_later_reflection_but_cannot_repeat_confirmation(self):
        wanted = (cards("H:3", "S:3", "D:3"), (), (), ())
        game = prepared(order=fixed_order(wanted)).confirm_declarations(0)
        game = game.reveal(0, wanted[0])
        self.assertEqual(set(game.context.reflected_cards), set(wanted[0]))
        self.violation("ALREADY_CONFIRMED", game.confirm_declarations, 0)

    def tribute_game(self, double=False, wanted=None):
        if wanted is None:
            wanted = (
                cards("D:5", "J:big", "H:2"), cards("J:small", "S:J", "D:2"),
                cards("S:Q", "C:J", "S:2"), cards("D:J", "H:J", "C:2"),
            )
        obligations = settle(0, 80 if double else 60).tribute_obligations
        return declarations_done(prepared(Game(dealer=1, obligations=obligations), fixed_order(wanted)))

    def test_single_tribute_diamond_five_and_legal_return(self):
        game = self.tribute_game()
        self.assertEqual(game.phase, Phase.TRIBUTE_GIVE)
        initial = game.hands
        self.violation("MUST_GIVE_LARGEST", game.give_tribute, 0, cards("J:big")[0])
        game = game.give_tribute(0, cards("D:5")[0])
        self.violation("RETURNED_GIFT", game.return_tribute, 1, cards("D:5")[0])
        game = game.return_tribute(1, cards("D:2")[0])
        self.assertEqual(game.phase, Phase.TAKE_BOTTOM)
        self.assertIn(cards("D:5")[0], game.hands[1])
        self.assertIn(cards("D:2")[0], game.hands[0])
        self.assertNotIn(cards("D:5")[0], game.hands[0])
        self.assertEqual(game.context.exchanged_cards, frozenset(cards("D:5", "D:2")))
        self.assertEqual([len(h) for h in game.hands], [12]*4)
        self.assertNotEqual(game.hands, initial)

    def test_double_exchange_is_atomic_and_rejected_action_changes_nothing(self):
        game = self.tribute_game(double=True)
        initial = game.hands
        game = game.give_tribute(2, cards("S:Q")[0])
        self.assertEqual(game.phase, Phase.TRIBUTE_GIVE)
        self.violation("WRONG_PHASE", game.return_tribute, 3, cards("C:2")[0])
        game = game.give_tribute(0, cards("D:5")[0])
        game = game.return_tribute(1, cards("D:2")[0])
        self.assertEqual(game.hands, initial)
        self.violation("INVALID_RETURN", game.return_tribute, 3, next(c for c in game.hands[3] if not is_trump(c, game.context)))
        self.assertEqual(game.hands, initial)
        self.assertEqual(game.view_for(1)["hand_counts"], [12, 12, 11, 13])
        game = game.return_tribute(3, cards("C:2")[0])
        self.assertEqual(game.phase, Phase.TAKE_BOTTOM)
        self.assertEqual([len(h) for h in game.hands], [12]*4)
        self.assertEqual(len(game.context.exchanged_cards), 4)

    def test_revealed_three_cannot_be_returned(self):
        wanted = (
            cards("D:5", "J:big", "H:2"), cards("H:3", "S:3", "D:3", "D:2"),
            cards("S:Q", "C:J", "S:2"), cards("D:J", "H:J", "C:2"),
        )
        game = prepared(Game(dealer=1, obligations=(Tribute(0, 1),)), fixed_order(wanted))
        game = game.reveal(1, wanted[1][:3])
        game = declarations_done(game).give_tribute(0, cards("D:5")[0])
        self.violation("INVALID_RETURN", game.return_tribute, 1, cards("H:3")[0])
        game = game.return_tribute(1, cards("D:2")[0])
        self.assertEqual(len(game.context.reflected_cards), 3)

    def test_receiver_with_only_revealed_threes_cancels_all_tribute(self):
        # Club trumps stay away from receiver1 except for its own declared 3s.
        wanted = (
            cards("D:5", "J:big", "C:2"),
            cards("H:3", "S:3", "D:3", "H:4", "H:6", "H:7", "H:8", "H:9", "H:10", "H:Q", "H:K", "H:A"),
            cards("J:small", "S:Q", "C:J", "H:J", "S:J", "D:J", "S:2", "H:2", "D:2"), (),
        )
        game = prepared(Game(dealer=1, obligations=(Tribute(0, 1),)), fixed_order(wanted))
        self.assertEqual(game.context.trump_suit, Suit.CLUBS)
        game = game.reveal(1, wanted[1][:3])
        game = declarations_done(game)
        self.assertEqual(game.exemption, "no_eligible_return")
        self.assertEqual(game.phase, Phase.TAKE_BOTTOM)

    def test_no_trump_or_only_point_trumps_exempts_all_givers(self):
        pure_side = cards("H:3", "H:4", "H:5", "H:6", "H:7", "H:8", "H:9", "H:10", "H:Q", "H:K", "H:A", "S:4")
        for extra in ((), cards("D:5")):
            hand = extra + pure_side[:12-len(extra)]
            wanted = (hand, cards("C:2"), (), ())
            game = prepared(Game(dealer=1, obligations=(Tribute(0, 1),)), fixed_order(wanted))
            game = declarations_done(game)
            self.assertEqual(game.exemption, "no_eligible_giver")
            self.assertEqual(game.phase, Phase.TAKE_BOTTOM)

    def test_tribute_cards_cannot_form_late_reflection(self):
        wanted = (
            cards("D:5", "H:3", "S:3", "C:2"), cards("C:3", "H:5", "S:5"), (), (),
        )
        game = prepared(Game(dealer=0, obligations=(Tribute(0, 1),)), fixed_order(wanted))
        game = declarations_done(game).give_tribute(0, cards("D:5")[0]).return_tribute(1, cards("C:3")[0])
        game = game.take_bottom(0)
        # Return card is a member of an otherwise possible triple held by giver0.
        # Ownership and source are enforced even before a potential rollback.
        self.violation("EXCHANGED_REFLECTION", game.reveal, 0, cards("H:3", "S:3", "C:3"))
        self.assertIn(cards("C:3")[0], game.context.exchanged_cards)

    def test_dealer_only_take_and_discard_with_reveal_deadline(self):
        wanted = (cards("H:3", "S:3", "D:3", "C:2"), (), (), ())
        game = declarations_done(prepared(order=fixed_order(wanted)))
        self.violation("NOT_DEALER", game.take_bottom, 1)
        game = game.take_bottom(0)
        self.assertEqual(len(game.hands[0]), 18)
        game = game.reveal(0, wanted[0][:3])
        selected = wanted[0][:3] + tuple(c for c in game.hands[0] if not c.points and c not in wanted[0][:3])[:3]
        game = game.discard(0, selected)
        self.assertEqual(game.phase, Phase.PLAYING)
        self.assertEqual([len(h) for h in game.hands], [12]*4)
        self.assertTrue(set(wanted[0][:3]) <= set(game.bottom))
        self.violation("WRONG_PHASE", game.reveal, 0, wanted[0][:3])

    def test_failed_discard_does_not_remove_cards(self):
        game = declarations_done(prepared())
        game = game.take_bottom(game.dealer)
        selected = tuple(c for c in game.hands[game.dealer] if not c.points)[:5] + (next(c for c in game.hands[game.dealer] if c.points),)
        original = game.hands
        self.violation("POINTS_IN_BOTTOM", game.discard, game.dealer, selected)
        self.assertEqual(game.hands, original)
        self.assertEqual(game.phase, Phase.DISCARD)

    def test_public_views_do_not_leak_stock_other_hands_or_unseen_bottom(self):
        game = prepared()
        views = [game.view_for(s) for s in range(4)]
        for seat, view in enumerate(views):
            self.assertEqual(view["hand"], [c.id for c in game.hands[seat]])
            self.assertEqual(view["bottom_cards"], [])
            json.dumps(view)
            self.assertNotIn("stock", view)
            self.assertNotIn("hands", view)
        game = declarations_done(game).take_bottom(game.dealer)
        self.assertEqual(len(game.view_for(game.dealer)["hand"]), 18)
        for seat in range(4):
            self.assertEqual(len(game.view_for(seat)["hand"]), 18 if seat == game.dealer else 12)

    def test_pending_exchange_views_hide_cards_from_uninvolved_players(self):
        game = self.tribute_game().give_tribute(0, cards("D:5")[0])
        self.assertEqual(len(game.view_for(0)["hand"]), 11)
        self.assertEqual(len(game.view_for(1)["hand"]), 13)
        self.assertEqual(game.view_for(2)["exchanges"][0]["gift"], None)
        self.assertEqual(game.view_for(1)["exchanges"][0]["gift"], "D:5")

    def test_full_deal_and_next_deal_reset_and_carry_obligations(self):
        game = play_entire_deal(prepared())
        self.assertEqual(game.phase, Phase.SETTLED)
        self.assertEqual(sum(game.team_points), 100)
        self.assertEqual(len(game.tricks), 12)
        self.assertEqual(game.hands, ((), (), (), ()))
        self.assertEqual(game.result.settlement, settle(game.dealer, game.team_points[1-game.dealer % 2]))
        old_result = game.result
        next_game = game.next_deal()
        self.assertEqual(next_game.phase, Phase.WAITING)
        self.assertEqual(next_game.number, 1)
        self.assertEqual(next_game.dealer, old_result.settlement.next_dealer)
        self.assertEqual(next_game.obligations, old_result.settlement.tribute_obligations)
        self.assertEqual(next_game.history, (old_result,))
        self.assertIsNone(next_game.context)
        self.assertEqual(next_game.team_points, (0, 0))
        self.assertEqual(next_game.ready_seats, frozenset())
        self.violation("NOT_ALL_READY", next_game.start_deal)
        self.violation("WRONG_PHASE", game.ready, 0)

    def test_many_consecutive_full_deals_including_actual_tribute(self):
        game = Game()
        exchanged = exempted = 0
        for number in range(1, 21):
            order = list(deck())
            random.Random(number).shuffle(order)
            game = prepared(game, order)
            game = play_entire_deal(game)
            game.check_invariants()
            self.assertEqual(game.number, number)
            self.assertEqual(sum(game.team_points), 100)
            self.assertEqual(len(game.tricks), 12)
            self.assertEqual(len({c for t in game.tricks for c in t.cards}), 48)
            self.assertEqual(len(game.bottom), 6)
            self.assertFalse(any(c.points for c in game.bottom))
            self.assertEqual(game.context.exchanged_cards, frozenset(c for e in game.exchanges for c in (e.gift,e.returned)))
            exchanged += bool(game.exchanges)
            exempted += game.exemption not in (None, "no_obligation")
            game = game.next_deal()
        self.assertEqual(len(game.history), 20)
        self.assertGreater(exchanged, 0)

    def test_cannot_skip_preparation_or_start_play_early(self):
        game = prepared()
        self.violation("WRONG_PHASE", game.take_bottom, game.dealer)
        self.violation("WRONG_PHASE", game.play, 0, game.hands[0][:1])
        self.violation("WRONG_PHASE", game.next_deal)
        self.assertEqual(game.phase, Phase.DECLARING)

    def test_late_reflection_keep_or_rollback_exchange_policies(self):
        wanted = (
            cards("D:5", "J:big", "H:3", "S:3", "D:3", "C:2"),
            cards("J:small", "S:J", "D:2"), (), (),
        )
        for policy in (LateReflection.KEEP_EXCHANGES, LateReflection.ROLLBACK_EXCHANGES):
            for take_bottom in (False, True):
                with self.subTest(policy=policy, taken=take_bottom):
                    game = prepared(Game(dealer=1, obligations=(Tribute(0,1),), options=GameOptions(late_reflection=policy)), fixed_order(wanted))
                    initial_hands = game.hands
                    game = declarations_done(game).give_tribute(0, cards("D:5")[0]).return_tribute(1, cards("D:2")[0])
                    bottom = game.bottom
                    if take_bottom:
                        game = game.take_bottom(1)
                    game = game.reveal(0, cards("H:3", "S:3", "D:3"))
                    self.assertEqual(len(game.context.reflected_cards), 3)
                    if policy == LateReflection.KEEP_EXCHANGES:
                        self.assertEqual(len(game.exchanges), 1)
                        self.assertIn(cards("D:5")[0], game.hands[1])
                        self.assertIn(cards("D:2")[0], game.hands[0])
                    else:
                        expected = list(initial_hands)
                        if take_bottom:
                            expected[1] += bottom
                        self.assertEqual(game.hands, tuple(expected))
                        self.assertEqual(game.context.exchanged_cards, frozenset())
                        self.assertEqual(game.exchanges, ())
                        self.assertEqual(game.exemption, "late_reflection")

    def test_multi_card_gangs_complete_deal_in_nine_tricks(self):
        gang8 = cards("S:8", "H:8", "C:8", "D:8")
        gang9 = cards("S:9", "H:9", "C:9", "D:9")
        four_q = cards("S:Q", "H:Q", "C:Q", "D:Q")
        four_2 = cards("S:2", "H:2", "C:2", "D:2")
        cases = [
            ((gang8 + cards("C:2"), cards("D:5", "J:big", "J:small", "S:Q"), gang9, cards("C:3", "C:4", "C:A", "C:K")),
             cards("H:3", "S:3", "D:3", "H:4", "S:4", "D:4"), PlayKind.TRUE_GANG, 2),
            ((four_q, four_2, cards("H:4", "C:4", "D:4", "H:6"), cards("H:8", "C:8", "D:8", "H:10")),
             cards("H:3", "S:3", "D:3", "H:7", "S:7", "D:7"), PlayKind.FALSE_GANG, 1),
        ]
        for wanted, bottom, kind, winner in cases:
            with self.subTest(kind=kind):
                game = declarations_done(prepared(Game(dealer=0), fixed_order(wanted, bottom)))
                game = game.take_bottom(0).discard(0, bottom)
                game = game.play(0, wanted[0][:4], kind)
                view = game.view_for(3)
                self.assertEqual(view["current_lead"]["kind"], kind.value)
                self.assertEqual(view["current_lead"]["cards"], [c.id for c in wanted[0][:4]])
                self.assertEqual(view["next_seat"], 1)
                json.dumps(view)
                for seat in (1,2,3):
                    game = game.play(seat, wanted[seat][:4])
                self.assertEqual(game.tricks[-1].winner, winner)
                view = game.view_for(0)
                self.assertIsNone(view["current_lead"])
                self.assertEqual(view["last_trick"]["kind"], kind.value)
                self.assertEqual(view["last_trick"]["leader"], 0)
                self.assertEqual(view["last_trick"]["winner"], winner)
                self.assertEqual(view["last_trick"]["plays"], [
                    {"seat": s, "cards": [c.id for c in wanted[s][:4]]} for s in range(4)
                ])
                json.dumps(view)
                self.assertEqual([len(h) for h in game.hands], [8]*4)
                while game.phase == Phase.PLAYING:
                    seat = game.trick.next_seat
                    hand = game.hands[seat]
                    if game.trick.lead:
                        required = group(game.trick.lead.cards[0], game.context)
                        matches = [c for c in hand if group(c, game.context) == required]
                        chosen = (matches or list(hand))[0]
                    else:
                        chosen = hand[0]
                    game = game.play(seat, (chosen,))
                self.assertEqual(game.phase, Phase.SETTLED)
                self.assertEqual(game.result.trick_count, 9)
                self.assertEqual(sum(game.team_points), 100)


if __name__ == "__main__":
    unittest.main()
