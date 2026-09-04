"""SafeFileWriter：所有持久写入复用的原子写 + fsync 顺序保证。

顺序不变量：文件替换持久化 **先于** 任何依赖它的状态标记持久化
（例如 staging manifest 的 done 标志）。因此任意崩溃点下重跑幂等、状态可判定。

崩溃注入：环境变量 ``LW_TEST_CRASH_POINT`` 等于某个 crash tag 时，
在该点 ``os._exit(137)``，用于故障注入测试（规格 13.3.4）。
"""

from __future__ import annotations

import os
from pathlib import Path

from learning_wiki.domain import ids


def crash_point(tag: str) -> None:
    """测试钩子：命中注入点则模拟进程被强杀。生产环境环境变量未设置时为 no-op。"""
    if os.environ.get("LW_TEST_CRASH_POINT") == tag:
        os._exit(137)


class SafeFileWriter:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _tmp_path(self, target: Path) -> Path:
        return target.parent / f".lw-tmp-{ids.new_ulid().lower()}"

    def _fsync_dir(self, directory: Path) -> None:
        fd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def write_bytes(
        self,
        target: Path,
        data: bytes,
        *,
        crash_before_replace: str | None = None,
        crash_after_replace: str | None = None,
    ) -> None:
        """temp 文件 + fsync + os.replace + 目录 fsync。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._tmp_path(target)
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if crash_before_replace:
            crash_point(crash_before_replace)
        os.replace(tmp, target)
        self._fsync_dir(target.parent)
        if crash_after_replace:
            crash_point(crash_after_replace)

    def write_text(self, target: Path, text: str, **kwargs: object) -> None:
        self.write_bytes(target, text.encode("utf-8"), **kwargs)  # type: ignore[arg-type]

    def append_line(self, target: Path, line: str) -> None:
        """追加型写入（JSONL 事件流）：append + flush + fsync。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = line if line.endswith("\n") else line + "\n"
        with open(target, "ab") as f:
            f.write(payload.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())

    def delete(self, target: Path) -> None:
        """删除文件（move_to_archive 第二步）；目录 fsync 保证持久。"""
        if target.exists():
            target.unlink()
            self._fsync_dir(target.parent)
