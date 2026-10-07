"""Online SQLite backup; does not acquire the service's exclusive owner lock."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3

from .storage import StorageError


def backup_database(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file() or source == destination:
        raise StorageError("源存档不存在，或备份路径与源存档相同")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise StorageError("备份校验失败，请保留原存档")
    except (OSError, sqlite3.Error) as exc:
        raise StorageError("备份失败，请检查路径；不会覆盖已有备份") from exc
    return destination


def main():
    parser = argparse.ArgumentParser(description="备份正在运行的三五反好友房存档")
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    try:
        print(f"已备份：{backup_database(args.source, args.destination)}")
    except StorageError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
