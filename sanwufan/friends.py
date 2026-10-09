"""Four human players; identities and private hands stay on the server."""
import secrets
import hashlib
import random
import time
import unicodedata
from copy import deepcopy
from threading import RLock

from .game import Game, Phase, REFLECTION_PHASES
from .models import Card, PlayKind, RuleViolation, validate_seat
from .practice import DEAL_INTERVAL, PracticeRoom
from .storage import StorageError
from .table_session import SESSION_FIELDS


class FriendRoom(PracticeRoom):
    def __init__(self, code, owner, name, seed=None):
        super().__init__(seed)
        self.code = code
        self.owner = owner
        self.members = [owner, None, None, None]
        self.names = [name, "空座", "空座", "空座"]
        self.seen = {owner: time.monotonic()}
        self.events = [f"{name}创建了好友房，邀请三位朋友入座。"]
        self.recovering = False
        self.blocked = frozenset()
        self.social_sequence = 0
        self.social_events = []
        self.emote_last = {}
        self.chat_sequence = 0
        self.chat_messages = []
        self.chat_last = {}

    def capture(self):
        with self.lock:
            return {"game": self.game, "table_id": self.table_id, "version": self.version,
                    "events": list(self.events), "code": self.code, "owner": self.owner,
                    "members": list(self.members), "names": list(self.names), "seen": dict(self.seen),
                    "last_active": self.last_active, "claim_deadline": self.claim_deadline,
                    "next_tick": self.next_tick, "fault": self.fault, "recovering": self.recovering,
                    "rng_state": self.rng.getstate() if self.rng else None,
                    "blocked": self.blocked, "social_sequence": self.social_sequence,
                    "social_events": list(self.social_events), "emote_last": dict(self.emote_last),
                    "chat_sequence": self.chat_sequence, "chat_messages": list(self.chat_messages),
                    "chat_last": dict(self.chat_last),
                    **{key: deepcopy(getattr(self, key)) for key in SESSION_FIELDS}}

    def restore(self, data):
        with self.lock:
            for key, value in data.items():
                if key != "rng_state":
                    setattr(self, key, value)
            if data["rng_state"] is not None:
                self.rng = random.Random()
                self.rng.setstate(data["rng_state"])
            else:
                self.rng = None
            if 'match_ledger' not in data:
                self.match_ledger = [r.to_dict() for r in self.game.history]
                if self.game.result is not None:
                    self.match_ledger.append(self.game.result.to_dict())
            self._sync_turn_clock()

    def seat_for(self, token):
        if token not in self.members:
            raise RuleViolation("NOT_SEATED", "你还没有加入这个房间")
        return self.members.index(token)

    def occupant_id(self, seat):
        token = self.members[seat]
        return hashlib.sha256((self.table_id + token).encode()).hexdigest()[:16] if token else None

    def join(self, token, name):
        with self.lock:
            if token in self.blocked:
                raise RuleViolation("REMOVED_FROM_ROOM", "房主已请你离开这桌；房主重新开桌后才能再次加入")
            if token in self.members:
                return self.seat_for(token)
            if self.game.phase != Phase.WAITING or self.game.number:
                raise RuleViolation("MATCH_STARTED", "这桌已经开局，请等待房主重新开桌")
            if None not in self.members:
                raise RuleViolation("ROOM_FULL", "四个座位都已有人")
            seat = self.members.index(None)
            self.members[seat] = token
            self.names[seat] = name
            self.seen[token] = time.monotonic()
            self.version += 1
            self._log(f"{name}加入了房间。")
            return seat

    def leave(self, token, removed=False):
        with self.lock:
            seat = self.seat_for(token)
            if self.game.phase not in (Phase.WAITING, Phase.SETTLED):
                raise RuleViolation("MATCH_IN_PROGRESS", "本局进行中，请打完后离房；关闭页面会保留座位")
            # A replacement player starts a fresh match, without inheriting tribute.
            self.game = Game(options=self.game.options)
            self._reset_session(keep_reports=False)
            self._log(f"{self.names[seat]}{'被房主请离' if removed else '离开了房间'}，重新等待准备。")
            self.members[seat] = None
            self.names[seat] = "空座"
            self.seen.pop(token, None)
            if token == self.owner:
                self.owner = next((m for m in self.members if m), None)
            self.version += 1

    def social(self, token, target, item):
        with self.lock:
            source = self.seat_for(token)
            validate_seat(target)
            if target == source or self.members[target] is None:
                raise RuleViolation("INVALID_TARGET", "请选择另一位已入座的玩家")
            self._social(source, target, item, token)

    def chat(self, token, text):
        with self.lock:
            seat = self.seat_for(token)
            if (not isinstance(text, str) or not 1 <= len(text) <= 200
                    or any(unicodedata.category(c) == 'Cc' and not c.isspace() for c in text)):
                raise RuleViolation("INVALID_CHAT", "聊天消息须为1至200字的文字")
            text = ' '.join(text.split())
            if not text:
                raise RuleViolation("INVALID_CHAT", "请输入聊天内容")
            now = time.time()
            if now - self.chat_last.get(token, 0) < 1:
                raise RuleViolation("CHAT_TOO_FAST", "发送太快，请稍等一秒")
            self.chat_last[token] = now
            self.chat_sequence += 1
            self.chat_messages = (self.chat_messages + [{'id': self.chat_sequence, 'seat': seat,
                                   'name': self.names[seat], 'text': text, 'at': now}])[-80:]

    def action_for(self, token, payload):
        with self.lock:
            seat = self.seat_for(token)
            if not isinstance(payload, dict) or set(payload) - {"action", "version", "table_id", "cards", "kind"}:
                raise RuleViolation("INVALID_REQUEST", "操作请求格式不正确")
            # A previously owned selection remains
            # usable, but the engine still arbitrates the first claim and deadline.
            version = payload.get("version")
            live_declaration = (type(version) is int and version < self.version
                                and ((self.game.phase == Phase.DEALING
                                      and payload.get("action") in ("call_trump", "reveal"))
                                     or (self.game.phase in REFLECTION_PHASES
                                         and payload.get("action") == "reveal")))
            if (payload.get("table_id") != self.table_id or type(version) is not int
                    or (version != self.version and not live_declaration)):
                raise RuleViolation("STATE_CHANGED", "牌桌已更新，请按最新状态操作")
            action = payload.get("action")
            ids = payload.get("cards", [])
            if not isinstance(action, str) or not isinstance(ids, list) or len(ids) > 18:
                raise RuleViolation("INVALID_REQUEST", "操作请求格式不正确")
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
                if token != self.owner:
                    raise RuleViolation("OWNER_ONLY", "只有房主可以重新开桌")
                if self.game.phase not in (Phase.WAITING, Phase.SETTLED) and not self.fault:
                    raise RuleViolation("MATCH_IN_PROGRESS", "请在本局结束后重新开桌")
                self.game = Game(options=self.game.options)
                self._reset_session()
                self.fault = None
                self.claim_deadline = None
                self.two_seen = {}
                self.blocked = frozenset()
                self.version += 1
                self._log("房主重新开桌，请四人准备。")
            elif action == "unready":
                self._commit(self.game.ready(seat, False), f"{self.names[seat]}取消了准备。")
            elif action == "change_seat":
                if self.game.phase != Phase.WAITING or self.game.number or self.game.ready_seats:
                    raise RuleViolation("SEATS_LOCKED", "请全部取消准备后换座；开局后座位固定")
                # Move to the next empty seat. Team partners sit opposite one another.
                target = next((s for s in ((seat + i) % 4 for i in range(1, 4))
                               if self.members[s] is None), None)
                if target is None:
                    raise RuleViolation("ROOM_FULL", "没有空座可换")
                self.members[target], self.members[seat] = token, None
                self.names[target], self.names[seat] = self.names[seat], "空座"
                self.version += 1
                self._log(f"{self.names[target]}换到了{target + 1}号座位。")
            else:
                if action not in self.available(seat):
                    raise RuleViolation("ACTION_UNAVAILABLE", "当前不能进行此操作")
                if action == 'play':
                    self._check_play_deadline()
                self._perform(seat, action, cards, kind)
            return self.snapshot_for(token)

    def tick(self, now=None):
        with self.lock:
            now = time.monotonic() if now is None else now
            if self._timeout_turn():
                return True
            if self.fault or now < self.next_tick:
                return False
            if self.recovering:
                if not all(m and now - self.seen.get(m, 0) < 15 for m in self.members):
                    return False
                self.recovering = False
                if self.game.phase == Phase.DEALING and self.game.dealt_count == 48:
                    self.claim_deadline = now + 5
            self.next_tick = now + DEAL_INTERVAL
            g = self.game
            if g.phase == Phase.WAITING and None not in self.members and len(g.ready_seats) == 4:
                self._commit(g.start_deal(self._order()), "四人已准备，开始发牌。首局最先收到的2自动亮主，后续局手动亮2。")
                return True
            if g.phase == Phase.DEALING:
                if g.context is not None:
                    self._commit(self._complete_called_deal(g), "主花色已确定，剩余手牌已发完。")
                    self._auto_reflections()
                    return True
                if g.dealt_count < 48:
                    self._deal_card()
                    if self.game.phase == Phase.DEALING and self.game.dealt_count == 48:
                        self.claim_deadline = now + 5.0
                    return True
                if now >= self.claim_deadline:
                    self._commit(g.finish_dealing(), "发牌结束，请四人确认三五反。")
                    self._auto_reflections()
                    return True
            if g.phase == Phase.FIRST_NO_TRUMP:
                self._commit(g.redeal(self._order()), "首局无人亮2，重新发牌。")
                self.claim_deadline = None
                return True
            return False

    def available(self, seat=0):
        actions = super().available(seat)
        if self.game.phase == Phase.WAITING and seat in self.game.ready_seats:
            actions.append("unready")
        if (self.game.phase == Phase.WAITING and not self.game.number
                and not self.game.ready_seats and None in self.members):
            actions.append("change_seat")
        return actions

    def snapshot_for(self, token):
        with self.lock:
            seat = self.seat_for(token)
            now = time.monotonic()
            self.seen[token] = now
            self.last_active = now
            view = super().snapshot(seat)
            view.update(mode="friends", room_code=self.code, is_owner=token == self.owner, recovering=self.recovering,
                        can_kick=token == self.owner and self.game.phase in (Phase.WAITING, Phase.SETTLED),
                        social_sequence=self.social_sequence,
                        chat_sequence=self.chat_sequence,
                        chat_messages=[dict(m) for m in self.chat_messages],
                        social_events=[dict(e) for e in self.social_events if time.time() - e['at'] < 8],
                        players=[{"seat": s, "name": self.names[s], "bot": False, "occupant_id": self.occupant_id(s),
                                  "occupied": m is not None, "owner": m == self.owner and m is not None,
                                  "online": m is not None and now - self.seen.get(m, 0) < 15}
                                 for s, m in enumerate(self.members)])
            return view


class FriendRooms:
    """Registry lock precedes each room lock. Never take them in reverse order."""
    def __init__(self, seed=None, store=None):
        self.lock = RLock()
        self.seed = seed
        self.sessions = {}
        self.rooms = {}
        self.store = store
        self.last_saved = 0
        self.closed = False
        if store:
            data = store.read()
            if data is not None:
                self._load(data)

    def _capture(self):
        return ({t: dict(s) for t, s in self.sessions.items()},
                [(code, room, room.capture()) for code, room in self.rooms.items()])

    def _rollback(self, captured):
        self.sessions, previous = captured
        self.rooms = {}
        for code, room, data in previous:
            room.restore(data)
            self.rooms[code] = room

    def _save(self):
        if not self.store:
            return
        now, wall = time.monotonic(), time.time()
        sessions = {t: {**s, "seen": wall - max(0, now - s["seen"])} for t, s in self.sessions.items()}
        rooms = []
        for room in self.rooms.values():
            data = room.capture()
            data["last_active"] = wall - max(0, now - data["last_active"])
            data["claim_deadline"] = max(0, data["claim_deadline"] - now) if data["claim_deadline"] is not None else None
            data["next_tick"] = 0
            data["seen"] = {}
            rooms.append(data)
        self.store.write({"sessions": sessions, "rooms": rooms})
        self.last_saved = now

    def _load(self, state):
        now, wall = time.monotonic(), time.time()
        try:
            for token, session in state["sessions"].items():
                if not isinstance(token, str) or not isinstance(session["name"], str):
                    raise ValueError
                self.sessions[token] = {**session, "seen": now - max(0, wall - session["seen"])}
            for data in state["rooms"]:
                members = data["members"]
                if (len(members) != 4 or len(data["names"]) != 4 or data["owner"] not in members
                        or len({m for m in members if m}) != len([m for m in members if m])
                        or any(m and m not in self.sessions for m in members)
                        or not isinstance(data["game"], Game)
                        or type(data["version"]) is not int or data["version"] < 0):
                    raise ValueError
                data["game"].check_invariants()
                active = now - max(0, wall - data["last_active"])
                if now - active >= 7200:
                    continue
                room = FriendRoom(data["code"], data["owner"], data["names"][members.index(data["owner"])])
                data["last_active"] = active
                data["next_tick"] = now
                data["seen"] = {}
                data["recovering"] = data["game"].phase in (Phase.WAITING, Phase.DEALING) and None not in members
                data["claim_deadline"] = now + max(5, data["claim_deadline"] or 0) if data["game"].phase == Phase.DEALING and data["game"].dealt_count == 48 else None
                room.restore(data)
                room._log("服务已恢复，房间和手牌已保留，请用原浏览器继续。")
                self.rooms[room.code] = room
            for session in self.sessions.values():
                if session["room"] not in self.rooms:
                    session["room"] = None
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise StorageError("存档房间或身份信息无效，未覆盖原存档") from exc

    def _run(self, operation, *args, force=False):
        with self.lock:
            if self.closed:
                raise StorageError("服务正在关闭，请稍后重试")
            if not self.store:
                return operation(*args)
            before = self._capture()
            try:
                result = operation(*args)
                membership_changed = set(before[0]) != set(self.sessions) or {c for c, _, _ in before[1]} != set(self.rooms)
                if (force and result is not False) or membership_changed or time.monotonic() - self.last_saved >= 30:
                    self._save()
                return result
            except StorageError:
                self._rollback(before)
                raise

    def session(self, token):
        with self.lock:
            needs_key = token in self.sessions and not self.sessions[token].get("recovery_code")
            return self._run(self._session, token, force=needs_key)

    def state(self, token):
        return self._run(self._state, token)

    def request(self, token, payload):
        return self._run(self._request, token, payload, force=True)

    def tick(self, now=None):
        with self.lock:
            for room in list(self.rooms.values()):
                if time.monotonic() - room.last_active <= 7200:
                    self._run(room.tick, now, force=True)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.store:
                try:
                    self._save()
                finally:
                    self.store.close()

    def _session(self, token):
        with self.lock:
            now = time.monotonic()
            stale = [code for code, room in self.rooms.items() if now - room.last_active > 7200]
            for code in stale:
                del self.rooms[code]
            self.sessions = {t: s for t, s in self.sessions.items()
                             if now - s["seen"] < 7200 or s["room"] in self.rooms}
            created = token not in self.sessions
            if created:
                if len(self.sessions) >= 512:
                    raise RuleViolation("SERVER_FULL", "当前玩家已满，请稍后再试")
                token = secrets.token_urlsafe(32)
                self.sessions[token] = {"name": "", "room": None, "seen": now,
                                        "recovery_code": secrets.token_urlsafe(18)}
            session = self.sessions[token]
            session.setdefault("recovery_code", secrets.token_urlsafe(18))
            session["seen"] = now
            if session["room"] not in self.rooms:
                session["room"] = None
            return token, session, created

    def _state(self, token):
        with self.lock:
            session = self.sessions.get(token)
            if session is None:
                return {"mode": "lobby", "name": ""}
            if session["room"] not in self.rooms:
                session["room"] = None
                return {"mode": "lobby", "name": session["name"], "recovery_code": session.get("recovery_code", ""),
                        "notice": session.get("notice", "")}
            view = self.rooms[session["room"]].snapshot_for(token)
            view["recovery_code"] = session.get("recovery_code", "")
            return view

    def _request(self, token, payload):
        with self.lock:
            if token not in self.sessions:
                raise RuleViolation("SESSION_EXPIRED", "请刷新页面，重新连接")
            session = self.sessions[token]
            session["seen"] = time.monotonic()
            if session["room"] not in self.rooms:
                session["room"] = None
            if not isinstance(payload, dict):
                raise RuleViolation("INVALID_REQUEST", "操作请求格式不正确")
            command = payload.get("command")
            if command is None:
                if session["room"] not in self.rooms:
                    raise RuleViolation("NOT_SEATED", "请先加入房间")
                return self.rooms[session["room"]].action_for(token, payload)
            if command == "recover":
                if set(payload) != {"command", "recovery_code"}:
                    raise RuleViolation("INVALID_REQUEST", "恢复请求格式不正确")
                if session["room"] is not None:
                    raise RuleViolation("ALREADY_SEATED", "请先离开当前房间，再恢复之前的座位")
                key = payload["recovery_code"]
                if not isinstance(key, str) or len(key) != 24 or not key.isascii():
                    raise RuleViolation("INVALID_RECOVERY", "恢复码无效，请核对后重试")
                previous = next((t for t, s in self.sessions.items()
                                 if secrets.compare_digest(s.get("recovery_code", ""), key)), None)
                old = self.sessions.get(previous)
                if previous == token or old is None or old["room"] not in self.rooms:
                    raise RuleViolation("INVALID_RECOVERY", "恢复码无效，或之前的房间已关闭")
                room = self.rooms[old["room"]]
                with room.lock:
                    seat = room.seat_for(previous)
                    room.members[seat] = token
                    if room.owner == previous:
                        room.owner = token
                    room.seen.pop(previous, None)
                    room.seen[token] = time.monotonic()
                    room.version += 1
                    room._log(f"{room.names[seat]}恢复了原来的座位。")
                session.update(name=old["name"], room=old["room"], recovery_code=secrets.token_urlsafe(18))
                del self.sessions[previous]
                return self._state(token)
            if command == 'chat':
                if set(payload) != {'command', 'text', 'room_code'}:
                    raise RuleViolation('INVALID_REQUEST', '聊天请求格式不正确')
                if session['room'] not in self.rooms:
                    raise RuleViolation('NOT_SEATED', '请先加入房间')
                room = self.rooms[session['room']]
                if payload['room_code'] != room.code:
                    raise RuleViolation('STATE_CHANGED', '房间已变更，请重新发送')
                room.chat(token, payload['text'])
                return self._state(token)
            if command == 'table_vote':
                if session['room'] not in self.rooms:
                    raise RuleViolation('NOT_SEATED', '请先加入房间')
                room = self.rooms[session['room']]
                room.table_vote(room.seat_for(token), payload)
                return self._state(token)
            if command in ('kick', 'emote'):
                expected = ({'command', 'target', 'target_id', 'version', 'table_id'} if command == 'kick' else
                            {'command', 'target', 'target_id', 'item', 'table_id'})
                if set(payload) != expected:
                    raise RuleViolation('INVALID_REQUEST', '房间操作格式不正确')
                if session['room'] not in self.rooms:
                    raise RuleViolation('NOT_SEATED', '请先加入房间')
                room = self.rooms[session['room']]
                if payload['table_id'] != room.table_id:
                    raise RuleViolation('STATE_CHANGED', '房间已更新，请刷新后操作')
                target = payload['target']
                validate_seat(target)
                if payload['target_id'] != room.occupant_id(target):
                    raise RuleViolation('STATE_CHANGED', '这个座位的玩家已变更，请重新选择')
                if command == 'emote':
                    if not isinstance(payload['item'], str):
                        raise RuleViolation('INVALID_EMOTE', '不支持这种互动')
                    room.social(token, target, payload['item'])
                else:
                    if room.owner != token:
                        raise RuleViolation('OWNER_ONLY', '只有房主可以请离玩家')
                    if type(payload['version']) is not int or payload['version'] != room.version:
                        raise RuleViolation('STATE_CHANGED', '房间已更新，请按最新状态操作')
                    other = room.members[target]
                    if other is None or other == token:
                        raise RuleViolation('INVALID_TARGET', '请选择另一位已入座的玩家')
                    room.leave(other, removed=True)
                    room.blocked = room.blocked | {other}
                    self.sessions[other].update(room=None, notice='房主已请你离开房间。你可以创建其他房间。',
                                                recovery_code=secrets.token_urlsafe(18))
                return self._state(token)
            if set(payload) - {"command", "name", "room_code"}:
                raise RuleViolation("INVALID_REQUEST", "操作请求格式不正确")
            if command == "leave":
                if session["room"] in self.rooms:
                    room = self.rooms[session["room"]]
                    room.leave(token)
                    if not any(room.members):
                        del self.rooms[session["room"]]
                session["room"] = None
                return self._state(token)
            if command not in ("create", "join"):
                raise RuleViolation("UNKNOWN_ACTION", "不支持此操作")
            if session["room"] is not None:
                raise RuleViolation("ALREADY_SEATED", "请先离开当前房间")
            name = payload.get("name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 16 or any(ord(c) < 32 for c in name):
                raise RuleViolation("INVALID_NAME", "昵称需为1到16个字符")
            name = name.strip()
            if command == "create":
                if len(self.rooms) >= 64:
                    raise RuleViolation("SERVER_FULL", "好友房已满，请稍后再试")
                code = str(secrets.randbelow(900000) + 100000)
                while code in self.rooms:
                    code = str(secrets.randbelow(900000) + 100000)
                self.rooms[code] = FriendRoom(code, token, name, self.seed)
            else:
                code = payload.get("room_code")
                if not isinstance(code, str) or code not in self.rooms:
                    raise RuleViolation("ROOM_NOT_FOUND", "房间不存在或已关闭，请核对房间号")
                self.rooms[code].join(token, name)
            session.update(name=name, room=code, notice='')
            return self._state(token)
