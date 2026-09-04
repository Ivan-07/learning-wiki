"""SQLite 派生库连接与 migration。

- 派生库可随时删除重建；连接参数以「可丢弃缓存」为准（WAL、synchronous=NORMAL）。
- migration 为包内顺序 SQL 文件，记录于 schema_migrations 表。
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False：MCP 工具在工作线程执行；
    # 并发安全由 flock（跨进程写锁）+ 单事件循环（进程内串行）保证
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _migration_files() -> list[tuple[int, str]]:
    traversable = resources.files("learning_wiki.storage.db") / "migrations"
    entries: list[tuple[int, str]] = []
    for item in traversable.iterdir():
        name = item.name
        if name.endswith(".sql"):
            entries.append((int(name.split("_", 1)[0]), name))
    return sorted(entries)


def migrate(conn: sqlite3.Connection) -> list[int]:
    """按序应用未执行的 migration，返回本次应用的版本号列表。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    used: list[int] = []
    for version, name in _migration_files():
        if version in applied:
            continue
        sql = (resources.files("learning_wiki.storage.db") / "migrations" / name).read_text(
            encoding="utf-8"
        )
        # 注：executescript 自带事务语义，派生库可丢弃，失败即整体重跑
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        used.append(version)
    return used
