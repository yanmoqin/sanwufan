"""Seeded, automatic actors for demonstrating the full lifecycle, not game AI."""
import random

from .game import Game, Phase
from .models import deck
from .rules import group, is_trump, strength


def prepare_demo(game: Game, rng: random.Random) -> Game:
    for seat in range(4):
        game = game.ready(seat)
    order = list(deck())
    rng.shuffle(order)
    game = game.start_deal(order)
    for attempt in range(100):
        for _ in range(48):
            game = game.deal_one()
            if game.context is None:
                for seat, hand in enumerate(game.hands):
                    twos = [c for c in hand if c.rank == "2"]
                    if twos:
                        game = game.call_trump(seat, twos[0])
                        break
        game = game.finish_dealing()
        if game.phase != Phase.FIRST_NO_TRUMP:
            break
        order = list(deck())
        rng.shuffle(order)
        game = game.redeal(order)
    else:
        raise RuntimeError("演示连续重发100次仍无人亮2")
    while game.phase == Phase.DRAW_TRUMP:
        game = game.draw_trump((game.dealer + 1) % 4, rng.randrange(6))
    # Reveal any available reflection before everyone confirms the tribute window.
    for seat, hand in enumerate(game.hands):
        for rank in ("5", "3"):
            selected = tuple(c for c in hand if c.rank == rank)
            if len(selected) >= 3:
                game = game.reveal(seat, selected[:3])
    for seat in range(4):
        game = game.confirm_declarations(seat)
    if game.phase == Phase.TRIBUTE_GIVE:
        for obligation in game.obligations:
            seat = obligation.giver
            trumps = [c for c in game.hands[seat] if is_trump(c, game.context)]
            game = game.give_tribute(seat, max(trumps, key=lambda c: strength(c, game.context)))
    if game.phase == Phase.TRIBUTE_RETURN:
        for exchange in game.exchanges:
            seat = exchange.obligation.receiver
            candidates = [c for c in game.hands[seat] if is_trump(c, game.context)
                          and not c.points and c not in game.context.reflected_cards]
            game = game.return_tribute(seat, min(candidates, key=lambda c: strength(c, game.context)))
    game = game.take_bottom(game.dealer)
    selected = tuple(c for c in game.hands[game.dealer] if not c.points)[:6]
    return game.discard(game.dealer, selected)


def finish_demo(game: Game, rng: random.Random) -> Game:
    while game.phase == Phase.PLAYING:
        seat = game.trick.next_seat
        hand = game.hands[seat]
        if game.trick.lead:
            lead_group = group(game.trick.lead.cards[0], game.context)
            legal = tuple(c for c in hand if group(c, game.context) == lead_group)
        else:
            legal = hand
        game = game.play(seat, (rng.choice(legal or hand),))
    return game


def run_demo(deals: int = 3, seed: int = 7) -> Game:
    rng = random.Random(seed)
    game = Game()
    for number in range(1, deals + 1):
        game = prepare_demo(game, rng)
        trump_names = {"S": "黑桃", "H": "红桃", "C": "梅花", "D": "方片"}
        print(f"第{number}局：庄家座位{game.dealer}，{trump_names[game.context.trump_suit.value]}为主")
        if game.reflections:
            print("亮反：" + "；".join(f"座位{r.seat}亮{'五反' if r.cards[0].rank == '5' else '三反'}" for r in game.reflections))
        if game.exchanges:
            print("进贡还贡：" + "；".join(
                f"{e.obligation.giver}→{e.obligation.receiver}贡{e.gift.id}，还{e.returned.id}"
                for e in game.exchanges))
        else:
            reasons = {"no_obligation": "本局无进贡义务", "reflection": "亮三五反免贡",
                       "blackout": "断电免贡", "no_eligible_giver": "无合适主牌免贡",
                       "no_eligible_return": "无合法还贡牌免贡"}
            print(reasons.get(game.exemption, "本局无进贡"))
        game = finish_demo(game, rng)
        result = game.result
        print(f"打完{result.trick_count}轮：两队得分{result.team_points}，闲家{result.settlement.defender_points}分")
        print(f"下一局庄家：座位{result.settlement.next_dealer}；下局需进贡{len(result.settlement.tribute_obligations)}人")
        if number < deals:
            game = game.next_deal()
    return game
