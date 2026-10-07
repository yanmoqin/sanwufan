"""Shared single-process game service with a checkpointed friend-room ticker."""
import logging
import secrets
from threading import Event, RLock, Thread
import time

from .friends import FriendRooms
from .models import RuleViolation
from .practice import PracticeRoom
from .storage import SQLiteStore, StorageError


class GameService:
    def __init__(self, address=("127.0.0.1", 8765), seed=None, allowed_hosts=(), database=None, public_origin=None):
        self.server_address = address
        self.seed = seed
        self.rooms = {}
        self.room_lock = RLock()
        self.logger = logging.getLogger("sanwufan")
        self.public_origin = public_origin
        self.allowed_hosts = {"127.0.0.1", "localhost", *allowed_hosts}
        if address[0] != "0.0.0.0":
            self.allowed_hosts.add(address[0])
        store = SQLiteStore(database) if database else None
        try:
            self.friends = FriendRooms(seed, store)
        except Exception:
            if store:
                store.close()
            raise
        self.stop_ticks = Event()
        self.closing = False
        self.ticker = Thread(target=self._tick_rooms, daemon=True)
        self.ticker.start()

    def room(self, token, create=False):
        with self.room_lock:
            room = self.rooms.get(token)
            if room:
                room.last_active = time.monotonic()
                return token, room, False
            if not create:
                return None, None, False
            now = time.monotonic()
            self.rooms = {t: r for t, r in self.rooms.items() if now - r.last_active < 7200}
            if len(self.rooms) >= 64:
                raise RuleViolation("SERVER_FULL", "练习桌已满，请稍后再试")
            token = secrets.token_urlsafe(32)
            room = PracticeRoom(self.seed)
            self.rooms[token] = room
            return token, room, True

    def _tick_rooms(self):
        last_error = 0
        while not self.stop_ticks.wait(0.05):
            with self.room_lock:
                rooms = list(self.rooms.values())
            for room in rooms:
                if time.monotonic() - room.last_active > 7200:
                    continue
                try:
                    room.tick()
                except Exception:
                    self.logger.exception("practice_tick_failed")
                    with room.lock:
                        room.fault = "牌桌暂停，请重新开桌。"
                        room.version += 1
            try:
                self.friends.tick()
            except StorageError:
                if time.monotonic() - last_error > 10:
                    self.logger.exception("friend_checkpoint_failed")
                    last_error = time.monotonic()
            except Exception:
                self.logger.exception("friend_tick_failed")
                # Preserve the latest valid checkpoint. Avoid an automatic redeal.
                with self.friends.lock:
                    for room in self.friends.rooms.values():
                        if not room.fault:
                            room.fault = "牌桌暂停，房主可以重新开桌。"
                            room.version += 1
                    try:
                        self.friends._save()
                    except StorageError:
                        self.logger.exception("friend_fault_checkpoint_failed")

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.stop_ticks.set()
        self.ticker.join(timeout=5)
        self.friends.close()
