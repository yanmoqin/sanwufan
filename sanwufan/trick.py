"""One authoritative trick, consuming validated actions in seat order."""
from dataclasses import dataclass, replace

from .models import Card, PlayKind, RuleContext, RuleViolation, unique_cards, validate_seat
from .rules import Lead, prepare_lead, trick_winner, validate_follow


@dataclass(frozen=True)
class Play:
    seat: int
    cards: tuple[Card, ...]


@dataclass(frozen=True)
class TrickResult:
    winner: int
    points: int
    next_leader: int
    cards: tuple[Card, ...]
    leader: int | None = None
    plays: tuple[Play, ...] = ()
    lead: Lead | None = None


@dataclass(frozen=True)
class Trick:
    """Private server state. Never send all four hands to a browser."""
    context: RuleContext
    hands: tuple[tuple[Card, ...], ...]
    leader: int
    plays: tuple[Play, ...] = ()
    lead: Lead | None = None

    def __post_init__(self):
        validate_seat(self.leader)
        if not isinstance(self.context, RuleContext):
            raise RuleViolation("INVALID_CONTEXT", "规则上下文无效")
        object.__setattr__(self, "hands", tuple(unique_cards(h) for h in self.hands))
        object.__setattr__(self, "plays", tuple(self.plays))
        if len(self.hands) != 4:
            raise RuleViolation("WRONG_PLAYERS", "必须提供四人的手牌")
        if len(self.plays) > 4 or any(not isinstance(p, Play) for p in self.plays):
            raise RuleViolation("INVALID_STATE", "单轮出牌记录无效")
        for i, play in enumerate(self.plays):
            if play.seat != (self.leader + i) % 4:
                raise RuleViolation("INVALID_STATE", "出牌记录不符合座位顺序")
        unique_cards([c for h in self.hands for c in h] + [c for p in self.plays for c in p.cards])
        initial_sizes = [len(h) for h in self.hands]
        for p in self.plays:
            initial_sizes[p.seat] += len(p.cards)
        if len(set(initial_sizes)) != 1 or initial_sizes[0] == 0:
            raise RuleViolation("WRONG_HAND_COUNT", "一轮开始时四人应持有相同的非零张数")
        if bool(self.plays) != (self.lead is not None):
            raise RuleViolation("INVALID_STATE", "领出记录与出牌记录不一致")

    @property
    def finished(self) -> bool:
        return len(self.plays) == 4

    @property
    def next_seat(self) -> int | None:
        return None if self.finished else (self.leader + len(self.plays)) % 4

    def play(self, seat: int, cards, kind: PlayKind | None = None) -> "Trick":
        """Return a new state. Rejected actions leave the original state untouched."""
        validate_seat(seat)
        if self.finished:
            raise RuleViolation("TRICK_FINISHED", "本轮已经结束")
        if seat != self.next_seat:
            raise RuleViolation("NOT_YOUR_TURN", "尚未轮到你出牌")
        lead = self.lead
        if lead is None:
            lead = prepare_lead(
                self.hands[seat], cards,
                [h for i, h in enumerate(self.hands) if i != seat], self.context, kind,
            )
            accepted = lead.cards
        else:
            if kind is not None:
                raise RuleViolation("INVALID_KIND", "跟牌无需声明领出类型")
            accepted = validate_follow(self.hands[seat], cards, lead, self.context)
        hands = list(self.hands)
        hands[seat] = tuple(c for c in hands[seat] if c not in accepted)
        return replace(self, hands=tuple(hands), lead=lead, plays=self.plays + (Play(seat, accepted),))

    def result(self) -> TrickResult:
        if not self.finished:
            raise RuleViolation("INCOMPLETE_TRICK", "四人出完牌后才能结算本轮")
        winner = trick_winner(((p.seat, p.cards) for p in self.plays), self.lead, self.context)
        cards = tuple(c for p in self.plays for c in p.cards)
        return TrickResult(winner, sum(c.points for c in cards), winner, cards,
                           leader=self.leader, plays=self.plays, lead=self.lead)
