"""XhsAdapter：小红书笔记 → 结构化文本。

实测结论（2026-09）：小红书笔记详情页 SSR 首屏直接内联
``window.__INITIAL_STATE__``，匿名即可拿到标题 / 正文 / 作者 / 话题 /
互动数据，**不需要登录 cookie、不需要 X-S 签名、不需要伪造 TLS
指纹**（风控在 API 接口签名层，SSR 首屏不过 X-S 校验）。但必须携带
``xsec_token``：

- 带 token → 200 + 完整 ``noteDetailMap``
- 缺 / 过期 → 302 到 ``/404``，``noteDetailMap`` 为 ``{}``，HTTP 仍是 200

成功判定因此必须做**内容级检查**，不能只看状态码。
``xsec_token`` 每次分享 / 每次首页渲染都不同，故 ``canonical_url``
一律剥离 token，否则同一笔记会因 token 变化反复入库，违反规格 5.1
的重复检测（按 canonical_url 查重）。

无 token / 数据缺失等"通用兜底救不了"的失败一律抛
``PermanentCaptureError``，由 dispatcher 原样上抛，把可行动的原因
（"请复制完整分享链接"）交给用户。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

import httpx

from learning_wiki.adapters.ingestion.router import Platform, identify_platform
from learning_wiki.adapters.net_guard import (
    CaptureBlockedError,
    GuardedFetcher,
    PermanentCaptureError,
)
from learning_wiki.domain.contracts import ExtractionResult, InboxItem
from learning_wiki.storage import evidence_store

# 小红书 noteId 历史上是 24 位 hex；留 16..32 宽松匹配
_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-fA-F]{16,32})")
# desc 里的 #xxx[话题]# 还原为普通话题标签 #xxx#
_TOPIC_RE = re.compile(r"#([^#\[\]\n]{1,30})\[话题\]#")
_MIN_DESC_CHARS = 8
_HINT_NO_TOKEN = "请从小红书 App 或网页版重新分享笔记 → 复制完整链接（含 xsec_token 参数）后重试"


def _extract_note_id(url: str) -> str | None:
    m = _NOTE_ID_RE.search(url)
    return m.group(1) if m else None


def _extract_token(url: str) -> str | None:
    """从 URL query 取 xsec_token；不同场景下参数名有差异，兼容两种。"""
    try:
        params = httpx.URL(url).params
    except Exception:
        return None
    for key in ("xsec_token", "xsecToken"):
        v = params.get(key)
        if v:
            return v
    return None


def _scan_object(text: str, start: int) -> str | None:
    """从 ``start``（指向 ``{``）做括号配对扫描，跳过字符串字面量。"""
    depth = 0
    i = start
    in_str = False
    esc = False
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def _extract_initial_state(html: str) -> dict[str, Any] | None:
    """提取并解析 ``window.__INITIAL_STATE__``；含 ``undefined`` 替换为 ``null``。"""
    i = html.find("window.__INITIAL_STATE__")
    if i < 0:
        return None
    j = html.find("{", i)
    if j < 0:
        return None
    raw = _scan_object(html, j)
    if raw is None:
        return None
    try:
        return json.loads(raw.replace("undefined", "null"))
    except json.JSONDecodeError:
        return None


def _find_note(state: dict[str, Any], note_id: str) -> dict[str, Any] | None:
    """从 state.note.noteDetailMap 取出对应笔记的 dict；取不到返回 None。"""
    ndm = ((state.get("note") or {}).get("noteDetailMap")) or {}
    entry = ndm.get(note_id)
    if not isinstance(entry, dict):
        return None
    inner = entry.get("note")
    note: dict[str, Any] = inner if isinstance(inner, dict) else entry
    # 必须有 type 才是真笔记；空 dict / 仅占位字段视为缺失
    if not note.get("type"):
        return None
    return note


def _clean_desc(text: str | None) -> str:
    if not text:
        return ""
    return _TOPIC_RE.sub(r"#\1#", text).strip()


def _ts_to_iso(ms: Any) -> str | None:
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000, tz=dt.UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _media_kind(note: dict[str, Any]) -> str:
    t = note.get("type")
    if t == "video":
        return "video"
    if note.get("imageList"):
        return "image"
    return "text"


def _quality(note: dict[str, Any]) -> float:
    """图文笔记正文通常很短（几十字），按 desc 长度分档。"""
    desc_len = len(_clean_desc(note.get("desc")))
    title = (note.get("title") or "").strip()
    if desc_len >= 100:
        return 0.85
    if desc_len >= _MIN_DESC_CHARS * 4:
        return 0.7
    if title:
        return 0.45
    return 0.25


def _build_markdown(note: dict[str, Any]) -> tuple[str, str, str | None, str | None, str]:
    """组装 markdown，返回 (markdown, title, author, published_at_iso, media_kind)。"""
    title = (note.get("title") or "").strip()
    desc = _clean_desc(note.get("desc"))
    user_obj = note.get("user")
    user = user_obj.get("nickname") if isinstance(user_obj, dict) else None
    ip = note.get("ipLocation")
    published = _ts_to_iso(note.get("time"))
    tags = [
        t.get("name") for t in (note.get("tagList") or []) if isinstance(t, dict) and t.get("name")
    ]
    interact = note.get("interactInfo") or {}
    images = note.get("imageList") or []
    kind = note.get("type")

    parts: list[str] = [f"# {title or '(无标题)'}\n"]

    meta_bits: list[str] = []
    if user:
        meta_bits.append(f"作者：{user}")
    if ip:
        meta_bits.append(ip)
    if meta_bits:
        parts.append("> " + " · ".join(meta_bits) + "\n")

    if desc:
        parts.append(desc + "\n")

    parts.append("\n---\n")

    facts: list[str] = []
    if kind:
        facts.append("类型：" + ("视频" if kind == "video" else "图文"))
    if images:
        facts.append(f"图片 {len(images)} 张（见原始快照）")
    if tags:
        facts.append("话题：" + " ".join(f"#{t}#" for t in tags))
    if any(interact.get(k) for k in ("likedCount", "collectedCount", "commentCount", "shareCount")):
        facts.append(
            f"互动：点赞 {interact.get('likedCount', '—')} · "
            f"收藏 {interact.get('collectedCount', '—')} · "
            f"评论 {interact.get('commentCount', '—')} · "
            f"分享 {interact.get('shareCount', '—')}"
        )
    if published:
        facts.append(f"发布：{published}")
    for f in facts:
        parts.append(f"- {f}\n")

    final_title = title or (desc[:30] + "…" if desc else None) or None
    md = "\n".join(parts).rstrip() + "\n"
    return md, final_title or "未命名笔记", user, published, _media_kind(note)


class XhsAdapter:
    input_type = "url"

    def __init__(self, fetcher: GuardedFetcher | None = None) -> None:
        self.fetcher = fetcher or GuardedFetcher()

    def can_handle(self, item: InboxItem) -> bool:
        if item.input_type != "url":
            return False
        try:
            return identify_platform(item.payload or "") is Platform.XHS
        except Exception:
            return False

    def extract(
        self,
        item: InboxItem,
        progress: Any = None,
    ) -> ExtractionResult:
        url = (item.payload or "").strip()
        if progress:
            progress("fetch", 0.2)

        # 短链 / 重定向 → 一次 fetch 即可同时拿到最终 URL + 内容（SSR 内联）
        page = self.fetcher.fetch(url)
        html = page.content.decode("utf-8", errors="replace")
        final_url = page.url

        note_id = _extract_note_id(final_url) or _extract_note_id(url)

        if note_id is None:
            # 短链未展开到笔记详情；可能是用户主页 / 发现页
            raise CaptureBlockedError(f"无法从小红书链接解析出笔记 ID：{url}")

        if "/404" in final_url:
            # 缺 token 或 token 过期 → 302 到 /404；明确告诉用户怎么修
            raise PermanentCaptureError(
                f"小红书链接无有效 xsec_token（{_HINT_NO_TOKEN}）。当前链接：{url}"
            )

        if progress:
            progress("extract", 0.6)

        state = _extract_initial_state(html)
        if state is None:
            raise PermanentCaptureError(
                f"小红书页面结构异常（未找到 __INITIAL_STATE__），可能页面改版或风控升级：{url}"
            )

        note = _find_note(state, note_id)
        if note is None:
            # 即便最终 URL 是 explore，若 noteDetailMap 为空，等同于 401
            raise PermanentCaptureError(
                f"小红书未返回笔记内容（可能 xsec_token 已过期，{_HINT_NO_TOKEN}）。当前链接：{url}"
            )

        if progress:
            progress("build", 0.85)

        markdown, title, author, published_at, kind = _build_markdown(note)

        # 原始快照：note JSON 即可（结构化、可重提取）；不存 65KB 的 HTML
        original = json.dumps({"note": note}, ensure_ascii=False).encode("utf-8")

        return ExtractionResult(
            original_bytes=original,
            original_filename="original.json",
            content_markdown=markdown,
            title=title,
            author=author,
            canonical_url=f"https://www.xiaohongshu.com/explore/{note_id}",
            published_at=published_at,
            extraction_method="xhs",
            extraction_quality=_quality(note),
            platform="xhs",
            media_kind=kind,  # type: ignore[arg-type]
            blocks=evidence_store.split_markdown_blocks(markdown),
        )
