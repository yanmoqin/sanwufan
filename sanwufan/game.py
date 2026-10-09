"""Authoritative, immutable deal lifecycle for four players.

Only the server calls dealing/shuffling methods. Player actions operate on the
returned state; failed actions never commit partial changes.
"""
from dataclasses import dataclass, field, replace
from enum import Enum
from random import SystemRandom

from .models import Card, PlayKind, Reflection, RuleContext, RuleViolation, Suit, deck, unique_cards, validate_seat
from .rules import declare_reflection, is_trump, strength, validate_bottom, validate_selection
from .settlement import Settlement, Tribute, settle
from .trick import Trick, TrickResult


class Phase(str, Enum):
    WAITING = "waiting"
    DEALING = "dealing"
    FIRST_NO_TRUMP = "first_no_trump"
    DRAW_TRUMP = "draw_trump"
    DECLARING = "declaring"
    TRIBUTE_GIVE = "tribute_give"
    TRIBUTE_RETURN = "tribute_return"
    TAKE_BOTTOM = "take_bottom"
    DISCARD = "discard"
    PLAYING = "playing"
    SETTLED = "settled"


REFLECTION_PHASES = (Phase.DEALING, Phase.DECLARING, Phase.TRIBUTE_GIVE,
                     Phase.TRIBUTE_RETURN, Phase.TAKE_BOTTOM, Phase.DISCARD)


class FirstNoTrump(str, Enum):
    REDEAL = "redeal"
    RANDOM_DEALER = "random_dealer"


class LateReflection(str, Enum):
    KEEP_EXCHANGES = "keep_exchanges"
    ROLLBACK_EXCHANGES = "rollback_exchanges"


@dataclass(frozen=True)
class GameOptions:
    first_no_trump: FirstNoTrump = FirstNoTrump.REDEAL
    late_reflection: LateReflection = LateReflection.KEEP_EXCHANGES

    def __post_init__(self):
        for value, enum in ((self.first_no_trump, FirstNoTrump),
                            (self.late_reflection, LateReflection)):
            if not isinstance(value, enum):
                raise RuleViolation("INVALID_OPTIONS", "牌桌规则设置无效")


@dataclass(frozen=True)
class Exchange:
    obligation: Tribute
    gift: Card
    returned: Card | None = None


@dataclass(frozen=True)
class DealResult:
    number: int
    dealer: int
    team_points: tuple[int, int]
    settlement: Settlement
    trick_count: int

    def to_dict(self) -> dict:
        return {"number": self.number, "dealer": self.dealer,
                "team_points": list(self.team_points), "trick_count": self.trick_count,
                "settlement": self.settlement.to_dict()}


EMPTY_HANDS = ((), (), (), ())


@dataclass(frozen=True)
class Game:
    """Private state: use view_for(seat) to expose only that player's cards."""
    options: GameOptions = field(default_factory=GameOptions)
    phase: Phase = Phase.WAITING
    revision: int = 0
    number: int = 0
    ready_seats: frozenset[int] = field(default_factory=frozenset)
    dealer: int | None = None
    context: RuleContext | None = None
    reflections: tuple[Reflection, ...] = ()
    stock: tuple[Card, ...] = ()
    dealt_count: int = 0
    deal_start_seat: int = 0
    hands: tuple[tuple[Card, ...], ...] = EMPTY_HANDS
    bottom: tuple[Card, ...] = ()
    called_two: Card | None = None
    caller: int | None = None
    blackout: bool = False
    last_draw: Card | None = None
    confirmed: frozenset[int] = field(default_factory=frozenset)
    obligations: tuple[Tribute, ...] = ()
    exchanges: tuple[Exchange, ...] = ()
    exemption: str | None = None
    pre_tribute_hands: tuple[tuple[Card, ...], ...] | None = None
    taken_bottom: tuple[Card, ...] = ()
    trick: Trick | None = None
    tricks: tuple[TrickResult, ...] = ()
    team_points: tuple[int, int] = (0, 0)
    result: DealResult | None = None
    history: tuple[DealResult, ...] = ()

    def _require(self, *phases):
        if self.phase not in phases:
            raise RuleViolation("WRONG_PHASE", f"当前阶段为{self.phase.value}，不能执行此操作")

    def _change(self, **updates) -> "Game":
        result = replace(self, revision=self.revision + 1, **updates)
        result.check_invariants()
        return result

    def check_invariants(self) -> None:
        """Check physical cards and points after every successfully accepted action."""
        if len(self.hands) != 4:
            raise RuleViolation("INVALID_STATE", "牌局必须包含四个座位")
        validate_seat(self.deal_start_seat)
        active = tuple(c for p in self.trick.plays for c in p.cards) if self.trick else ()
        captured = tuple(c for t in self.tricks for c in t.cards)
        live = unique_cards(tuple(c for h in self.hands for c in h) + self.stock + self.bottom + active + captured)
        if self.phase == Phase.WAITING:
            if live:
                raise RuleViolation("INVALID_STATE", "等待开局时不应持有本局牌张")
        elif len(live) != 54 or set(live) != set(deck()):
            raise RuleViolation("INVALID_STATE", "本局54张牌必须完整且不重复")
        if self.trick and self.trick.hands != self.hands:
            raise RuleViolation("INVALID_STATE", "单轮手牌与牌局手牌不一致")
        if sum(self.team_points) != sum(c.points for c in captured):
            raise RuleViolation("INVALID_STATE", "累计分数与已收牌分数不一致")
        if any(c.points for c in self.bottom) and self.phase in (Phase.PLAYING, Phase.SETTLED):
            raise RuleViolation("INVALID_STATE", "扣底中不能包含分牌")
        if self.phase == Phase.SETTLED and (any(self.hands) or sum(self.team_points) != 100):
            raise RuleViolation("INVALID_STATE", "结束时手牌必须打完且两队合计100分")

    def ready(self, seat: int, ready: bool = True) -> "Game":
        self._require(Phase.WAITING)
        validate_seat(seat)
        if type(ready) is not bool:
            raise RuleViolation("INVALID_READY", "准备状态必须是布尔值")
        seats = self.ready_seats | {seat} if ready else self.ready_seats - {seat}
        return self._change(ready_seats=frozenset(seats))

    def start_deal(self, order=None) -> "Game":
        """Server-only: supply a full fixed order for tests, or shuffle securely."""
        self._require(Phase.WAITING)
        if self.ready_seats != frozenset(range(4)):
            raise RuleViolation("NOT_ALL_READY", "四人全部准备后才能发牌")
        if order is None:
            order = list(deck())
            SystemRandom().shuffle(order)
        order = unique_cards(order)
        if len(order) != 54 or set(order) != set(deck()):
            raise RuleViolation("INVALID_DECK", "发牌顺序必须包含完整的54张牌")
        return self._change(phase=Phase.DEALING, number=self.number + 1, stock=order)

    def deal_one(self) -> "Game":
        self._require(Phase.DEALING)
        if self.dealt_count == 48:
            raise RuleViolation("DEAL_FINISHED", "48张手牌已经发完，请结束发牌阶段")
        seat = (self.deal_start_seat + self.dealt_count) % 4
        hands = list(self.hands)
        hands[seat] += (self.stock[0],)
        count = self.dealt_count + 1
        return self._change(hands=tuple(hands), stock=self.stock[1:], dealt_count=count)

    def finish_dealing(self) -> "Game":
        """Close the last claim window after all 48 hand cards were delivered."""
        self._require(Phase.DEALING)
        if self.dealt_count != 48:
            raise RuleViolation("DEAL_NOT_FINISHED", "必须先发完48张手牌")
        phase = (Phase.DECLARING if self.context else
                 Phase.FIRST_NO_TRUMP if self.dealer is None else Phase.DRAW_TRUMP)
        return self._change(phase=phase, bottom=self.stock, stock=())

    def call_trump(self, seat: int, card: Card) -> "Game":
        self._require(Phase.DEALING)
        validate_seat(seat)
        if self.context is not None:
            raise RuleViolation("ALREADY_CALLED", "主花色已经确定，不能再次亮2改主")
        validate_selection(self.hands[seat], (card,), 1)
        if card.rank != "2":
            raise RuleViolation("NOT_TWO", "只能亮2来确定主花色")
        return self._change(context=RuleContext(card.suit, self.reflections), caller=seat,
                            called_two=card, dealer=seat if self.dealer is None else self.dealer)

    def redeal(self, order=None) -> "Game":
        self._require(Phase.FIRST_NO_TRUMP)
        if self.options.first_no_trump != FirstNoTrump.REDEAL:
            raise RuleViolation("WRONG_FALLBACK", "本牌桌设置为首局随机定庄")
        blank = Game(options=self.options, revision=self.revision, number=self.number - 1,
                     ready_seats=self.ready_seats, history=self.history,
                     deal_start_seat=self.deal_start_seat)
        return blank.start_deal(order)

    def choose_first_dealer(self, seat: int | None = None) -> "Game":
        self._require(Phase.FIRST_NO_TRUMP)
        if self.options.first_no_trump != FirstNoTrump.RANDOM_DEALER:
            raise RuleViolation("WRONG_FALLBACK", "本牌桌设置为首局重新发牌")
        if seat is None:
            seat = SystemRandom().randrange(4)
        validate_seat(seat)
        return self._change(dealer=seat, phase=Phase.DRAW_TRUMP)

    def draw_trump(self, seat: int, index: int | None = None) -> "Game":
        """Draw from hidden bottom; jokers cause another draw, not a lost card."""
        self._require(Phase.DRAW_TRUMP)
        validate_seat(seat)
        if seat % 2 == self.dealer % 2:
            raise RuleViolation("NOT_DEFENDER", "断电时由闲家方抽底定主")
        if index is None:
            index = SystemRandom().randrange(6)
        if type(index) is not int or index not in range(6):
            raise RuleViolation("INVALID_DRAW", "抽底位置必须是0到5的整数")
        card = self.bottom[index]
        if card.suit == Suit.JOKER:
            return self._change(last_draw=card, blackout=True)
        return self._change(last_draw=card, blackout=True, phase=Phase.DECLARING,
                            context=RuleContext(card.suit, self.reflections), caller=seat)

    def can_revolution(self, seat: int) -> bool:
        validate_seat(seat)
        return (self.phase in (Phase.DEALING, Phase.FIRST_NO_TRUMP, Phase.DRAW_TRUMP,
                               Phase.DECLARING, Phase.TRIBUTE_GIVE, Phase.TAKE_BOTTOM)
                and self.dealt_count == 48 and not self.exchanges and not self.taken_bottom
                and len(self.hands[seat]) == 12 and not any(c.points for c in self.hands[seat]))

    def revolution(self, seat: int, order=None) -> "Game":
        """A voluntary scoreless hand resets the match before any card exchange."""
        if not self.can_revolution(seat):
            raise RuleViolation("REVOLUTION_UNAVAILABLE", "发完12张手牌且没有5、10、K，进贡或拿底前才可革命")
        first = self.dealer if self.dealer is not None else self.deal_start_seat
        return Game(options=self.options, revision=self.revision,
                    ready_seats=frozenset(range(4)), deal_start_seat=first).start_deal(order)

    def reflection_hand(self, seat: int) -> tuple[Card, ...]:
        """Own cards still available for specials, excluding staged transfers."""
        moving = (frozenset(c for e in self.exchanges for c in (e.gift, e.returned) if c is not None)
                  if self.phase in (Phase.TRIBUTE_GIVE, Phase.TRIBUTE_RETURN) else frozenset())
        return tuple(c for c in self.hands[seat] if c not in moving)

    def reveal(self, seat: int, cards) -> "Game":
        self._require(*REFLECTION_PHASES)
        validate_seat(seat)
        selected = validate_selection(self.reflection_hand(seat), cards, 3)
        if self.context is None:
            reflection = Reflection(seat, selected)
            unique_cards(c for r in self.reflections + (reflection,) for c in r.cards)
            return self._change(reflections=self.reflections + (reflection,))
        ctx = declare_reflection(self.context, seat, self.hands[seat], selected)
        game = self
        # A late declaration only cancels exchanges if the declaring team owed tribute.
        if (self.phase in (Phase.TAKE_BOTTOM, Phase.DISCARD)
                and self.options.late_reflection == LateReflection.ROLLBACK_EXCHANGES
                and any(t.giver % 2 == seat % 2 for t in self.obligations)
                and self.exchanges and self.pre_tribute_hands is not None):
            restored = list(self.pre_tribute_hands)
            if self.phase == Phase.DISCARD:
                restored[self.dealer] += self.taken_bottom
            if not set(selected) <= set(restored[seat]):
                raise RuleViolation("REFLECTION_AFTER_ROLLBACK", "撤回贡牌后这些牌不能组成你的三五反")
            ctx = RuleContext(ctx.trump_suit, ctx.reflections)
            game = replace(self, hands=tuple(restored), exchanges=(), exemption="late_reflection")
        game = game._change(context=ctx, reflections=ctx.reflections)
        if self.phase in (Phase.TRIBUTE_GIVE, Phase.TRIBUTE_RETURN):
            # Choices are staged; cancelling restores the original hands for
            # everyone, including a giver who has already selected a gift.
            exemption = ("reflection" if any(t.giver % 2 == seat % 2 for t in self.obligations)
                         else "no_eligible_return" if any(not game._return_candidates(t.receiver)
                                                         for t in self.obligations) else None)
            if exemption:
                game = game._change(phase=Phase.TAKE_BOTTOM, exchanges=(), exemption=exemption)
        return game

    def confirm_declarations(self, seat: int) -> "Game":
        self._require(Phase.DECLARING)
        validate_seat(seat)
        if seat in self.confirmed:
            raise RuleViolation("ALREADY_CONFIRMED", "不能重复确认")
        confirmed = self.confirmed | {seat}
        game = self._change(confirmed=frozenset(confirmed))
        return game._begin_tribute() if len(confirmed) == 4 else game

    def _return_candidates(self, receiver: int) -> tuple[Card, ...]:
        return tuple(c for c in self.hands[receiver] if is_trump(c, self.context) and not c.points
                     and c not in self.context.reflected_cards)

    def _begin_tribute(self) -> "Game":
        if not self.obligations:
            return self._change(phase=Phase.TAKE_BOTTOM, exemption="no_obligation")
        for t in self.obligations:
            if any(r.seat % 2 == t.giver % 2 for r in self.reflections):
                return self._change(phase=Phase.TAKE_BOTTOM, exemption="reflection")
            if self.blackout and t.giver % 2 != self.dealer % 2:
                return self._change(phase=Phase.TAKE_BOTTOM, exemption="blackout")
            trumps = tuple(c for c in self.hands[t.giver] if is_trump(c, self.context))
            if not trumps or all(c.points for c in trumps):
                return self._change(phase=Phase.TAKE_BOTTOM, exemption="no_eligible_giver")
            if not self._return_candidates(t.receiver):
                return self._change(phase=Phase.TAKE_BOTTOM, exemption="no_eligible_return")
        return self._change(phase=Phase.TRIBUTE_GIVE, pre_tribute_hands=self.hands)

    def give_tribute(self, seat: int, card: Card) -> "Game":
        self._require(Phase.TRIBUTE_GIVE)
        validate_seat(seat)
        obligation = next((t for t in self.obligations if t.giver == seat), None)
        if obligation is None:
            raise RuleViolation("NOT_GIVER", "你不需要进贡")
        if any(e.obligation.giver == seat for e in self.exchanges):
            raise RuleViolation("ALREADY_GIVEN", "你已经选择贡牌")
        validate_selection(self.hands[seat], (card,), 1)
        trumps = tuple(c for c in self.hands[seat] if is_trump(c, self.context))
        if not is_trump(card, self.context) or strength(card, self.context) != max(strength(c, self.context) for c in trumps):
            raise RuleViolation("MUST_GIVE_LARGEST", "必须进贡手上最大的主牌")
        exchanges = self.exchanges + (Exchange(obligation, card),)
        return self._change(exchanges=exchanges, phase=Phase.TRIBUTE_RETURN
                            if len(exchanges) == len(self.obligations) else Phase.TRIBUTE_GIVE)

    def return_tribute(self, seat: int, card: Card) -> "Game":
        self._require(Phase.TRIBUTE_RETURN)
        validate_seat(seat)
        exchange = next((e for e in self.exchanges if e.obligation.receiver == seat), None)
        if exchange is None:
            raise RuleViolation("NOT_RECEIVER", "你不需要还贡")
        if exchange.returned is not None:
            raise RuleViolation("ALREADY_RETURNED", "你已经选择还贡牌")
        if card == exchange.gift:
            raise RuleViolation("RETURNED_GIFT", "不能把收到的贡牌原样返还")
        validate_selection(self.hands[seat], (card,), 1)
        if card not in self._return_candidates(seat):
            raise RuleViolation("INVALID_RETURN", "还贡必须是不带分的主牌，且不能使用已亮出的三反牌")
        exchanges = tuple(replace(e, returned=card) if e is exchange else e for e in self.exchanges)
        game = self._change(exchanges=exchanges)
        if not all(e.returned is not None for e in exchanges):
            return game
        return game._commit_exchanges()

    def _commit_exchanges(self) -> "Game":
        hands = [list(h) for h in self.hands]
        for e in self.exchanges:
            hands[e.obligation.giver].remove(e.gift)
            hands[e.obligation.receiver].remove(e.returned)
        for e in self.exchanges:
            hands[e.obligation.receiver].append(e.gift)
            hands[e.obligation.giver].append(e.returned)
        moved = frozenset(c for e in self.exchanges for c in (e.gift, e.returned))
        ctx = replace(self.context, exchanged_cards=moved)
        return self._change(hands=tuple(tuple(h) for h in hands), context=ctx, phase=Phase.TAKE_BOTTOM)

    def take_bottom(self, seat: int) -> "Game":
        self._require(Phase.TAKE_BOTTOM)
        validate_seat(seat)
        if seat != self.dealer:
            raise RuleViolation("NOT_DEALER", "只有庄家可以拿底")
        hands = list(self.hands)
        hands[seat] += self.bottom
        return self._change(hands=tuple(hands), taken_bottom=self.bottom, bottom=(), phase=Phase.DISCARD)

    def discard(self, seat: int, cards) -> "Game":
        self._require(Phase.DISCARD)
        validate_seat(seat)
        if seat != self.dealer:
            raise RuleViolation("NOT_DEALER", "只有庄家可以扣底")
        selected = validate_bottom(self.hands[seat], cards)
        hands = list(self.hands)
        hands[seat] = tuple(c for c in hands[seat] if c not in selected)
        return self._change(hands=tuple(hands), bottom=selected, phase=Phase.PLAYING,
                            trick=Trick(self.context, tuple(hands), self.dealer))

    def play(self, seat: int, cards, kind: PlayKind | None = None) -> "Game":
        self._require(Phase.PLAYING)
        trick = self.trick.play(seat, cards, kind)
        if not trick.finished:
            return self._change(trick=trick, hands=trick.hands)
        result = trick.result()
        points = list(self.team_points)
        points[result.winner % 2] += result.points
        tricks = self.tricks + (result,)
        if any(trick.hands):
            return self._change(hands=trick.hands, tricks=tricks, team_points=tuple(points),
                                trick=Trick(self.context, trick.hands, result.next_leader))
        decision = settle(self.dealer, points[1 - self.dealer % 2])
        summary = DealResult(self.number, self.dealer, tuple(points), decision, len(tricks))
        return self._change(hands=trick.hands, tricks=tricks, team_points=tuple(points),
                            trick=None, result=summary, phase=Phase.SETTLED)

    def next_deal(self) -> "Game":
        self._require(Phase.SETTLED)
        return Game(options=self.options, revision=self.revision + 1, number=self.number,
                    dealer=self.result.settlement.next_dealer,
                    obligations=self.result.settlement.tribute_obligations,
                    history=self.history + (self.result,))

    def view_for(self, seat: int) -> dict:
        """JSON-compatible per-seat snapshot; private cards never cross seats."""
        validate_seat(seat)
        shown_hands = [list(h) for h in self.hands]
        if self.phase in (Phase.TRIBUTE_GIVE, Phase.TRIBUTE_RETURN):
            for e in self.exchanges:
                shown_hands[e.obligation.giver].remove(e.gift)
                shown_hands[e.obligation.receiver].append(e.gift)
                if e.returned is not None:
                    shown_hands[e.obligation.receiver].remove(e.returned)
                    shown_hands[e.obligation.giver].append(e.returned)
        return {
            "revision": self.revision, "phase": self.phase.value, "deal_number": self.number,
            "options": {"first_no_trump": self.options.first_no_trump.value,
                        "late_reflection": self.options.late_reflection.value},
            "dealer": self.dealer, "trump_suit": self.context.trump_suit.value if self.context else None,
            "ready_seats": sorted(self.ready_seats), "confirmed_seats": sorted(self.confirmed),
            "hand": [c.id for c in shown_hands[seat]], "hand_counts": [len(h) for h in shown_hands],
            "stock_count": len(self.stock), "bottom_count": len(self.bottom),
            "dealt_count": self.dealt_count,
            "next_deal_seat": (self.deal_start_seat + self.dealt_count) % 4 if self.phase == Phase.DEALING and self.dealt_count < 48 else None,
            "bottom_cards": [c.id for c in self.bottom] if self.phase in (Phase.PLAYING, Phase.SETTLED) else [],
            "called_two": self.called_two.id if self.called_two else None, "caller": self.caller,
            "reflections": [{"seat": r.seat, "cards": [c.id for c in r.cards]} for r in self.reflections],
            "blackout": self.blackout, "last_draw": self.last_draw.id if self.last_draw else None,
            "exemption": self.exemption,
            "tribute_obligations": [{"giver": t.giver, "receiver": t.receiver} for t in self.obligations],
            "exchanges": [{"giver": e.obligation.giver, "receiver": e.obligation.receiver,
                           "gift": e.gift.id if seat in (e.obligation.giver, e.obligation.receiver) else None,
                           "returned": e.returned.id if e.returned and seat in (e.obligation.giver, e.obligation.receiver) else None,
                           "given": True, "return_chosen": e.returned is not None} for e in self.exchanges],
            "next_seat": self.trick.next_seat if self.trick else None,
            "current_lead": ({"kind": self.trick.lead.kind.value,
                              "cards": [c.id for c in self.trick.lead.cards],
                              "throw_failed": self.trick.lead.throw_failed}
                             if self.trick and self.trick.lead else None),
            "current_plays": [{"seat": p.seat, "cards": [c.id for c in p.cards]} for p in self.trick.plays] if self.trick else [],
            "last_trick": ({"winner": self.tricks[-1].winner, "points": self.tricks[-1].points,
                            "leader": self.tricks[-1].leader,
                            "kind": self.tricks[-1].lead.kind.value,
                            "throw_failed": self.tricks[-1].lead.throw_failed,
                            "plays": [{"seat": p.seat, "cards": [c.id for c in p.cards]} for p in self.tricks[-1].plays],
                            "cards": [c.id for c in self.tricks[-1].cards]} if self.tricks else None),
            "trick_count": len(self.tricks), "team_points": list(self.team_points),
            "trick_history": [{"round": i + 1, "winner": t.winner, "points": t.points,
                               "leader": t.leader, "kind": t.lead.kind.value,
                               "throw_failed": t.lead.throw_failed,
                               "plays": [{"seat": p.seat, "cards": [c.id for c in p.cards]}
                                         for p in t.plays]} for i, t in enumerate(self.tricks)],
            "result": self.result.to_dict() if self.result else None,
        }
