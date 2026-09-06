"""接入层分发器（降级链编排）。

把「平台专用适配器 → 通用网页提取 → 受限降级」串成一条链，对外暴露
统一的 ``extract(item, progress) -> ExtractionResult``。

降级顺序：
1. 平台专用适配器（bili/douyin/xhs/wechat）；
2. 通用 trafilatura 网页提取（兜底）；
3. 受限来源（登录墙/反爬/正文过短）→ 抛 CaptureBlockedError，由
   CaptureService 记入 Inbox 并引导用户改用粘贴文字/文件。

**例外**：平台适配器抛 ``PermanentCaptureError`` 时不降级，直接上抛。
这类错误表示"认得来源但缺凭证"（如小红书链接缺 xsec_token），
交给 trafilatura 只会拿到 JS 空壳，还会把可行动的精确原因
（"请复制完整分享链接"）替换成无信息量的"无法提取正文"。

所有网络访问经 GuardedFetcher 的 SSRF 防护，不在本层重写安全逻辑。
"""

from __future__ import annotations

from learning_wiki.adapters.capture.web import GenericWebAdapter
from learning_wiki.adapters.ingestion.bili import BiliAdapter
from learning_wiki.adapters.ingestion.registry import AdapterRegistry
from learning_wiki.adapters.ingestion.router import Platform, UrlRouter
from learning_wiki.adapters.ingestion.wechat import WechatAdapter
from learning_wiki.adapters.ingestion.xhs import XhsAdapter
from learning_wiki.adapters.net_guard import (
    CaptureBlockedError,
    GuardedFetcher,
    PermanentCaptureError,
)
from learning_wiki.domain.contracts import ExtractionResult, InboxItem


class IngestionDispatcher:
    """URL 输入的接入层分发：路由 + 平台适配器 + 降级链。"""

    def __init__(self, fetcher: GuardedFetcher | None = None) -> None:
        self.fetcher = fetcher or GuardedFetcher()
        self.router = UrlRouter()
        self.registry = AdapterRegistry()
        # 已接入的平台适配器（后续 douyin 在此追加）
        self.registry.register(Platform.BILI.value, BiliAdapter(fetcher=self.fetcher))
        self.registry.register(Platform.XHS.value, XhsAdapter(fetcher=self.fetcher))
        self.registry.register(Platform.WECHAT.value, WechatAdapter(fetcher=self.fetcher))
        # 通用网页兜底
        self._generic = GenericWebAdapter(fetcher=self.fetcher)

    def extract(self, item: InboxItem, progress=None) -> ExtractionResult:
        url = (item.payload or "").strip()
        platform = self.router.route(url).platform

        # 1. 平台专用适配器
        if platform is not Platform.GENERIC:
            adapter = self.registry.get(platform.value)
            if adapter is not None:
                try:
                    return adapter.extract(item, progress)
                except PermanentCaptureError:
                    # 凭证类失败：通用兜底救不了，原样上抛可行动的原因
                    raise
                except CaptureBlockedError:
                    # 平台专用失败 → 落入通用网页兜底（不立即放弃）
                    pass
                except Exception:
                    # 平台解析异常不阻断，走通用兜底
                    pass

        # 2. 通用网页提取（兜底；trafilatura 能抓就抓）
        return self._generic.extract(item, progress)
