"""SQLite migration 幂等与 SafeFileWriter 崩溃注入测试。"""

import subprocess
import sys

from learning_wiki.adapters.executor.local_fsync import SafeFileWriter, crash_point
from learning_wiki.storage.db import connection as db_connection


def test_migrations_idempotent(tmp_path) -> None:
    conn = db_connection.connect(tmp_path / "knowledge.db")
    assert db_connection.migrate(conn) == [1, 2, 3]
    assert db_connection.migrate(conn) == []  # 幂等
    count = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    assert count >= 20
    conn.close()


def test_atomic_write_and_dir_fsync(tmp_path) -> None:
    writer = SafeFileWriter(tmp_path)
    writer.write_text(tmp_path / "a" / "b.md", "内容")
    assert (tmp_path / "a" / "b.md").read_text(encoding="utf-8") == "内容"
    # 不留临时文件
    assert not list((tmp_path / "a").glob(".lw-tmp-*"))


def test_crash_point_injection(tmp_path, monkeypatch) -> None:
    """LW_TEST_CRASH_POINT 命中时进程以 137 退出（模拟强杀）。"""
    monkeypatch.setenv("LW_TEST_CRASH_POINT", "write:before_replace")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from learning_wiki.adapters.executor.local_fsync import crash_point;"
            "crash_point('write:before_replace')",
        ],
        capture_output=True,
    )
    assert proc.returncode == 137


def test_crash_point_noop_without_env(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LW_TEST_CRASH_POINT", raising=False)
    crash_point("write:before_replace")  # 不抛出
