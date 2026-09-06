"""小红书接入单元测试。

全部用 FakeFetcher 注入脱敏 fixture，不触网；覆盖：
- URL 解析（三种形态 + 短链）
- 缺 xsec_token 给出可行动错误
- 页面结构异常（找不到 __INITIAL_STATE__）容错
- canonical_url 剥离 token（不同 token 同一笔记不重复入库）
- 正文话题标签清洗
- Markdown 组装 / media_kind / quality
- dispatcher 注册与降级链
"""

from __future__ import annotations

import pathlib

import pytest

from learning_wiki.adapters.ingestion.dispatcher import IngestionDispatcher
from learning_wiki.adapters.ingestion.xhs import (
    XhsAdapter,
    _clean_desc,
    _extract_note_id,
    _extract_token,
    _find_note,
    _quality,
)
from learning_wiki.adapters.net_guard import (
    CaptureBlockedError,
    FetchedPage,
    PermanentCaptureError,
)
from learning_wiki.domain.contracts import InboxItem

FIXTURE_DIR = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
NID = "6a585ada0000000011017e41"


# ---------------------------------------------------------------------------
# 假 fetcher 与助手
# ---------------------------------------------------------------------------


class FakeFetcher:
    def __init__(self, responses: dict[str, FetchedPage]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def fetch(self, url: str) -> FetchedPage:
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"未预设的 URL: {url}")
        return self.responses[url]


def _page(body: bytes | str, url: str, status: int = 200) -> FetchedPage:
    if isinstance(body, str):
        body = body.encode("utf-8")
    return FetchedPage(url=url, status=status, headers={}, content=body)


def _inbox(url: str) -> InboxItem:
    return InboxItem(
        item_id="itm_00000000000000000000000001",
        input_type="url",
        payload=url,
        created_at="2026-09-05T10:00:00+08:00",
    )


def _ok_html() -> bytes:
    return (FIXTURE_DIR / "xhs_note_ok.html").read_bytes()


def _404_html() -> bytes:
    return (FIXTURE_DIR / "xhs_note_404.html").read_bytes()


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


class TestUrlParsing:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                f"https://www.xiaohongshu.com/explore/{NID}?xsec_token=AAA=&xsec_source=pc_feed",
                NID,
            ),
            (
                f"https://www.xiaohongshu.com/discovery/item/{NID}?xsec_token=BBB",
                NID,
            ),
            (f"https://xhslink.com/a/{NID}", None),
            ("https://example.com/foo", None),
        ],
    )
    def test_note_id(self, url: str, expected: str | None) -> None:
        assert _extract_note_id(url) == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.xiaohongshu.com/explore/X?xsec_token=ABC&xsec_source=pc", "ABC"),
            ("https://www.xiaohongshu.com/explore/X?xsecToken=XYZ", "XYZ"),
            ("https://www.xiaohongshu.com/explore/X", None),
        ],
    )
    def test_token(self, url: str, expected: str | None) -> None:
        assert _extract_token(url) == expected


class TestCleanDesc:
    def test_topic_marker_removed(self) -> None:
        assert _clean_desc("#纸嫁衣[话题]# #其他#") == "#纸嫁衣# #其他#"

    def test_plain_text_unchanged(self) -> None:
        assert _clean_desc("普通正文 #标签#") == "普通正文 #标签#"

    def test_empty_and_none(self) -> None:
        assert _clean_desc("") == ""
        assert _clean_desc(None) == ""

    def test_long_topic_skipped(self) -> None:
        # 主题标签长度 > 30 时正则不匹配；保留原文本（异常输入容错）
        weird = "#" + "x" * 31 + "[话题]#"
        assert _clean_desc(weird) == weird


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


class TestXhsAdapter:
    def _ok_url(self, token: str = "TOK") -> str:
        return f"https://www.xiaohongshu.com/explore/{NID}?xsec_token={token}&xsec_source=pc_feed"

    def _ok_response(self, url: str) -> FetchedPage:
        return _page(_ok_html(), url)

    def test_extract_full_url(self) -> None:
        url = self._ok_url("TOK-A")
        adapter = XhsAdapter(fetcher=FakeFetcher({url: self._ok_response(url)}))
        r = adapter.extract(_inbox(url))
        assert r.title == "纸嫁衣里的纸人像哪位明星"
        assert r.author == "测试用户"
        assert r.platform == "xhs"
        assert r.media_kind == "image"
        assert r.extraction_method == "xhs"
        assert r.canonical_url == f"https://www.xiaohongshu.com/explore/{NID}"
        # token 不进 canonical
        assert "xsec_token" not in (r.canonical_url or "")
        assert "xsec_token" not in r.content_markdown
        # desc 清洗：话题标签还原
        assert "#纸嫁衣[话题]#" not in r.content_markdown
        assert "#纸嫁衣#" in r.content_markdown
        # 元数据
        assert "点赞" in r.content_markdown and "4903" in r.content_markdown
        assert "图片 2 张" in r.content_markdown
        assert r.blocks  # 有证据块
        # fixture 正文偏短（图为主的小红书图文笔记典型情况），反映真实数据
        assert r.extraction_quality == 0.45
        # 时间戳（毫秒）转换
        assert r.published_at == "2025-09-04T15:33:20+00:00"

    def test_canonical_strip_different_token(self) -> None:
        """同一笔记不同 token → 同一 canonical；保证重复检测生效。"""
        urls = [self._ok_url(f"TOK-{t}") for t in ("A", "B", "C")]
        fetcher = FakeFetcher({u: self._ok_response(u) for u in urls})
        adapter = XhsAdapter(fetcher=fetcher)
        canonicals = {adapter.extract(_inbox(u)).canonical_url for u in urls}
        assert canonicals == {f"https://www.xiaohongshu.com/explore/{NID}"}

    def test_shortlink_via_final_url(self) -> None:
        """短链 fetch 后 final_url 应包含 token；适配器从 final_url 提取。"""
        final = self._ok_url("FROM_SHORT")
        adapter = XhsAdapter(fetcher=FakeFetcher({final: self._ok_response(final)}))
        # 直接传 final URL（短链跳转后等价）→ canonical 干净
        r = adapter.extract(_inbox(final))
        assert r.canonical_url == f"https://www.xiaohongshu.com/explore/{NID}"
        assert r.title == "纸嫁衣里的纸人像哪位明星"

    def test_missing_token_raises_permanent(self) -> None:
        """无 token → 服务器 302 到 /404；适配器给出可行动的错误。"""
        url_no_token = f"https://www.xiaohongshu.com/explore/{NID}"
        # 模拟真实 302 跳转结果：page.url 是 /404
        page_404 = _page(
            _404_html(),
            "https://www.xiaohongshu.com/404?source=/404/sec_xxx",
        )
        adapter = XhsAdapter(fetcher=FakeFetcher({url_no_token: page_404}))
        with pytest.raises(PermanentCaptureError) as ei:
            adapter.extract(_inbox(url_no_token))
        assert "xsec_token" in str(ei.value)
        assert "重新分享" in str(ei.value)

    def test_404_state_with_id_raises_permanent(self) -> None:
        """服务器返回 200 + 空 state（极少见但要兜住）。"""
        url = self._ok_url("BAD-TOK")
        # 直接给 404 fixture（noteDetailMap 为空）
        adapter = XhsAdapter(fetcher=FakeFetcher({url: _page(_404_html(), url)}))
        with pytest.raises(PermanentCaptureError):
            adapter.extract(_inbox(url))

    def test_page_structure_broken(self) -> None:
        url = self._ok_url("X")
        broken = b"<html><head></head><body>no state here</body></html>"
        adapter = XhsAdapter(fetcher=FakeFetcher({url: _page(broken, url)}))
        with pytest.raises(PermanentCaptureError) as ei:
            adapter.extract(_inbox(url))
        assert "__INITIAL_STATE__" in str(ei.value) or "结构异常" in str(ei.value)

    def test_non_xhs_url_not_handled(self) -> None:
        adapter = XhsAdapter(fetcher=FakeFetcher({}))
        assert not adapter.can_handle(_inbox("https://www.bilibili.com/video/BV1"))
        assert not adapter.can_handle(_inbox("not-a-url"))

    def test_no_note_id_raises_capture(self) -> None:
        url = "https://www.xiaohongshu.com/user/profile/abc"
        page = _page(b"<html></html>", url)
        adapter = XhsAdapter(fetcher=FakeFetcher({url: page}))
        with pytest.raises(CaptureBlockedError):
            adapter.extract(_inbox(url))

    def test_original_json_contains_note(self) -> None:
        """原始快照为结构化 JSON（不是 65KB HTML），便于重提取。"""
        url = self._ok_url("Z")
        adapter = XhsAdapter(fetcher=FakeFetcher({url: self._ok_response(url)}))
        r = adapter.extract(_inbox(url))
        assert r.original_filename == "original.json"
        assert r.original_bytes is not None
        # 必须是有效 JSON
        import json as _json

        data = _json.loads(r.original_bytes.decode("utf-8"))
        assert "note" in data
        assert data["note"]["noteId"] == NID

    def test_find_note_handles_bare_entry(self) -> None:
        """noteDetailMap[id] 直接是 note dict 的兼容路径（少数情形）。"""
        state = {"note": {"noteDetailMap": {NID: {"noteId": NID, "type": "normal", "title": "x"}}}}
        n = _find_note(state, NID)
        assert n is not None
        assert n["title"] == "x"

    def test_find_note_missing(self) -> None:
        assert _find_note({"note": {"noteDetailMap": {}}}, NID) is None


class TestQuality:
    """_quality 决定下游 Agent 是否值得投入学习，必须覆盖各档。"""

    def test_long_desc(self) -> None:
        note = {"title": "t", "desc": "正文" + "x" * 100}
        assert _quality(note) == 0.85

    def test_medium_desc(self) -> None:
        note = {"title": "t", "desc": "正文" + "x" * 60}
        assert _quality(note) == 0.7

    def test_short_desc_with_title(self) -> None:
        # 小红书图文笔记典型：desc 几十字 + 标题
        note = {"title": "标题", "desc": "正文只有几十个字"}
        assert _quality(note) == 0.45

    def test_only_title(self) -> None:
        note = {"title": "标题", "desc": ""}
        assert _quality(note) == 0.45

    def test_nothing(self) -> None:
        note = {"title": "", "desc": ""}
        assert _quality(note) == 0.25


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TestDispatcherXhs:
    def test_xhs_platform_hits_dedicated_adapter(self) -> None:
        url = f"https://www.xiaohongshu.com/explore/{NID}?xsec_token=T&xsec_source=pc"
        fetcher = FakeFetcher({url: _page(_ok_html(), url)})
        d = IngestionDispatcher(fetcher=fetcher)
        r = d.extract(_inbox(url))
        assert r.extraction_method == "xhs"
        assert r.platform == "xhs"

    def test_permanent_error_not_swallowed(self) -> None:
        """凭证缺失类错误必须直达用户，不被通用兜底吞掉。"""
        url = f"https://www.xiaohongshu.com/explore/{NID}"
        page_404 = _page(
            _404_html(),
            "https://www.xiaohongshu.com/404?source=/404/sec_x",
        )
        fetcher = FakeFetcher({url: page_404})
        d = IngestionDispatcher(fetcher=fetcher)
        with pytest.raises(PermanentCaptureError) as ei:
            d.extract(_inbox(url))
        assert "xsec_token" in str(ei.value)
