"""GenericWebAdapter：公开网页 → HTML 快照 + trafilatura 规范化正文。

受限来源（登录、验证码、付费墙、反爬）停止自动处理，返回可解释降级，
允许用户改用粘贴文字或文件导入（规格 6.1）。
"""

from __future__ import annotations

from learning_wiki.adapters.net_guard import CaptureBlockedError, GuardedFetcher
from learning_wiki.domain.contracts import ExtractionResult, InboxItem
from learning_wiki.storage import evidence_store

_MIN_CONTENT_CHARS = 80


class GenericWebAdapter:
    input_type = "url"

    def __init__(self, fetcher: GuardedFetcher | None = None, store_html: bool = True) -> None:
        self.fetcher = fetcher or GuardedFetcher()
        self.store_html = store_html

    def can_handle(self, item: InboxItem) -> bool:
        return item.input_type == "url"

    def extract(self, item: InboxItem, progress=None) -> ExtractionResult:
        url = (item.payload or "").strip()
        if progress:
            progress("fetch", 0.3)
        page = self.fetcher.fetch(url)
        if page.status != 200:
            raise CaptureBlockedError(f"HTTP {page.status}，网页不可公开访问")
        if progress:
            progress("extract", 0.6)
        html = page.content.decode("utf-8", errors="replace")

        import trafilatura

        markdown = trafilatura.extract(
            html,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            with_metadata=True,
        )
        metadata = trafilatura.extract_metadata(html)

        if not markdown or len(markdown.strip()) < _MIN_CONTENT_CHARS:
            raise CaptureBlockedError(
                "无法提取正文（页面可能需要登录、受验证码/反爬保护或无实质文本），"
                "请改用粘贴文字或导入文件"
            )
        if progress:
            progress("blocks", 0.9)
        title = (metadata.title if metadata else None) or page.url
        # trafilatura 的 with_metadata 会把元数据 YAML frontmatter 留在正文开头；
        # 元数据已单独进入 manifest，不作为证据块
        body = _strip_frontmatter(markdown)
        return ExtractionResult(
            original_bytes=page.content if self.store_html else None,
            original_filename="original.html",
            content_markdown=body,
            title=title,
            author=(metadata.author if metadata else None),
            canonical_url=page.url,
            published_at=(metadata.date if metadata else None),
            extraction_method="trafilatura",
            extraction_quality=0.9 if len(body) >= 500 else 0.7,
            blocks=evidence_store.split_markdown_blocks(body),
        )


def _strip_frontmatter(markdown: str) -> str:
    if markdown.startswith("---\n"):
        end = markdown.find("\n---", 4)
        if end > 0:
            return markdown[end + 4 :].lstrip("\n")
    return markdown
