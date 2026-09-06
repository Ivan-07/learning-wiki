"""资料接入层（ingestion）。

把异构平台的分享链接（抖音/小红书/B站/公众号等）统一路由到平台专用
适配器，归一为 ``ExtractionResult``，进入既有捕获闭环。

分层职责：
- ``router``：短链展开 + 域名/路径 → 平台类型识别（纯函数，无 IO 副作用）；
- ``registry``：平台适配器注册表（可插拔，新平台只需注册）；
- 各平台 adapter：实现 ``domain.ports.CaptureAdapter``，复用
  ``net_guard.GuardedFetcher`` 的 SSRF 防护。
"""

from learning_wiki.adapters.ingestion.registry import AdapterRegistry
from learning_wiki.adapters.ingestion.router import Platform, UrlRouter, identify_platform

__all__ = ["AdapterRegistry", "Platform", "UrlRouter", "identify_platform"]
