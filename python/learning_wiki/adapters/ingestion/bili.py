"""BiliAdapter：B站视频/专栏 → 规范化文本。

B站是四个平台里最友好的一个：有公开、无需登录的 web API。策略：

1. 短链（b23.tv / bili2233.cn / acg.tv）经 GuardedFetcher 展开到
   ``bilibili.com/video/BVxxxx``；
2. 从落地 URL 提取稳定 ID（BV 号优先，兼容 av 号）；
3. 调 ``api.bilibili.com/x/web-interface/view`` 拿标题 / 简介 / UP 主 / 分P；
4. 组装为 Markdown，产出 ExtractionResult。

canonical_url 使用稳定 BV URL（而非短链），保证同一视频换短链不会重复入库。
视频口播/字幕的文本转写（ASR）属异步路径，见 asr.py；本适配器只取元数据与
简介这类「同步可得的文本」，并如实标注 media_kind=video。
"""

from __future__ import annotations

import json
import re

from learning_wiki.adapters.ingestion.media import BiliMediaFetcher
from learning_wiki.adapters.net_guard import CaptureBlockedError, GuardedFetcher
from learning_wiki.domain.contracts import ExtractionResult, InboxItem
from learning_wiki.storage import evidence_store

_BV_RE = re.compile(r"/video/(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"/video/av(\d+)")
_VIEW_API = "https://api.bilibili.com/x/web-interface/view"

_MIN_CONTENT_CHARS = 20  # 简介可能很短，标题+简介即可入库


class BiliAdapter:
    input_type = "url"

    def __init__(self, fetcher: GuardedFetcher | None = None) -> None:
        self.fetcher = fetcher or GuardedFetcher()
        self.media = BiliMediaFetcher(fetcher=self.fetcher)

    def can_handle(self, item: InboxItem) -> bool:
        return item.input_type == "url" and _extract_bvid(item.payload or "") is not None

    def extract(self, item: InboxItem, progress=None) -> ExtractionResult:
        url = (item.payload or "").strip()
        if progress:
            progress("resolve", 0.1)
        final_url = self._resolve(url)
        bvid = _extract_bvid(final_url)
        if bvid is None:
            raise CaptureBlockedError("无法从 B站链接解析出视频 ID（BV/av）")

        if progress:
            progress("fetch", 0.4)
        data = self._fetch_view(bvid)

        if progress:
            progress("build", 0.8)
        markdown, title, author, desc = self._build_markdown(data, bvid)

        return ExtractionResult(
            original_bytes=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            original_filename="original.json",
            content_markdown=markdown,
            title=title,
            author=author,
            canonical_url=f"https://www.bilibili.com/video/{bvid}",
            published_at=data.get("pubdate") and _ts_to_iso(data["pubdate"]),
            extraction_method="bili",
            extraction_quality=0.9 if desc else 0.6,
            platform="bili",
            media_kind="video",
            blocks=evidence_store.split_markdown_blocks(markdown),
        )

    def fetch_audio(self, url: str, *, prefer_lowest: bool = True):
        """下载视频音频（供 ASR 转写）。返回 (cid, FetchedAudio)。

        转写不需要高音质，默认选最低码率音频，省流量省时间。
        """
        final_url = self._resolve(url.strip())
        bvid = _extract_bvid(final_url)
        if bvid is None:
            raise CaptureBlockedError("无法从 B站链接解析出视频 ID（BV/av）")
        view = self._fetch_view(bvid)
        cid = view.get("cid")
        if not cid:
            raise CaptureBlockedError("视频缺少 cid，无法定位播放流")
        return cid, self.media.fetch_audio(bvid, int(cid), prefer_lowest=prefer_lowest)

    # -- 内部 ---------------------------------------------------------------

    def _resolve(self, url: str) -> str:
        """短链展开；已是完整 URL 则原样返回。"""
        if _BV_RE.search(url) or _AV_RE.search(url):
            return url
        page = self.fetcher.fetch(url)
        return page.url

    def _fetch_view(self, bvid: str) -> dict:
        page = self.fetcher.fetch(f"{_VIEW_API}?bvid={bvid}")
        if page.status != 200:
            raise CaptureBlockedError(f"B站 API 返回 HTTP {page.status}")
        body = json.loads(page.content.decode("utf-8", errors="replace"))
        if body.get("code") != 0:
            raise CaptureBlockedError(f"B站 API 错误: {body.get('message', body.get('code'))}")
        return body.get("data") or {}

    def _build_markdown(self, data: dict, bvid: str) -> tuple[str, str, str | None, str]:
        title = data.get("title") or f"B站视频 {bvid}"
        author = (data.get("owner") or {}).get("name")
        desc = (data.get("desc") or "").strip()
        pages = data.get("pages") or []
        stat = data.get("stat") or {}

        parts: list[str] = [f"# {title}\n"]
        if author:
            parts.append(f"> UP 主：{author}\n")
        if data.get("pic"):
            parts.append(f"封面：{data['pic']}\n")
        if desc:
            parts.append(f"\n{desc}\n")
        if len(pages) > 1:
            parts.append(f"\n分P（共 {len(pages)}）：\n")
            for p in pages:
                parts.append(f"- P{p.get('page', '')} {p.get('part', '')}")
                if p.get("duration"):
                    parts[-1] += f"（{p['duration'] // 60}:{p['duration'] % 60:02d}）"
        if stat:
            parts.append(
                f"\n播放 {stat.get('view', '—')} · 弹幕 {stat.get('danmaku', '—')}"
                f" · 收藏 {stat.get('favorite', '—')}\n"
            )
        parts.append(f"\n视频地址：https://www.bilibili.com/video/{bvid}\n")
        return "\n".join(parts), title, author, desc


def _extract_bvid(url: str) -> str | None:
    m = _BV_RE.search(url)
    if m:
        return m.group(1)
    m = _AV_RE.search(url)
    if m:
        return f"av{m.group(1)}"
    return None


def _ts_to_iso(ts: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).isoformat()
