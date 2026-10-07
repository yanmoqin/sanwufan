"""Pure rules: no random choices, printing, mutation, or network access."""
from dataclasses import dataclass, replace

from .models import (
    Card, PlayKind, Reflection, RuleContext, RuleViolation, Suit,
    unique_cards, validate_seat,
)

TRUMP_GROUP = "T"
ORDINARY = {r: i for i, r in enumerate(("3", "4", "5", "6", "7", "8", "9", "10", "Q", "K", "A"))}
GANG_ORDER = {r: i for i, r in enumerate(("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"))}


def is_trump(card: Card, ctx: RuleContext) -> bool:
    return (
        card == Card(Suit.DIAMONDS, "5")
        or card.suit == Suit.JOKER
        or card == Card(Suit.SPADES, "Q")
        or card.rank in ("J", "2")
        or card.suit == ctx.trump_suit
        or card in ctx.reflected_cards
    )


def group(card: Card, ctx: RuleContext) -> str:
    return TRUMP_GROUP if is_trump(card, ctx) else card.suit.value


def strength(card: Card, ctx: RuleContext) -> tuple[int, int]:
    """Compare only cards eligible for the same trick. Equal strength keeps first."""
    if card == Card(Suit.DIAMONDS, "5"):
        return (11, 0)
    if card in ctx.reflected_cards:
        return (10 if card.rank == "5" else 9, 0)
    if card.suit == Suit.JOKER:
        return (8 if card.rank == "big" else 7, 0)
    if card == Card(Suit.SPADES, "Q"):
        return (6, 0)
    if card.rank == "J":
        return (5 if card.suit == ctx.trump_suit else 4, 0)
    if card.rank == "2":
        return (3 if card.suit == ctx.trump_suit else 2, 0)
    return (1 if card.suit == ctx.trump_suit else 0, ORDINARY[card.rank])


def declare_reflection(ctx: RuleContext, seat: int, hand, cards) -> RuleContext:
    reflection = Reflection(seat, tuple(cards))
    hand = unique_cards(hand)
    if not set(reflection.cards) <= set(hand):
        raise RuleViolation("NOT_OWNED", "只能亮出自己手中的牌")
    return replace(ctx, reflections=ctx.reflections + (reflection,))


def validate_selection(hand, cards, count: int | None = None) -> tuple[Card, ...]:
    hand, cards = unique_cards(hand), unique_cards(cards)
    if not cards or (count is not None and len(cards) != count):
        raise RuleViolation("WRONG_COUNT", "所选牌张数不符合要求")
    if not set(cards) <= set(hand):
        raise RuleViolation("NOT_OWNED", "不能使用不在手中的牌")
    return cards


def validate_bottom(hand, cards) -> tuple[Card, ...]:
    hand = unique_cards(hand)
    if len(hand) != 18:
        raise RuleViolation("WRONG_HAND_COUNT", "庄家拿底后必须持有18张牌")
    cards = validate_selection(hand, cards, 6)
    if any(c.points for c in cards):
        raise RuleViolation("POINTS_IN_BOTTOM", "不能扣任何5、10或K")
    return cards


def is_true_gang(cards, ctx: RuleContext) -> bool:
    cards = unique_cards(cards)
    return (
        len(cards) == 4 and len({c.rank for c in cards}) == 1
        and not set(cards) & ctx.special_forbidden
    )


def is_false_gang(cards, ctx: RuleContext) -> bool:
    cards = unique_cards(cards)
    queen = Card(Suit.SPADES, "Q")
    others = [c for c in cards if c != queen]
    return (
        len(cards) == 4 and queen in cards and len(others) == 3
        and len({c.rank for c in others}) == 1
        and not set(cards) & ctx.special_forbidden
    )


@dataclass(frozen=True)
class Lead:
    cards: tuple[Card, ...]
    kind: PlayKind
    throw_failed: bool = False


def prepare_lead(hand, cards, other_hands, ctx: RuleContext, kind: PlayKind | None = None) -> Lead:
    """Validate a lead; failed throws return only their smallest selected card."""
    cards = validate_selection(hand, cards)
    opponents = tuple(unique_cards(h) for h in other_hands)
    if len(opponents) != 3:
        raise RuleViolation("WRONG_PLAYERS", "必须提供其余三人的手牌供甩牌判定")
    if kind is not None and not isinstance(kind, PlayKind):
        raise RuleViolation("INVALID_KIND", "出牌类型无效")
    true, false = is_true_gang(cards, ctx), is_false_gang(cards, ctx)
    if kind is None:
        if true and false:
            raise RuleViolation("AMBIGUOUS_Q_GANG", "四张Q领出时须指定真杠或假杠")
        kind = (PlayKind.SINGLE if len(cards) == 1 else PlayKind.TRUE_GANG if true
                else PlayKind.FALSE_GANG if false else PlayKind.THROW)
    if kind == PlayKind.SINGLE:
        if len(cards) != 1:
            raise RuleViolation("WRONG_COUNT", "单张出牌必须选择一张")
    elif kind == PlayKind.TRUE_GANG:
        if not true:
            raise RuleViolation("INVALID_GANG", "这些牌不能组成真杠")
    elif kind == PlayKind.FALSE_GANG:
        if not false:
            raise RuleViolation("INVALID_GANG", "这些牌不能组成假杠")
    else:
        groups = {group(c, ctx) for c in cards}
        if len(cards) < 2 or len(groups) != 1 or TRUMP_GROUP in groups:
            raise RuleViolation("INVALID_THROW", "甩牌必须选择同一副牌花色的多张牌")
        minimum = min(cards, key=lambda c: strength(c, ctx))
        if any(group(c, ctx) in groups and strength(c, ctx) > strength(minimum, ctx)
               for h in opponents for c in h):
            return Lead((minimum,), PlayKind.SINGLE, throw_failed=True)
    return Lead(cards, kind)


def validate_follow(hand, cards, lead: Lead, ctx: RuleContext) -> tuple[Card, ...]:
    cards = validate_selection(hand, cards, len(lead.cards))
    hand = unique_cards(hand)
    if lead.kind in (PlayKind.TRUE_GANG, PlayKind.FALSE_GANG):
        if is_true_gang(cards, ctx):
            if lead.kind == PlayKind.FALSE_GANG or GANG_ORDER[cards[0].rank] > GANG_ORDER[lead.cards[0].rank]:
                return cards
        required = lambda c: is_trump(c, ctx) == (lead.kind == PlayKind.TRUE_GANG)
    else:
        lead_group = group(lead.cards[0], ctx)
        required = lambda c: group(c, ctx) == lead_group
    must_follow = min(len(cards), sum(required(c) for c in hand))
    if sum(required(c) for c in cards) != must_follow:
        raise RuleViolation("MUST_FOLLOW", "必须先跟尽应跟的牌，不足再补其他牌")
    return cards


def trick_winner(plays, lead: Lead, ctx: RuleContext) -> int:
    """Return a seat for four already validated plays in actual action order."""
    plays = tuple((seat, unique_cards(cards)) for seat, cards in plays)
    if len(plays) != 4 or len({seat for seat, _ in plays}) != 4:
        raise RuleViolation("INCOMPLETE_TRICK", "每轮必须由四名玩家各出一次牌")
    for seat, cards in plays:
        validate_seat(seat)
        if len(cards) != len(lead.cards):
            raise RuleViolation("WRONG_COUNT", "每名玩家必须出相同张数")
    if plays[0][1] != lead.cards:
        raise RuleViolation("INVALID_LEAD", "首家出牌与领出记录不一致")
    unique_cards(c for _, cards in plays for c in cards)
    if lead.kind in (PlayKind.TRUE_GANG, PlayKind.FALSE_GANG):
        # Only valid true gangs can beat the opening gang. Other cards are padding.
        key = lambda cards: (1, GANG_ORDER[cards[0].rank]) if is_true_gang(cards, ctx) else (0, 0)
        def play_key(play):
            if play[0] == plays[0][0] and lead.kind == PlayKind.FALSE_GANG:
                # Four Qs declared as a false gang remain a false gang, even though
                # the same physical cards could otherwise form a true gang.
                return (0, 0)
            return key(play[1])
    else:
        lead_group = group(lead.cards[0], ctx)

        def key(cards):
            groups = {group(c, ctx) for c in cards}
            if groups == {TRUMP_GROUP}:
                return (2, max(strength(c, ctx) for c in cards))
            if groups == {lead_group}:
                return (1, max(strength(c, ctx) for c in cards))
            return (0, (0, 0))
        play_key = lambda play: key(play[1])
    # Python max retains the first item on equal keys, implementing 先出者大.
    return max(plays, key=play_key)[0]
