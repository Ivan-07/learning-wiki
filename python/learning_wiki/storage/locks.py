"""修改型操作的进程间互斥（CLI 与 Claudian 内 Agent 可能并发）。

- 锁文件位于 .learning-wiki/locks/，fcntl.flock 独占；
- 单用户场景，锁粒度：每 Vault 一把写锁；
- 读操作不加锁。
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def vault_write_lock(dot_dir: Path, name: str = "write.lock"):
    lock_dir = dot_dir / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / name, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
