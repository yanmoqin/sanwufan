"""Private special-card suggestions, using only the requesting player's hand."""
from itertools import combinations

from .models import Card, Suit
from .game import REFLECTION_PHASES
from .rules import is_false_gang, is_true_gang


def reflection_groups(game, seat):
    forbidden = game.context.special_forbidden if game.context else frozenset(
        c for r in game.reflections for c in r.cards)
    groups = []
    for rank in ('5', '3'):
        eligible = tuple(sorted((c for c in game.reflection_hand(seat) if c.rank == rank and c not in forbidden), key=lambda c: c.id))
        if len(eligible) < 3:
            continue
        preferred = (Card(Suit.DIAMONDS, '5') if rank == '5' else
                     Card(game.context.trump_suit, '3') if game.context else None)
        choices = list(combinations(eligible, 3))
        choices.sort(key=lambda cs: (preferred in cs if preferred else False, tuple(c.id for c in cs)))
        groups.append({'rank': rank, 'count': len(eligible), 'choices': choices})
    return groups


def special_hints(game, seat):
    reflections = []
    open_window = game.phase in REFLECTION_PHASES
    for group in reflection_groups(game, seat) if open_window else []:
        eligible = {c for cs in group['choices'] for c in cs}
        reflections.append({'rank': group['rank'], 'needs_choice': group['count'] == 4,
                            'choices': [{'cards': [c.id for c in cs],
                                         'keep': next((c.id for c in eligible if c not in cs), None)}
                                        for cs in group['choices']]})
    gangs = []
    if game.context:
        hand = [c for c in game.reflection_hand(seat) if c not in game.context.special_forbidden]
        queen = Card(Suit.SPADES, 'Q')
        for rank in sorted({c.rank for c in hand}):
            same = tuple(sorted((c for c in hand if c.rank == rank), key=lambda c: c.id))
            if len(same) == 4 and is_true_gang(same, game.context):
                gangs.append({'kind': 'true_gang', 'rank': rank, 'cards': [c.id for c in same]})
            if queen in hand:
                for triple in list(combinations((c for c in same if c != queen), 3))[:1]:
                    cs = (queen,) + triple
                    if is_false_gang(cs, game.context):
                        gangs.append({'kind': 'false_gang', 'rank': rank, 'cards': [c.id for c in cs]})
    return {'reflections': reflections, 'gangs': gangs,
            'revealed': [{'rank': r.cards[0].rank, 'cards': [c.id for c in r.cards]}
                         for r in game.reflections if r.seat == seat]}
