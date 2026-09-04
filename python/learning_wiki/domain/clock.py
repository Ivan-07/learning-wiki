"""时钟抽象：所有时间经注入获取，落盘一律 ISO 8601 带偏移。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


class SystemClock:
    def __init__(self, timezone: str = "Asia/Shanghai") -> None:
        self.timezone = timezone

    def now(self) -> datetime:
        return datetime.now(tz=ZoneInfo(self.timezone))

    def now_iso(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def today_iso(self) -> str:
        return self.now().date().isoformat()


class FrozenClock(SystemClock):
    """测试用：时间冻结，可手动推进。"""

    def __init__(self, fixed: datetime, timezone: str = "Asia/Shanghai") -> None:
        super().__init__(timezone)
        if fixed.tzinfo is None:
            fixed = fixed.replace(tzinfo=ZoneInfo(timezone))
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed

    def advance(self, **kwargs: int) -> None:
        from datetime import timedelta

        self._fixed = self._fixed + timedelta(**kwargs)
