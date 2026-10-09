"""Versioned JSON checkpoints in SQLite. Never deserialize executable objects."""
from dataclasses import fields, is_dataclass
from contextlib import closing
from enum import Enum
import json
from pathlib import Path
import sqlite3
from threading import RLock

from .game import DealResult, Exchange, FirstNoTrump, Game, GameOptions, LateReflection, Phase
from .models import Card, PlayKind, Reflection, RuleContext, Suit
from .rules import Lead
from .settlement import Settlement, Tribute
from .trick import Play, Trick, TrickResult


class StorageError(RuntimeError):
    pass


TYPES = {cls.__name__: cls for cls in (DealResult, Exchange, Game, GameOptions, Card, Reflection,
                                     RuleContext, Lead, Settlement, Tribute, Play, Trick, TrickResult)}
ENUMS = {cls.__name__: cls for cls in (FirstNoTrump, LateReflection, Phase, PlayKind, Suit)}


def encode(value):
    if isinstance(value, Enum):
        if type(value).__name__ not in ENUMS:
            raise StorageError("不支持的枚举类型")
        return {"enum": type(value).__name__, "value": value.value}
    if is_dataclass(value):
        if type(value).__name__ not in TYPES:
            raise StorageError("不支持的牌局对象")
        return {"type": type(value).__name__, "fields": {f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, (tuple, frozenset)):
        sequence = sorted(value, key=repr) if isinstance(value, frozenset) else value
        return {"collection": "tuple" if isinstance(value, tuple) else "frozenset", "items": [encode(v) for v in sequence]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return {k: encode(v) for k, v in value.items()}
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise StorageError("不支持的存档数据")


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "enum" in value:
        return ENUMS[value["enum"]](value["value"])
    if "type" in value:
        cls = TYPES[value["type"]]
        attrs = dict(value["fields"])
        # v1.0 dealt from seat zero and did not store this field.
        if cls is Game and "deal_start_seat" not in attrs:
            attrs["deal_start_seat"] = 0
        if cls is DealResult and "surrender_by" not in attrs:
            attrs["surrender_by"] = None
        if set(attrs) != {f.name for f in fields(cls)}:
            raise StorageError("存档字段与程序版本不匹配")
        result = cls(**{k: decode(v) for k, v in attrs.items()})
        if isinstance(result, Game):
            result.check_invariants()
        return result
    if "collection" in value:
        if value["collection"] not in ("tuple", "frozenset"):
            raise StorageError("存档集合类型无效")
        items = [decode(v) for v in value["items"]]
        return tuple(items) if value["collection"] == "tuple" else frozenset(items)
    return {k: decode(v) for k, v in value.items()}


class SQLiteStore:
    """One process owns a database; checkpoints commit before clients receive ACKs."""
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.owner_file = self.path.with_suffix(self.path.suffix + ".lock")
        self.owner = self.owner_file.open("a+b")
        self.connection = None
        try:
            self.owner.seek(0)
            if self.owner.read(1) == b"":
                self.owner.write(b"0")
                self.owner.flush()
            self.owner.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(self.owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.connection = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
            if __import__("os").name != "nt":
                self.path.chmod(0o600)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("CREATE TABLE IF NOT EXISTS checkpoints (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
            self.connection.commit()
        except (OSError, sqlite3.Error) as exc:
            self.close()
            raise StorageError("无法打开存档，或另一服务正在使用同一个存档") from exc

    def read(self):
        with self.lock:
            try:
                row = self.connection.execute("SELECT payload FROM checkpoints WHERE id=1").fetchone()
                if row is None:
                    return None
                data = json.loads(row[0])
                if data.get("schema") != 1:
                    raise StorageError("不支持此存档版本")
                return decode(data["state"])
            except (ValueError, KeyError, TypeError, sqlite3.Error, RecursionError) as exc:
                raise StorageError("存档无效，请保留原文件并检查备份；服务未新建空牌桌") from exc

    def write(self, state):
        with self.lock:
            try:
                payload = json.dumps({"schema": 1, "state": encode(state)}, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                with self.connection:
                    self.connection.execute("INSERT INTO checkpoints(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (payload,))
            except (ValueError, sqlite3.Error) as exc:
                raise StorageError("牌局保存失败，本次操作未提交，请稍后重试") from exc

    def backup(self, destination):
        """Consistent backup including committed WAL transactions."""
        destination = Path(destination).resolve()
        if destination == self.path:
            raise StorageError("备份文件不能覆盖当前存档")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, closing(sqlite3.connect(destination)) as target:
            self.connection.backup(target)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.owner is not None:
            self.owner.close()
            self.owner = None
