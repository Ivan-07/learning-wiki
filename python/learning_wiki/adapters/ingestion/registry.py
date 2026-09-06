"""平台适配器注册表（可插拔）。

新平台接入只需：实现 ``domain.ports.CaptureAdapter``，然后在这里注册。
"""

from __future__ import annotations

from learning_wiki.domain.contracts import ExtractionResult, InboxItem
from learning_wiki.domain.ports import CaptureAdapter


class AdapterRegistry:
    """按 platform 字符串映射到适配器实例。"""

    def __init__(self) -> None:
        self._adapters: dict[str, CaptureAdapter] = {}

    def register(self, platform: str, adapter: CaptureAdapter) -> None:
        self._adapters[platform] = adapter

    def get(self, platform: str) -> CaptureAdapter | None:
        return self._adapters.get(platform)

    def platforms(self) -> list[str]:
        return sorted(self._adapters)

    def extract(self, platform: str, item: InboxItem, progress=None) -> ExtractionResult | None:
        """调用对应适配器；无适配器或不适配时返回 None。"""
        adapter = self._adapters.get(platform)
        if adapter is None:
            return None
        if not adapter.can_handle(item):
            return None
        return adapter.extract(item, progress)
