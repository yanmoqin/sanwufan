"""One human and three simple automatic players, driven by the real engine."""
from itertools import combinations
import random
import secrets
from threading import RLock
import time

from .game import FirstNoTrump, Game, Phase, REFLECTION_PHASES
from .models import Card, PlayKind, RuleViolation, deck, validate_seat
from .rules import is_trump, strength, validate_follow
from .suggestions import reflection_groups, special_hints


NAMES = ("你", "阿北", "小岚", "老陈")
DEAL_INTERVAL = 0.4
SUIT_LABELS = {"S": "♠", "H": "♥", "C": "♣", "D": "♦", "J": ""}


def card_label(card):
    return {"small": "小王", "big": "大王"}.get(card.rank, SUIT_LABELS[card.suit.value] + card.rank)


def low_key(card, context):
    return (card.points, is_trump(card, context), strength(card, context))


def legal_play(game, seat):
    """Choose a legal move using only this hand and the public lead, not strategy."""
    hand = sorted(game.hands[seat], key=lambda c: low_key(c, game.context))
    if game.trick.lead is None:
        return (hand[0],)
    for selected in combinations(hand, len(game.trick.lead.cards)):
        try:
            return validate_follow(hand, selected, game.trick.lead, game.context)
        except RuleViolation:
            continue
    raise RuntimeError("没有找到合法跟牌")


def reflection_choices(game, seat):
    return [g['choices'][0] for g in reflection_groups(game, seat)]


def suggested_cards(game, seat, action):
    if action == "play":
        return legal_play(game, seat)
    if action == "discard":
        return tuple(sorted((c for c in game.hands[seat] if not c.points),
                            key=lambda c: low_key(c, game.context))[:6])
    if action == "give_tribute":
        return (max((c for c in game.hands[seat] if is_trump(c, game.context)),
                    key=lambda c: strength(c, game.context)),)
    if action == "return_tribute":
        return (min((c for c in game.hands[seat] if is_trump(c, game.context)
                     and not c.points and c not in game.context.reflected_cards),
                    key=lambda c: strength(c, game.context)),)
    if action == "call_trump":
        return (next(c for c in game.hands[seat] if c.rank == "2"),)
    if action == "reveal":
        return reflection_choices(game, seat)[0]
    return ()


class PracticeRoom:
    """Server-private practice room. All actions and ticks share the same lock."""

    def __init__(self, seed=None):
        self.lock = RLock()
        self.rng = random.Random(seed) if seed is not None else None
        self.game = Game()
        self.table_id = secrets.token_hex(12)
        self.version = 0
        self.auto = False
        self.events = ["欢迎入座。你与小岚一队，阿北与老陈一队。"]
        self.next_tick = 0.0
        self.last_active = time.monotonic()
        self.two_seen = {}
        self.claim_deadline = None
        self.fault = None
        self.names = list(NAMES)
        self.social_sequence = 0
        self.social_events = []
        self.emote_last = {}

    def _order(self):
        if self.rng is None:
            return None
        order = list(deck())
        self.rng.shuffle(order)
        return order

    def _log(self, message):
        if message:
            self.events = (self.events + [message])[-40:]

    def _commit(self, game, message=None):
        previous = self.game
        self.game = game
        self.version += 1
        self._log(message)
        if len(game.tricks) > len(previous.tricks):
            t = game.tricks[-1]
            self._log(f"{self.names[t.winner]}收得{t.points}分，并领出下一轮。")
        if game.phase == Phase.SETTLED:
            self.auto = False
            result = game.result.settlement
            self._log(f"本局结束：闲家{result.defender_points}分，下局由{self.names[result.next_dealer]}坐庄。")

    def available(self, seat=0):
        g = self.game
        actions = []
        if g.phase == Phase.WAITING and seat not in g.ready_seats:
            actions.append("ready")
        if g.phase == Phase.DEALING and g.context is None and any(c.rank == "2" for c in g.hands[seat]):
            actions.append("call_trump")
        if g.phase in REFLECTION_PHASES and reflection_choices(g, seat):
            actions.append("reveal")
        if g.phase == Phase.DECLARING and seat not in g.confirmed:
            actions.append("confirm")
        if g.phase == Phase.DRAW_TRUMP and seat % 2 != g.dealer % 2:
            actions.append("draw_trump")
        if (g.phase == Phase.TRIBUTE_GIVE and any(t.giver == seat for t in g.obligations)
                and not any(e.obligation.giver == seat for e in g.exchanges)):
            actions.append("give_tribute")
        if (g.phase == Phase.TRIBUTE_RETURN and any(e.obligation.receiver == seat and e.returned is None
                                                 for e in g.exchanges)):
            actions.append("return_tribute")
        if g.phase == Phase.TAKE_BOTTOM and seat == g.dealer:
            actions.append("take_bottom")
        if g.phase == Phase.DISCARD and seat == g.dealer:
            actions.append("discard")
        if g.phase == Phase.PLAYING and g.trick.next_seat == seat:
            actions.append("play")
        if g.phase == Phase.SETTLED:
            actions.append("next_deal")
        return actions

    def _perform(self, seat, action, cards=(), kind=None):
        g = self.game
        label = self.names[seat]
        text = None
        if action == "ready":
            g = g.ready(seat)
            text = f"{label}已准备。"
        elif action == "call_trump":
            if len(cards) != 1:
                raise RuleViolation("WRONG_CARD_COUNT", "请选择一张2")
            g = g.call_trump(seat, cards[0])
            g = self._complete_called_deal(g)
            text = f"{label}亮出{card_label(cards[0])}，确定主花色，剩余手牌已发完。"
        elif action == "reveal":
            g = g.reveal(seat, cards)
            text = f"{label}亮出{'五反' if cards[0].rank == '5' else '三反'}。"
        elif action == "confirm":
            self._auto_reflections()
            g = self.game
            g = g.confirm_declarations(seat)
            text = f"{label}确认继续；扣牌前仍可补亮反。"
        elif action == "draw_trump":
            g = g.draw_trump(seat, self.rng.randrange(6) if self.rng else None)
            text = f"{label}抽出{card_label(g.last_draw)}定主。" if g.context else f"{label}抽到王，重新抽底。"
        elif action in ("give_tribute", "return_tribute"):
            if len(cards) != 1:
                raise RuleViolation("WRONG_CARD_COUNT", "请选择一张牌")
            g = g.give_tribute(seat, cards[0]) if action == "give_tribute" else g.return_tribute(seat, cards[0])
            text = f"{label}已选择{'贡牌' if action == 'give_tribute' else '还贡牌'}。"
        elif action == "take_bottom":
            g = g.take_bottom(seat)
            text = f"{label}拿起6张底牌。"
        elif action == "discard":
            g = g.discard(seat, cards)
            text = f"{label}扣下6张底牌，开始出牌。"
        elif action == "play":
            g = g.play(seat, cards, kind)
            old_plays = len(self.game.trick.plays)
            if len(g.tricks) > len(self.game.tricks):
                accepted = g.tricks[-1].plays[-1].cards
                lead = g.tricks[-1].lead
            else:
                accepted = g.trick.plays[-1].cards
                lead = g.trick.lead
            text = f"{label}出牌：{' '.join(card_label(c) for c in accepted)}。"
            if old_plays == 0 and lead.throw_failed:
                text += "甩牌未成功，实际领出一张，其余牌保留。"
        elif action == "next_deal":
            g = g.next_deal()
            self.two_seen = {}
            self.claim_deadline = None
            text = "进入下一局，请重新准备。"
        else:
            raise RuleViolation("UNKNOWN_ACTION", "不支持此操作")
        self._commit(g, text)
        if action in ("call_trump", "take_bottom", "draw_trump"):
            self._auto_reflections()

    def _complete_called_deal(self, game):
        """Fill only the remaining 48 hand cards; the bottom stays private."""
        while game.dealt_count < 48:
            game = game.deal_one()
        self.claim_deadline = None
        return game.finish_dealing()

    def _auto_reflections(self):
        """Wait for the complete hand; leave every four-of-a-kind to its owner."""
        if self.game.phase not in (Phase.DECLARING, Phase.TAKE_BOTTOM, Phase.DISCARD):
            return
        for seat in range(4):
            if 'reveal' not in self.available(seat):
                continue
            for group in reflection_groups(self.game, seat):
                if group['count'] == 3:
                    self._perform(seat, 'reveal', group['choices'][0])

    def _deal_card(self):
        seat = self.game.dealt_count % 4
        self._commit(self.game.deal_one())
        card = self.game.hands[seat][-1]
        if self.game.number == 1 and self.game.context is None and card.rank == '2':
            self._perform(seat, 'call_trump', (card,))

    def _social(self, source, target, item, sender):
        validate_seat(source)
        validate_seat(target)
        if target == source:
            raise RuleViolation("INVALID_TARGET", "请选择另一位已入座的玩家")
        if item not in ('egg', 'tomato', 'flower', 'clap'):
            raise RuleViolation("INVALID_EMOTE", "不支持这种互动")
        now = time.time()
        if now - self.emote_last.get(sender, 0) < 3:
            raise RuleViolation("EMOTE_TOO_FAST", "互动间隔3秒，请稍等再发送")
        self.emote_last[sender] = now
        self.social_sequence += 1
        self.social_events = (self.social_events + [{'id': self.social_sequence, 'from': source,
                                'to': target, 'item': item, 'at': now}])[-16:]

    def action(self, payload):
        with self.lock:
            if isinstance(payload, dict) and payload.get('command') == 'emote':
                if set(payload) != {'command', 'target', 'item', 'table_id'}:
                    raise RuleViolation("INVALID_REQUEST", "操作请求格式不正确")
                if payload['table_id'] != self.table_id:
                    raise RuleViolation("STATE_CHANGED", "牌桌已更新，请按最新状态操作")
                self._social(0, payload['target'], payload['item'], 0)
                self.last_active = time.monotonic()
                return self.snapshot()
            if not isinstance(payload, dict) or set(payload) - {"action", "version", "table_id", "cards", "kind"}:
                raise RuleViolation("INVALID_REQUEST", "操作请求格式不正确")
            if (payload.get("table_id") != self.table_id or type(payload.get("version")) is not int
                    or payload["version"] != self.version):
                raise RuleViolation("STATE_CHANGED", "牌桌已更新，请按最新状态操作")
            action = payload.get("action")
            if not isinstance(action, str):
                raise RuleViolation("INVALID_REQUEST", "请选择操作")
            ids = payload.get("cards", [])
            if not isinstance(ids, list) or len(ids) > 18:
                raise RuleViolation("INVALID_REQUEST", "选牌格式不正确")
            cards = tuple(Card.from_id(c) for c in ids)
            kind = payload.get("kind")
            if kind is not None:
                try:
                    kind = PlayKind(kind)
                except (ValueError, TypeError):
                    raise RuleViolation("INVALID_KIND", "出牌类型不正确") from None
                if action != "play":
                    raise RuleViolation("INVALID_KIND", "此操作不能声明出牌类型")
            if action == "reset":
                self.game = Game(options=self.game.options)
                self.auto = False
                self.fault = None
                self.two_seen = {}
                self.claim_deadline = None
                self.events = ["已重新开桌，请准备。"]
                self.social_events = []
                self.version += 1
            elif action == "auto":
                if self.game.phase == Phase.SETTLED:
                    raise RuleViolation("WRONG_PHASE", "本局已结束，请进入下一局")
                self.auto = not self.auto
                self.version += 1
                self._log("已托管本局。" if self.auto else "已停止托管，请自行操作。")
            else:
                self._perform(0, action, cards, kind)
            self.last_active = time.monotonic()
            return self.snapshot()

    def tick(self, now=None):
        """Accept at most one autonomous action per tick; test with a virtual clock."""
        with self.lock:
            now = time.monotonic() if now is None else now
            if now < self.next_tick or self.fault or self.game.phase == Phase.SETTLED:
                return False
            self.next_tick = now + 0.65
            g = self.game
            actors = range(4) if self.auto else range(1, 4)
            if g.phase == Phase.WAITING:
                if len(g.ready_seats) == 4:
                    self._commit(g.start_deal(self._order()), "开始发牌，拿到2时可以抢亮。")
                    self.next_tick = now + DEAL_INTERVAL
                    return True
                for seat in actors:
                    if seat not in g.ready_seats:
                        self._perform(seat, "ready")
                        return True
            elif g.phase == Phase.DEALING:
                self.next_tick = now + DEAL_INTERVAL
                if g.context is not None:
                    self._commit(self._complete_called_deal(g), "主花色已确定，剩余手牌已发完。")
                    self._auto_reflections()
                    return True
                if g.context is None and g.number > 1:
                    for seat in actors:
                        if any(c.rank == "2" for c in g.hands[seat]):
                            self.two_seen.setdefault(seat, now)
                            if self.auto or now - self.two_seen[seat] >= 1.4:
                                self._perform(seat, "call_trump", suggested_cards(g, seat, "call_trump"))
                                return True
                if g.dealt_count < 48:
                    self._deal_card()
                    if self.game.phase == Phase.DEALING and self.game.dealt_count == 48:
                        self.claim_deadline = now + 3.0
                    return True
                if now >= self.claim_deadline:
                    self._commit(g.finish_dealing(), "发牌结束，确认三五反后进行进贡还贡。")
                    self._auto_reflections()
                    return True
            elif g.phase == Phase.FIRST_NO_TRUMP:
                if g.options.first_no_trump == FirstNoTrump.REDEAL:
                    self._commit(g.redeal(self._order()), "首局无人亮2，重新发牌。")
                    self.two_seen = {}
                    self.claim_deadline = None
                else:
                    self._commit(g.choose_first_dealer(), "首局无人亮2，随机定庄后抽底定主。")
                return True
            elif g.phase == Phase.DRAW_TRUMP:
                seat = next(s for s in actors if s % 2 != g.dealer % 2)
                self._perform(seat, "draw_trump")
                return True
            else:
                for seat in actors:
                    choices = self.available(seat)
                    # Bots also reveal newly completed groups after taking bottom.
                    preferred = ("reveal", "confirm") if g.phase == Phase.DECLARING else (
                        "reveal", "give_tribute", "return_tribute", "take_bottom", "discard", "play")
                    for action in preferred:
                        if action in choices:
                            self._perform(seat, action, suggested_cards(g, seat, action))
                            return True
            return False

    def snapshot(self, seat=0):
        with self.lock:
            g = self.game
            view = g.view_for(seat)
            actions = self.available(seat)
            priority = ("play", "discard", "give_tribute", "return_tribute", "call_trump", "reveal")
            hint_action = next((a for a in priority if a in actions), None)
            hand = [Card.from_id(c) for c in view["hand"]]
            if g.context:
                hand.sort(key=lambda c: (not is_trump(c, g.context),
                                         "" if is_trump(c, g.context) else c.suit.value,
                                         tuple(-x for x in strength(c, g.context))))
            else:
                ranks = ("3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2", "small", "big")
                hand.sort(key=lambda c: (c.suit.value, -ranks.index(c.rank)))
            view.update({
                "version": self.version, "table_id": self.table_id,
                "player_seat": seat, "auto": self.auto, "fault": self.fault,
                "players": [{"seat": s, "name": n, "bot": s != 0} for s, n in enumerate(NAMES)],
                "available_actions": actions, "events": list(self.events),
                "history": [r.to_dict() for r in g.history[-5:]],
                "hand": [c.id for c in hand],
                "hand_details": [{"id": c.id, "trump": bool(g.context and is_trump(c, g.context)),
                                  "reflected": bool(g.context and c in g.context.reflected_cards),
                                  "exchanged": bool(g.context and c in g.context.exchanged_cards),
                                  "points": c.points} for c in hand],
                "hint": {"action": hint_action, "cards": [c.id for c in suggested_cards(g, seat, hint_action)]}
                        if hint_action else None,
                "special_hints": special_hints(g, seat),
                "auto_first_trump": True, "deal_interval": DEAL_INTERVAL,
                "social_sequence": self.social_sequence,
                "social_events": [dict(e) for e in self.social_events if time.time() - e['at'] < 8],
            })
            return view
