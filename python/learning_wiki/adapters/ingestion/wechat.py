"""WechatAdapter：微信公众号文章 → 规范化文本。

特点（与 B 站的差异）：
- 必须伪装桌面浏览器 UA；默认 UA 被微信 302 到「环境异常」验证码页；
- 存在两种页面形态：传统图文（``#js_content`` 存在，正文完整）与
  图片消息/小绿书（Vue SSR，``#js_content`` 不存在，正文由 JS 渲染）；
  判据只看 ``#js_content`` 是否存在——``appmsg_like_type`` 实测两种都是
  ``2``，不可作判据；
- canonical_url 归一化：优先 ``__biz+mid+idx+sn`` 四元组构造，让短链
  ``/s/<id>`` 与长链 ``/s?biz=...`` 落到同一个 source，避免重复入库；
- ``original_bytes`` 只存 ``#img-content`` 片段（约 20KB），不存整页 3MB+；
- 图片不下载（``mmbiz.qpic.cn`` 无防盗链，markdown 保留远程 URL 即可）；
- 自写 lxml→Markdown 转换器，不引入新依赖；section 嵌套按"无块级子元素时当段落"处理。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, parse_qsl, urlparse, urlunparse

from lxml import html as lhtml
from lxml.html import HtmlElement

from learning_wiki.adapters.net_guard import GuardedFetcher, PermanentCaptureError
from learning_wiki.domain.contracts import ExtractionResult, InboxItem
from learning_wiki.storage import evidence_store

# 必须伪装成桌面浏览器——微信对非浏览器 UA 一律跳验证码页（实测）。
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# 页面中出现这些字面字符串即视为不可用（验证码 / 已删除 / 违规）
_BLOCKED_MARKERS = (
    "环境异常",  # wappoc_appmsgcaptcha 验证码页
    "请在微信客户端打开链接",  # 反爬拦截
    "该内容已被发布者删除",
    "此内容因违规无法查看",
)

# 微信长链里的跟踪/临时参数；归一化时全部剥掉
_TRACKING_PARAMS = frozenset(
    {
        "chksm",
        "scene",
        "src",
        "timestamp",
        "ver",
        "signature",
        "exportkey",
        "pass_ticket",
        "clickfrom",
        "uin",
        "key",
        "devicetype",
        "version",
        "lang",
        "networktype",
        "abtest_cookie",
        "subscene",
    }
)


class WechatAdapter:
    """微信公众号文章适配器。"""

    input_type = "url"

    def __init__(self, fetcher: GuardedFetcher | None = None) -> None:
        self.fetcher = fetcher or GuardedFetcher()

    def can_handle(self, item: InboxItem) -> bool:
        return _looks_like_wechat_article(item.payload or "")

    def extract(self, item: InboxItem, progress=None) -> ExtractionResult:
        url = (item.payload or "").strip()
        if progress:
            progress("resolve", 0.1)

        final_url, html = self._fetch(url)
        _ensure_not_blocked(html)

        if progress:
            progress("parse", 0.4)
        tree = lhtml.fromstring(html)

        original_bytes = _extract_evidence_bytes(tree)
        meta = _extract_meta(tree, url, final_url)

        js_content = tree.xpath('//*[@id="js_content"]')
        if js_content:
            if progress:
                progress("extract", 0.6)
            body = _js_content_to_markdown(js_content[0])
            if len(body.strip()) < 30:
                # js_content 存在但几乎为空 → 退到 og 摘要
                body = meta.og_description or ""
                quality = 0.3
            else:
                quality = 0.9 if len(body) >= 500 else 0.7
        else:
            # 图集/SSR：js_content 不存在；正文在 JS 渲染层，HTTP 拿不到
            body = meta.og_description or ""
            quality = 0.3

        if not body or len(body.strip()) < 30:
            raise PermanentCaptureError(
                "无法提取正文（可能为图片消息类型，正文由 JS 渲染），请用浏览器打开后粘贴全文"
            )

        if progress:
            progress("blocks", 0.85)
        markdown = _build_markdown(meta, body)
        return ExtractionResult(
            original_bytes=original_bytes,
            original_filename="original.html",
            content_markdown=markdown,
            title=meta.title or "未命名公众号文章",
            author=meta.author,
            canonical_url=meta.canonical_url,
            published_at=meta.published_at,
            extraction_method="wechat",
            extraction_quality=quality,
            platform="wechat",
            media_kind="mixed" if _has_remote_image(body) else "text",
            blocks=evidence_store.split_markdown_blocks(markdown),
        )

    # -- 内部 --------------------------------------------------------------

    def _fetch(self, url: str) -> tuple[str, str]:
        page = self.fetcher.fetch(url, headers={"User-Agent": _BROWSER_UA})
        if page.status != 200:
            raise PermanentCaptureError(f"HTTP {page.status}，微信文章不可访问")
        return page.url, page.content.decode("utf-8", errors="replace")


# ============================================================================
# URL 路由辅助
# ============================================================================


def _looks_like_wechat_article(url: str) -> bool:
    """``mp.weixin.qq.com/s`` 或 ``/s/<id>`` 才算文章（其他 mp 路径属聚合/开放平台）。"""
    if not url:
        return False
    try:
        u = urlparse(url)
    except Exception:
        return False
    # urlparse 的结果用 .hostname（不是 httpx.URL 的 .host）
    host = (u.hostname or "").lower()
    if host != "mp.weixin.qq.com":
        return False
    path = u.path or ""
    return path == "/s" or path.startswith("/s/")


# ============================================================================
# 元数据提取 + canonical 归一化
# ============================================================================


def _ensure_not_blocked(html: str) -> None:
    for marker in _BLOCKED_MARKERS:
        if marker in html:
            raise PermanentCaptureError(
                f"微信页面不可用（{marker}）。请用浏览器打开后粘贴全文，或确认链接未被删除。"
            )


def _meta(tree: HtmlElement, prop: str) -> str | None:
    """读 ``<meta property="og:xxx" content="...">``，空字符串视为 None。"""
    for v in tree.xpath(f'//meta[@property="{prop}"]/@content'):
        s = (v or "").strip()
        if s:
            return s
    for v in tree.xpath(f'//meta[@name="{prop}"]/@content'):
        s = (v or "").strip()
        if s:
            return s
    return None


def _first_h1_text(tree: HtmlElement) -> str | None:
    for h in tree.xpath("//h1"):
        text = "".join(h.itertext()).strip()
        if text:
            return text
    return None


def _read_var(tree: HtmlElement, name: str) -> str | None:
    """从所有 ``<script>`` 文本里提取 ``var xxx = '...';``。"""
    pattern_single = re.compile(rf"var\s+{re.escape(name)}\s*=\s*'([^']*)'")
    pattern_double = re.compile(rf'var\s+{re.escape(name)}\s*=\s*"([^"]*)"')
    for script in tree.xpath("//script//text()"):
        m = pattern_single.search(script) or pattern_double.search(script)
        if m:
            return m.group(1) or None
    return None


def _extract_quadruples(
    tree: HtmlElement, original_url: str, final_url: str
) -> tuple[str | None, str | None, str | None, str | None]:
    """从 URL 参数 + 页面变量提取 ``(__biz, mid, idx, sn)``，缺一不可。"""
    # 优先 URL 参数（用户原始 URL 几乎一定带）
    for src in (original_url, final_url):
        params = _query_params(src)
        biz = params.get("__biz")
        mid = params.get("mid")
        idx = params.get("idx")
        sn = params.get("sn")
        if all([biz, mid, idx, sn]):
            return biz, mid, idx, sn
    # 其次页面变量（短链页面变量齐全，长链页面变量往往为空）
    return (
        _read_var(tree, "biz"),
        _read_var(tree, "mid"),
        _read_var(tree, "idx"),
        _read_var(tree, "sn"),
    )


def _query_params(url: str) -> dict[str, str]:
    try:
        return {k: v[0] for k, v in parse_qs(urlparse(url).query).items() if v}
    except Exception:
        return {}


def _canonicalize_wechat_url(url: str) -> str:
    """规范化微信文章 URL：剔除跟踪/临时参数。"""
    try:
        u = urlparse(url)
    except Exception:
        return url
    if "mp.weixin.qq.com" not in (u.netloc or "").lower():
        return url
    # 短链 /s/<id>：原样
    if u.path.startswith("/s/"):
        return urlunparse((u.scheme or "https", u.netloc, u.path, "", "", ""))
    # 长链 /s?...：剥跟踪参数
    if u.path == "/s":
        kept = [
            (k, v)
            for k, v in parse_qsl(u.query, keep_blank_values=False)
            if k not in _TRACKING_PARAMS
        ]
        from urllib.parse import urlencode

        new_query = urlencode(kept)
        return urlunparse((u.scheme or "https", u.netloc, u.path, "", new_query, ""))
    # 其它路径：原样
    return url


def _ts_to_iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).isoformat()


def _has_remote_image(md: str) -> bool:
    return bool(re.search(r"!\[[^\]]*\]\(https?://", md))


# ============================================================================
# evidence 字节（只存 #img-content 片段，不存整页）
# ============================================================================


def _extract_evidence_bytes(tree: HtmlElement) -> bytes | None:
    nodes = tree.xpath('//*[@id="img-content"]')
    if not nodes:
        nodes = tree.xpath('//*[@id="js_content"]')
    if not nodes:
        return None
    return lhtml.tostring(nodes[0], encoding="utf-8")


# ============================================================================
# Markdown 组装
# ============================================================================


def _build_markdown(meta: _Meta, body: str) -> str:
    parts: list[str] = [f"# {meta.title or '未命名公众号文章'}\n"]
    if meta.author:
        parts.append(f"> 公众号：{meta.author}\n")
    if meta.published_at:
        parts.append(f"> 发布时间：{meta.published_at[:10]}\n")
    if meta.og_image:
        parts.append(f"封面：{meta.og_image}\n")
    parts.append("")
    parts.append(body.strip())
    parts.append("")
    parts.append(f"\n原文链接：{meta.canonical_url}\n")
    return "\n".join(parts)


# ============================================================================
# DOM → Markdown 转换器
# ============================================================================
#
# 设计：
# - 块级元素用 _block_md 返回"已包含尾部换行的整块字符串"；
# - 内联元素用 _walk_inline 处理 strong/em/a/code/br；
# - section/div 容器：若子节点全是块级，递归；否则当段落；
# - table / pre / 列表各自特化处理；img → ![](url)（data-src 优先）。
# - 不引入新依赖（只有 lxml，trafilatura 已有但对微信不稳）。


def _js_content_to_markdown(node: HtmlElement) -> str:
    blocks = _collect_blocks(node)
    text = "\n\n".join(b for b in blocks if b.strip())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _collect_blocks(node: HtmlElement) -> list[str]:
    """收集块级子节点；若无块级子节点则当段落。"""
    out: list[str] = []
    for child in node:
        if not isinstance(child.tag, str):
            continue
        sub = _block_md(child)
        if sub is not None and sub.strip():
            out.append(sub.rstrip("\n"))
    if not out:
        text = _walk_children_inline(node).strip()
        if text:
            out.append(text)
    return out


def _block_md(node: HtmlElement) -> str | None:
    if not isinstance(node.tag, str):
        return None
    tag = node.tag.lower()
    if tag in ("script", "style"):
        return None
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(tag[1])
        text = _walk_children_inline(node).strip()
        return f"{'#' * level} {text}\n" if text else None
    if tag == "p":
        text = _walk_children_inline(node).strip()
        return f"{text}\n" if text else None
    if tag == "blockquote":
        inner_blocks = _collect_blocks(node)
        body = "\n\n".join(inner_blocks).strip()
        if not body:
            return None
        quoted = "\n".join(f"> {line}" for line in body.splitlines())
        return f"{quoted}\n"
    if tag == "pre":
        code_in_pre = node.xpath(".//code")
        if code_in_pre:
            lang = _detect_lang(code_in_pre[0])
            text = "".join(code_in_pre[0].itertext()).rstrip("\n")
            return f"```{lang}\n{text}\n```\n"
        text = "".join(node.itertext()).rstrip("\n")
        return f"```\n{text}\n```\n"
    if tag == "ul":
        items = []
        for li in node.xpath("./li"):
            inner = _collect_blocks(li) or [_walk_children_inline(li).strip()]
            text = "\n".join(inner).strip()
            if text:
                items.append(f"- {text}")
        return ("\n".join(items) + "\n") if items else None
    if tag == "ol":
        items = []
        for i, li in enumerate(node.xpath("./li"), 1):
            inner = _collect_blocks(li) or [_walk_children_inline(li).strip()]
            text = "\n".join(inner).strip()
            if text:
                items.append(f"{i}. {text}")
        return ("\n".join(items) + "\n") if items else None
    if tag == "table":
        return _table_to_markdown(node)
    if tag == "hr":
        return "---\n"
    if tag == "img":
        return _img_to_markdown(node)
    if tag in ("section", "div"):
        # 容器：递归收集块；若无则当段落
        inner = _collect_blocks(node)
        return ("\n\n".join(inner) + "\n") if inner else None
    # 其它：按内联处理
    text = _walk_children_inline(node).strip()
    return f"{text}\n" if text else None


def _walk_children_inline(node: HtmlElement) -> str:
    """拼接直接子节点：text + 子节点 inline + tail。"""
    out: list[str] = []
    if node.text:
        out.append(node.text)
    for child in node:
        if not isinstance(child.tag, str):
            continue
        sub = _walk_inline(child)
        if sub:
            out.append(sub)
        if child.tail:
            out.append(child.tail)
    return "".join(out)


def _walk_inline(node: HtmlElement) -> str:
    """递归内联 → Markdown。"""
    if not isinstance(node.tag, str):
        return ""
    tag = node.tag.lower()
    if tag == "br":
        return "  \n"
    if tag in ("script", "style"):
        return ""
    if tag in ("strong", "b"):
        inner = _walk_children_inline(node).strip()
        if not inner:
            return ""
        # 纯装饰 strong（如微信标题里夹的 `<strong>/</strong>` 序号括弧）
        if len(inner) <= 4 and re.fullmatch(r"[\s\W_]+", inner):
            return ""
        return f"**{inner}**"
    if tag in ("em", "i"):
        inner = _walk_children_inline(node).strip()
        return f"*{inner}*" if inner else ""
    if tag == "code":
        text = "".join(node.itertext())
        return f"`{text}`" if text else ""
    if tag == "a":
        text = _walk_children_inline(node).strip()
        href = node.get("href") or ""
        return f"[{text}]({href})" if (text and href) else text
    if tag == "img":
        # 段落内嵌图片（<p><img></p>）：微信是懒加载，data-src 才是真实地址
        src = node.get("data-src") or node.get("src")
        if not src:
            return ""
        alt = node.get("alt") or ""
        return f"![{alt}]({src})"
    return _walk_children_inline(node)


def _img_to_markdown(node: HtmlElement) -> str | None:
    src = node.get("data-src") or node.get("src")
    if not src:
        return None
    alt = node.get("alt") or ""
    return f"![{alt}]({src})\n"


def _table_to_markdown(node: HtmlElement) -> str | None:
    rows: list[list[str]] = []
    for tr in node.xpath(".//tr"):
        cells: list[str] = []
        for c in tr.xpath("./td | ./th"):
            inner = _walk_children_inline(c).strip().replace("\n", " ").replace("|", "\\|")
            cells.append(inner)
        if cells:
            rows.append(cells)
    if not rows:
        return None
    ncols = len(rows[0])
    out = [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join("---" for _ in range(ncols)) + " |",
    ]
    for r in rows[1:]:
        while len(r) < ncols:
            r.append("")
        out.append("| " + " | ".join(r[:ncols]) + " |")
    return "\n".join(out) + "\n"


def _detect_lang(code_node: HtmlElement) -> str:
    """从 ``class="language-python"`` 取语言标签。

    注意：lxml 的 ``get("class")`` 返回**字符串**（空格分隔），不是列表。
    """
    raw = code_node.get("class")
    if not raw:
        return ""
    cls = raw if isinstance(raw, str) else " ".join(raw)
    m = re.search(r"language-([\w+-]+)", cls)
    return m.group(1) if m else ""


# ============================================================================
# 内部数据结构
# ============================================================================


@dataclass(frozen=True)
class _Meta:
    title: str | None
    author: str | None
    published_at: str | None
    canonical_url: str
    og_description: str | None
    og_image: str | None


def _extract_meta(tree: HtmlElement, original_url: str, final_url: str) -> _Meta:
    title = _meta(tree, "og:title") or _first_h1_text(tree)
    og_desc = _meta(tree, "og:description")
    og_img = _meta(tree, "og:image")
    og_url = _meta(tree, "og:url")

    author_nodes = tree.xpath('//a[@id="js_name"]//text()')
    author = next((s.strip() for s in author_nodes if s.strip()), None)

    ct = _read_var(tree, "ct")
    published_at = _ts_to_iso(int(ct)) if (ct and ct.isdigit()) else None

    biz, mid, idx, sn = _extract_quadruples(tree, original_url, final_url)
    if all([biz, mid, idx, sn]):
        canonical_url = f"https://mp.weixin.qq.com/s?__biz={biz}&mid={mid}&idx={idx}&sn={sn}"
    else:
        canonical_url = _canonicalize_wechat_url(og_url or final_url)

    return _Meta(
        title=title,
        author=author,
        published_at=published_at,
        canonical_url=canonical_url,
        og_description=(og_desc.replace("\\x0a", "\n") if og_desc else None),
        og_image=og_img,
    )
