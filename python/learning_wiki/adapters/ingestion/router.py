"""URL 路由：短链展开 + 域名 → 平台识别。

纯函数、无网络 IO：识别只依据 URL 字符串本身（域名 / 路径特征）。
短链展开（跟随重定向拿真实域名）属于网络操作，由平台适配器或
GuardedFetcher 完成；本模块只负责「给定一个 URL，判断它属于哪个平台」。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import httpx


class Platform(StrEnum):
    """已接入的平台。generic 表示无法识别、交给通用网页适配器。"""

    DOUYIN = "douyin"
    XHS = "xhs"
    BILI = "bili"
    WECHAT = "wechat"
    GENERIC = "generic"


# 域名特征表：命中即判定平台。顺序即优先级（更具体的放前面）。
# 值：域名后缀（无子域名前缀）+ 可选路径前缀。
_PLATFORM_HOSTS: list[tuple[Platform, tuple[str, ...]]] = [
    (Platform.BILI, ("bilibili.com", "b23.tv", "bili2233.cn", "acg.tv")),
    (Platform.DOUYIN, ("douyin.com", "iesdouyin.com", "douyinvod.com")),
    (Platform.XHS, ("xiaohongshu.com", "xhslink.com", "xhs.cn")),
    (Platform.WECHAT, ("mp.weixin.qq.com", "weixin.qq.com", "wechat.com")),
]

# 短链域名 → 平台（用于在无需网络时也能给出候选平台，便于日志/降级提示）
_SHORTLINK_HOSTS: dict[str, Platform] = {
    "v.douyin.com": Platform.DOUYIN,
    "xhslink.com": Platform.XHS,
    "b23.tv": Platform.BILI,
    "bili2233.cn": Platform.BILI,
    "acg.tv": Platform.BILI,
}


def _host_matches(host: str, suffix: str) -> bool:
    """host 等于 suffix，或以 `.suffix` 结尾（兼容子域名）。"""
    return host == suffix or host.endswith("." + suffix)


def identify_platform(url: str) -> Platform:
    """给定 URL，返回平台类型。纯函数，不触网。

    微信特例：``mp.weixin.qq.com`` 域下只有 ``/s`` 路径才算文章，其它路径
    （如 ``/mp/appmsgalbum``、``/mp/homepage``）属聚合/开放平台，交给通用兜底。
    """
    try:
        u = httpx.URL(url)
    except Exception:
        return Platform.GENERIC
    host = (u.host or "").lower()
    if not host:
        return Platform.GENERIC
    path = u.path or ""
    for platform, suffixes in _PLATFORM_HOSTS:
        for suffix in suffixes:
            if _host_matches(host, suffix):
                if platform is Platform.WECHAT:
                    if path == "/s" or path.startswith("/s/"):
                        return platform
                    continue
                return platform
    return Platform.GENERIC


def is_shortlink(url: str) -> bool:
    """是否为已知短链域名（需要展开才能拿到真实平台/内容 ID）。"""
    try:
        host = (httpx.URL(url).host or "").lower()
    except Exception:
        return False
    return host in _SHORTLINK_HOSTS


@dataclass(frozen=True)
class RouteDecision:
    """路由结果。"""

    platform: Platform
    is_shortlink: bool = False
    shortlink_platform: Platform | None = None


class UrlRouter:
    """识别入口：给定 URL 判断平台，并提示是否需要展开短链。"""

    def route(self, url: str) -> RouteDecision:
        platform = identify_platform(url)
        short = is_shortlink(url)
        return RouteDecision(
            platform=platform,
            is_shortlink=short,
            shortlink_platform=(_SHORTLINK_HOSTS.get(self._host(url)) if short else None),
        )

    @staticmethod
    def _host(url: str) -> str:
        try:
            return (httpx.URL(url).host or "").lower()
        except Exception:
            return ""
