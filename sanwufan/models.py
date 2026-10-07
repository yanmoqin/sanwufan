"""Immutable card values and public rule context, independent of UI/network code."""
from dataclasses import dataclass, field
from enum import Enum


class RuleViolation(ValueError):
    """An invalid action, with a stable code and a Chinese explanation."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class Suit(str, Enum):
    SPADES = "S"
    HEARTS = "H"
    CLUBS = "C"
    DIAMONDS = "D"
    JOKER = "J"


SUITS = (Suit.SPADES, Suit.HEARTS, Suit.CLUBS, Suit.DIAMONDS)
RANKS = ("3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2")


@dataclass(frozen=True)
class Card:
    suit: Suit
    rank: str

    def __post_init__(self):
        if not isinstance(self.suit, Suit):
            raise RuleViolation("INVALID_CARD", "牌的花色无效")
        allowed = ("small", "big") if self.suit == Suit.JOKER else RANKS
        if self.rank not in allowed:
            raise RuleViolation("INVALID_CARD", "牌的点数与花色不匹配")

    @property
    def id(self) -> str:
        return f"{self.suit.value}:{self.rank}"

    @classmethod
    def from_id(cls, value: str) -> "Card":
        if not isinstance(value, str):
            raise RuleViolation("INVALID_CARD", "牌编号必须是字符串")
        try:
            suit, rank = value.split(":")
            return cls(Suit(suit), rank)
        except ValueError as exc:
            if isinstance(exc, RuleViolation):
                raise
            raise RuleViolation("INVALID_CARD", "牌编号格式应为 S:A 或 J:big") from exc

    @property
    def points(self) -> int:
        return 5 if self.rank == "5" else 10 if self.rank in ("10", "K") else 0


def deck() -> tuple[Card, ...]:
    """One physical deck: 54 distinct cards, totaling 100 points."""
    return tuple(Card(s, r) for s in SUITS for r in RANKS) + (
        Card(Suit.JOKER, "small"), Card(Suit.JOKER, "big")
    )


def validate_seat(seat: int) -> None:
    if type(seat) is not int or seat not in range(4):
        raise RuleViolation("INVALID_SEAT", "座位必须是0到3的整数")


def unique_cards(cards) -> tuple[Card, ...]:
    cards = tuple(cards)
    if not all(isinstance(c, Card) for c in cards):
        raise RuleViolation("INVALID_CARD", "必须提供有效牌张")
    if len(cards) != len(set(cards)):
        raise RuleViolation("DUPLICATE_CARD", "一副牌中不能出现重复牌张")
    return cards


@dataclass(frozen=True)
class Reflection:
    seat: int
    cards: tuple[Card, ...]

    def __post_init__(self):
        validate_seat(self.seat)
        object.__setattr__(self, "cards", unique_cards(self.cards))
        if len(self.cards) != 3 or len({c.rank for c in self.cards}) != 1:
            raise RuleViolation("INVALID_REFLECTION", "三五反必须是三张同点数的牌")
        if self.cards[0].rank not in ("3", "5"):
            raise RuleViolation("INVALID_REFLECTION", "只能亮三张3或三张5")


@dataclass(frozen=True)
class RuleContext:
    trump_suit: Suit
    reflections: tuple[Reflection, ...] = ()
    exchanged_cards: frozenset[Card] = field(default_factory=frozenset)

    def __post_init__(self):
        if self.trump_suit not in SUITS or not isinstance(self.trump_suit, Suit):
            raise RuleViolation("INVALID_TRUMP", "主花色必须是四种普通花色之一")
        object.__setattr__(self, "reflections", tuple(self.reflections))
        object.__setattr__(self, "exchanged_cards", frozenset(unique_cards(self.exchanged_cards)))
        if not all(isinstance(r, Reflection) for r in self.reflections):
            raise RuleViolation("INVALID_REFLECTION", "亮反记录无效")
        reflected = [c for r in self.reflections for c in r.cards]
        unique_cards(reflected)
        if set(reflected) & self.exchanged_cards:
            raise RuleViolation("EXCHANGED_REFLECTION", "进贡还贡取得的牌不能组成三五反")

    @property
    def reflected_cards(self) -> frozenset[Card]:
        return frozenset(c for r in self.reflections for c in r.cards)

    @property
    def special_forbidden(self) -> frozenset[Card]:
        return self.reflected_cards | self.exchanged_cards


class PlayKind(str, Enum):
    SINGLE = "single"
    THROW = "throw"
    TRUE_GANG = "true_gang"
    FALSE_GANG = "false_gang"
