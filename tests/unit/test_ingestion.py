"""接入层单元测试：路由、注册表、B站解析、降级链。

全部用 mock fetcher，不真实触网；B站 API 返回用伪造 JSON 喂给适配器。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from learning_wiki.adapters.ingestion.bili import BiliAdapter, _extract_bvid
from learning_wiki.adapters.ingestion.dispatcher import IngestionDispatcher
from learning_wiki.adapters.ingestion.registry import AdapterRegistry
from learning_wiki.adapters.ingestion.router import (
    Platform,
    UrlRouter,
    identify_platform,
    is_shortlink,
)
from learning_wiki.adapters.ingestion.wechat import WechatAdapter
from learning_wiki.adapters.net_guard import FetchedPage, PermanentCaptureError
from learning_wiki.domain.contracts import InboxItem

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class FakeFetcher:
    """按 URL 返回预设 FetchedPage 的假 fetcher。"""

    def __init__(self, responses: dict[str, FetchedPage]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.headers_seen: list[dict[str, str]] = []

    def fetch(self, url: str, headers: dict[str, str] | None = None) -> FetchedPage:
        self.calls.append(url)
        self.headers_seen.append(headers or {})
        if url not in self.responses:
            raise AssertionError(f"未预设的 URL: {url}")
        return self.responses[url]


def _page(body: bytes, url: str, status: int = 200) -> FetchedPage:
    return FetchedPage(url=url, status=status, headers={}, content=body)


def _inbox(url: str) -> InboxItem:
    return InboxItem(
        item_id="itm_00000000000000000000000001",
        input_type="url",
        payload=url,
        created_at="2026-09-05T10:00:00+08:00",
    )


# ---------------------------------------------------------------------------
# 路由：平台识别
# ---------------------------------------------------------------------------


class TestRouter:
    @pytest.mark.parametrize(
        ("url", "platform"),
        [
            ("https://www.bilibili.com/video/BV1xx411c7mD", Platform.BILI),
            ("https://b23.tv/abcd1234", Platform.BILI),
            ("https://v.douyin.com/xxxx/", Platform.DOUYIN),
            ("https://www.douyin.com/video/7123456789012345678", Platform.DOUYIN),
            ("https://xhslink.com/a/abcd", Platform.XHS),
            ("https://www.xiaohongshu.com/explore/abc123", Platform.XHS),
            ("https://mp.weixin.qq.com/s/abcDEFgh", Platform.WECHAT),
            ("https://example.com/some/article", Platform.GENERIC),
            ("https://www.zhihu.com/question/123", Platform.GENERIC),
            ("not-a-url", Platform.GENERIC),
        ],
    )
    def test_identify(self, url: str, platform: Platform) -> None:
        assert identify_platform(url) is platform

    @pytest.mark.parametrize(
        ("url", "short"),
        [
            ("https://b23.tv/abcd", True),
            ("https://v.douyin.com/xxxx/", True),
            ("https://xhslink.com/a/abcd", True),
            ("https://www.bilibili.com/video/BV1xx411c7mD", False),
            ("https://mp.weixin.qq.com/s/abc", False),
        ],
    )
    def test_shortlink(self, url: str, short: bool) -> None:
        assert is_shortlink(url) is short

    def test_route_returns_decision(self) -> None:
        d = UrlRouter().route("https://b23.tv/abcd")
        assert d.platform is Platform.BILI
        assert d.is_shortlink is True
        assert d.shortlink_platform is Platform.BILI


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_get_unregistered_returns_none(self) -> None:
        reg = AdapterRegistry()
        assert reg.get("bili") is None

    def test_register_and_extract(self) -> None:
        reg = AdapterRegistry()
        reg.register("bili", BiliAdapter(fetcher=FakeFetcher({})))
        assert reg.get("bili") is not None
        assert reg.platforms() == ["bili"]

    def test_extract_when_can_handle_false(self) -> None:
        reg = AdapterRegistry()
        reg.register("bili", BiliAdapter(fetcher=FakeFetcher({})))
        # 非 B站 URL → can_handle False → 返回 None
        assert reg.extract("bili", _inbox("https://example.com/x")) is None


# ---------------------------------------------------------------------------
# B站适配器
# ---------------------------------------------------------------------------


class TestBiliAdapter:
    def _view_data(self, bvid: str) -> dict:
        return {
            "code": 0,
            "message": "0",
            "data": {
                "bvid": bvid,
                "title": "测试视频标题",
                "desc": "这是一个测试简介。",
                "pic": "https://i0.hdslb.com/bfs/xx.jpg",
                "pubdate": 1757000000,
                "owner": {"name": "测试UP主"},
                "pages": [{"page": 1, "part": "正片", "duration": 300}],
                "stat": {"view": 1234, "danmaku": 56, "favorite": 78},
            },
        }

    def test_extract_bvid_variants(self) -> None:
        assert _extract_bvid("https://www.bilibili.com/video/BV1xx411c7mD") == "BV1xx411c7mD"
        assert _extract_bvid("https://www.bilibili.com/video/av170001") == "av170001"
        assert _extract_bvid("https://example.com/x") is None

    def test_extract_full_url(self) -> None:
        bvid = "BV1xx411c7mD"
        fetcher = FakeFetcher(
            {
                f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}": _page(
                    json.dumps(self._view_data(bvid)).encode(),
                    f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                )
            }
        )
        adapter = BiliAdapter(fetcher=fetcher)
        result = adapter.extract(_inbox(f"https://www.bilibili.com/video/{bvid}"))
        assert result.title == "测试视频标题"
        assert result.author == "测试UP主"
        assert result.platform == "bili"
        assert result.media_kind == "video"
        assert result.canonical_url == f"https://www.bilibili.com/video/{bvid}"
        assert "测试简介" in result.content_markdown
        assert result.blocks  # 有证据块

    def test_extract_shortlink_resolves(self) -> None:
        bvid = "BV1xx411c7mD"
        fetcher = FakeFetcher(
            {
                "https://b23.tv/abcd": _page(
                    b"", f"https://www.bilibili.com/video/{bvid}", status=302
                ),
                f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}": _page(
                    json.dumps(self._view_data(bvid)).encode(),
                    f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                ),
            }
        )
        adapter = BiliAdapter(fetcher=fetcher)
        result = adapter.extract(_inbox("https://b23.tv/abcd"))
        # 短链展开后拿到稳定 canonical（BV），而非短链
        assert result.canonical_url == f"https://www.bilibili.com/video/{bvid}"
        assert result.title == "测试视频标题"

    def test_api_error_raises(self) -> None:
        bvid = "BV1xx411c7mD"
        fetcher = FakeFetcher(
            {
                f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}": _page(
                    json.dumps({"code": -404, "message": "啥都木有"}).encode(),
                    "https://api.bilibili.com/x/web-interface/view",
                )
            }
        )
        adapter = BiliAdapter(fetcher=fetcher)
        from learning_wiki.adapters.net_guard import CaptureBlockedError

        with pytest.raises(CaptureBlockedError):
            adapter.extract(_inbox(f"https://www.bilibili.com/video/{bvid}"))


# ---------------------------------------------------------------------------
# 降级链
# ---------------------------------------------------------------------------


class TestDispatcher:
    def test_bili_platform_hits_dedicated_adapter(self) -> None:
        bvid = "BV1xx411c7mD"
        data = {
            "code": 0,
            "data": {
                "bvid": bvid,
                "title": "B站标题",
                "desc": "简介",
                "owner": {"name": "UP"},
                "pages": [{"page": 1, "part": "P1", "duration": 60}],
                "stat": {},
            },
        }
        fetcher = FakeFetcher(
            {
                f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}": _page(
                    json.dumps(data).encode(), "https://api.bilibili.com/x/web-interface/view"
                )
            }
        )
        dispatcher = IngestionDispatcher(fetcher=fetcher)
        result = dispatcher.extract(_inbox(f"https://www.bilibili.com/video/{bvid}"))
        assert result.platform == "bili"
        assert result.extraction_method == "bili"

    def test_generic_fallback_for_unknown_platform(self) -> None:
        # 未识别平台 → 走 generic（trafilatura）。这里用能解析正文的 HTML。
        long_text = "body content long enough for extraction. " * 40
        html = (
            "<html><head><title>T</title></head><body><article><p>"
            + long_text
            + "</p></article></body></html>"
        ).encode("utf-8")
        fetcher = FakeFetcher({"https://example.com/a": _page(html, "https://example.com/a")})
        dispatcher = IngestionDispatcher(fetcher=fetcher)
        result = dispatcher.extract(_inbox("https://example.com/a"))
        assert result.extraction_method == "trafilatura"


# ---------------------------------------------------------------------------
# 微信适配器
# ---------------------------------------------------------------------------


def _wechat_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_text(encoding="utf-8").encode("utf-8")


def _wechat_fetcher(mapping: dict[str, tuple[str, str]]) -> FakeFetcher:
    """(url -> (fixture 名, 落地 URL)) → FakeFetcher。"""
    responses = {
        url: _page(_wechat_fixture(fixture), final_url)
        for url, (fixture, final_url) in mapping.items()
    }
    return FakeFetcher(responses)


SHORT_URL = "https://mp.weixin.qq.com/s/7EFs3X2rORIUAY1aZPFk-A"
LONG_URL = (
    "https://mp.weixin.qq.com/s?__biz=MzUxNjI3NTg4Mg==&mid=2247485512"
    "&idx=1&sn=abc123def456&chksm=zzz999&scene=21#rd"
)


class TestWechatRouter:
    @pytest.mark.parametrize(
        ("url", "platform"),
        [
            (SHORT_URL, Platform.WECHAT),
            (LONG_URL, Platform.WECHAT),
            # mp 域下的聚合页/开放平台路径不算文章 → 交通用兜底
            ("https://mp.weixin.qq.com/mp/appmsgalbum?__biz=abc", Platform.GENERIC),
            ("https://mp.weixin.qq.com/mp/homepage?__biz=abc", Platform.GENERIC),
            ("https://mp.weixin.qq.com/", Platform.GENERIC),
        ],
    )
    def test_only_s_path_is_article(self, url: str, platform: Platform) -> None:
        assert identify_platform(url) is platform

    def test_no_wechat_shortlink_domain(self) -> None:
        # 微信短链是路径形态（/s/<id>），没有独立短链域名
        assert is_shortlink(SHORT_URL) is False


class TestWechatAdapter:
    def test_can_handle_only_article_urls(self) -> None:
        adapter = WechatAdapter(fetcher=FakeFetcher({}))
        assert adapter.can_handle(_inbox(SHORT_URL)) is True
        assert adapter.can_handle(_inbox(LONG_URL)) is True
        assert adapter.can_handle(_inbox("https://mp.weixin.qq.com/mp/homepage?x=1")) is False
        assert adapter.can_handle(_inbox("https://example.com/a")) is False

    def test_extract_shortlink_article(self) -> None:
        fetcher = _wechat_fetcher({SHORT_URL: ("wechat_article.html", SHORT_URL)})
        result = WechatAdapter(fetcher=fetcher).extract(_inbox(SHORT_URL))

        assert result.title == "测试微信文章：OpenAI Astra 提示词写法"
        assert result.author == "贾克斯的平行世界"
        assert result.platform == "wechat"
        assert result.extraction_method == "wechat"
        assert result.extraction_quality == 0.9
        assert result.media_kind == "mixed"  # 文末有图
        assert result.published_at == "2026-09-05T14:55:01+00:00"
        assert result.blocks

    def test_canonical_uses_quadruples_from_page_vars(self) -> None:
        """短链页面：四元组来自页面变量，与长链归一到同一个 canonical。"""
        fetcher = _wechat_fetcher({SHORT_URL: ("wechat_article.html", SHORT_URL)})
        result = WechatAdapter(fetcher=fetcher).extract(_inbox(SHORT_URL))
        assert result.canonical_url == (
            "https://mp.weixin.qq.com/s?__biz=MzUxNjI3NTg4Mg==&mid=2247485512"
            "&idx=1&sn=68714003bf9ee4dc21d04df2ce8042be"
        )

    def test_longlink_canonical_strips_tracking_params(self) -> None:
        """长链：四元组来自 URL 参数；chksm/scene/#rd 必须剥掉。"""
        fetcher = _wechat_fetcher({LONG_URL: ("wechat_longlink.html", LONG_URL)})
        result = WechatAdapter(fetcher=fetcher).extract(_inbox(LONG_URL))
        assert result.canonical_url == (
            "https://mp.weixin.qq.com/s?__biz=MzUxNjI3NTg4Mg==&mid=2247485512&idx=1&sn=abc123def456"
        )

    def test_markdown_keeps_structure(self) -> None:
        fetcher = _wechat_fetcher({SHORT_URL: ("wechat_article.html", SHORT_URL)})
        md = WechatAdapter(fetcher=fetcher).extract(_inbox(SHORT_URL)).content_markdown

        # 装饰性 strong（<strong>/</strong>）被过滤，标题干净
        assert "# 1. 主动推进，把任务完成" in md
        assert "**/**" not in md
        # 结构保留
        assert "**核心优势**" in md
        assert "`" in md  # 内联 code
        assert "```python" in md  # 代码块
        assert "- 要点一：自主推进" in md  # 列表
        assert "> 引文" in md  # 引用
        assert "![图1](https://mmbiz.qpic.cn/img1.jpg)" in md  # 懒加载图片 data-src
        assert "## 小结" in md
        # 元数据 + 原文链接
        assert "> 公众号：贾克斯的平行世界" in md
        assert "原文链接：https://mp.weixin.qq.com/s?" in md

    def test_evidence_bytes_is_fragment_not_whole_page(self) -> None:
        """original_bytes 必须是 #img-content 片段，不是整页（整页 3MB+）。"""
        fetcher = _wechat_fetcher({SHORT_URL: ("wechat_article.html", SHORT_URL)})
        result = WechatAdapter(fetcher=fetcher).extract(_inbox(SHORT_URL))
        assert result.original_bytes is not None
        assert b'id="img-content"' in result.original_bytes
        assert len(result.original_bytes) < 20_000

    def test_image_msg_falls_back_to_og_description(self) -> None:
        """图片消息/SSR：无 #js_content → 用 og:description，质量降级。"""
        url = "https://mp.weixin.qq.com/s/RUHJpS9w3RhuhEm94z-1Kw"
        fetcher = _wechat_fetcher({url: ("wechat_image_msg.html", url)})
        result = WechatAdapter(fetcher=fetcher).extract(_inbox(url))

        assert result.extraction_quality == 0.3
        assert "图集描述" in result.content_markdown
        # \x0a 字面转义必须解码为真实换行，不能留在正文里
        assert "\\x0a" not in result.content_markdown
        assert result.title == "图集类文章"

    def test_image_msg_without_og_description_raises(self) -> None:
        """无 js_content 且无 og 摘要 → 上抛可行动的失败原因（不落通用兜底）。"""
        url = "https://mp.weixin.qq.com/s/nocontent1234567890ab"
        html = (
            "<html><head><meta property='og:title' content='空文章'></head>"
            "<body><div id='js_article'></div></body></html>"
        ).encode()
        fetcher = FakeFetcher({url: _page(html, url)})
        with pytest.raises(PermanentCaptureError, match="图片消息"):
            WechatAdapter(fetcher=fetcher).extract(_inbox(url))

    def test_captcha_page_raises(self) -> None:
        fetcher = _wechat_fetcher({SHORT_URL: ("wechat_captcha.html", SHORT_URL)})
        with pytest.raises(PermanentCaptureError, match="环境异常"):
            WechatAdapter(fetcher=fetcher).extract(_inbox(SHORT_URL))

    def test_deleted_article_raises(self) -> None:
        url = "https://mp.weixin.qq.com/s/deleted1234567890abc"
        html = "<html><body><p>该内容已被发布者删除</p></body></html>".encode()
        fetcher = FakeFetcher({url: _page(html, url)})
        with pytest.raises(PermanentCaptureError, match="已被发布者删除"):
            WechatAdapter(fetcher=fetcher).extract(_inbox(url))

    def test_http_error_raises(self) -> None:
        fetcher = FakeFetcher({SHORT_URL: _page(b"", SHORT_URL, status=404)})
        with pytest.raises(PermanentCaptureError, match="HTTP 404"):
            WechatAdapter(fetcher=fetcher).extract(_inbox(SHORT_URL))

    def test_uses_browser_user_agent(self) -> None:
        """默认 UA 会被微信拦到验证码页，必须伪装浏览器 UA。"""
        fetcher = _wechat_fetcher({SHORT_URL: ("wechat_article.html", SHORT_URL)})
        WechatAdapter(fetcher=fetcher).extract(_inbox(SHORT_URL))
        assert fetcher.headers_seen, "fetcher 未记录 headers"
        ua = fetcher.headers_seen[-1].get("User-Agent", "")
        assert "Chrome" in ua
        assert "learning-wiki" not in ua


class TestDispatcherWechat:
    def test_wechat_platform_hits_dedicated_adapter(self) -> None:
        fetcher = _wechat_fetcher({SHORT_URL: ("wechat_article.html", SHORT_URL)})
        dispatcher = IngestionDispatcher(fetcher=fetcher)
        result = dispatcher.extract(_inbox(SHORT_URL))
        assert result.platform == "wechat"
        assert result.extraction_method == "wechat"

    def test_wechat_permanent_error_not_downgraded(self) -> None:
        """验证码页是永久失败：不降级到 trafilatura（那只会得到无意义的正文过短）。"""
        fetcher = _wechat_fetcher({SHORT_URL: ("wechat_captcha.html", SHORT_URL)})
        dispatcher = IngestionDispatcher(fetcher=fetcher)
        with pytest.raises(PermanentCaptureError, match="环境异常"):
            dispatcher.extract(_inbox(SHORT_URL))
