"""EvidenceService：证据块构建、校验与解析。

核心保证（真实复杂 Markdown 上验证过的教训）：
- **无损性**：content.md = 原文按行插入锚点行；剥离锚点行后逐行还原原文，
  否则 content_hash 校验必然失败（不能从「块列表」重组——块间空行数会丢失）；
- **对称性**：校验侧用「剥离锚点 → 重新分块 → 按序号对应」还原块文本，
  与写入侧完全对称，天然 fence/空行/复杂结构安全；
- span_hash 绑定块原文（fence 感知分段，代码块为单一块）。
"""

from __future__ import annotations

import re
from pathlib import Path

from learning_wiki.domain import hashing, ids
from learning_wiki.domain.contracts import EvidenceRef, RawBlock, SourceManifest

# 精确锚点格式：^ev-{26 位 ULID}-v0001-0001。
# 用完整格式（而非宽松的 ^ev-…）区分系统锚点与原文自指的示例行
# （如保存"关于本系统的文档"时，其代码块里就有 ^ev-… 示例）。
_EV_ANCHOR_LINE = re.compile(r"^\^(ev-[0-9A-HJKMNP-TV-Z]{26}-v\d{4}-\d{4})\s*$")
_ANY_ANCHOR_LINE = _EV_ANCHOR_LINE


def strip_anchor_lines(text: str) -> str:
    """剥离系统追加的证据锚点行（剥离后逐行还原原文）。"""
    return "\n".join(line for line in text.split("\n") if not _EV_ANCHOR_LINE.match(line))


def content_hash_of_stored(text: str) -> str:
    """落盘 content.md（含锚点行）→ 与 manifest content_hash 可比的哈希。"""
    return hashing.hash_text(strip_anchor_lines(text))


# ---------------------------------------------------------------------------
# 分块（fence 感知、行号定位）
# ---------------------------------------------------------------------------


def _classify(first_line: str) -> str:
    first = first_line.lstrip()
    if first.startswith("#"):
        return "heading"
    if first.startswith(">"):
        return "quote"
    if first.startswith(("- ", "* ")) or re.match(r"^\d+\. ", first):
        return "list_item"
    if first.startswith("```"):
        return "code"
    return "paragraph"


def _split_ranges(lines: list[str]) -> list[tuple[int, int, str]]:
    """返回 [(start, end, anchor_type)]；end 含端点。fenced code 为单一块。"""
    blocks: list[tuple[int, int, str]] = []
    in_fence = False
    fence_start: int | None = None
    start: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_fence:
                blocks.append((fence_start or i, i, "code"))
                in_fence = False
                fence_start = None
            else:
                if start is not None:
                    blocks.append((start, i - 1, _classify(lines[start])))
                    start = None
                in_fence = True
                fence_start = i
            continue
        if not in_fence:
            if not stripped:
                if start is not None:
                    blocks.append((start, i - 1, _classify(lines[start])))
                    start = None
            elif start is None:
                start = i

    if in_fence and fence_start is not None:
        blocks.append((fence_start, len(lines) - 1, "code"))
    elif start is not None:
        blocks.append((start, len(lines) - 1, _classify(lines[start])))

    # 过滤全空白块（防御性；构造上不应出现）
    return [(s, e, k) for s, e, k in blocks if "\n".join(lines[s : e + 1]).strip()]


def split_markdown_blocks(markdown: str) -> list[RawBlock]:
    """按空行切块（fence 感知）；块文本取原文精确行切片（无损）。"""
    lines = markdown.split("\n")
    return [
        RawBlock(
            anchor_type=kind,  # type: ignore[arg-type]
            text="\n".join(lines[s : e + 1]),
        )
        for s, e, kind in _split_ranges(lines)
    ]


# ---------------------------------------------------------------------------
# 构建带锚点的 content.md
# ---------------------------------------------------------------------------


def build_content_with_anchors(
    source_id: str,
    version_id: str,
    content_hash: str,
    markdown: str,
) -> tuple[str, list[EvidenceRef]]:
    """在原文每个块末尾插入锚点行，生成 content.md 与 EvidenceRef 列表。

    直接对原文行号操作（**不经过块列表重组**，块间空行结构完整保留）；
    从后往前插入使行号不失效——剥离锚点行后逐行还原原文。
    """
    lines = markdown.split("\n")
    ranges = _split_ranges(lines)
    evidence: list[EvidenceRef] = []
    for seq in range(len(ranges), 0, -1):
        s, e, kind = ranges[seq - 1]
        block_id = ids.block_id_for(source_id, version_id, seq)
        evidence_id = ids.evidence_id_for(source_id, version_id, seq)
        block_text = "\n".join(lines[s : e + 1])
        lines.insert(e + 1, f"^{block_id}")
        evidence.insert(
            0,
            EvidenceRef(
                evidence_id=evidence_id,
                source_id=source_id,
                version_id=version_id,
                content_hash=content_hash,
                span_hash=hashing.span_hash(block_text),
                anchor_type=kind,
                anchor_start=block_id,
                text=block_text,
            ),
        )
    return "\n".join(lines) + "\n", evidence


def load_evidence_file(path: Path) -> list[EvidenceRef]:
    result: list[EvidenceRef] = []
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            result.append(EvidenceRef.model_validate_json(line))
    return result


# ---------------------------------------------------------------------------
# 校验（与写入侧对称）
# ---------------------------------------------------------------------------


def _anchor_order(content: str) -> dict[str, int]:
    """扫描锚点行，返回 {block_id: 序号}（第 n 个锚点 = 第 n 块）。"""
    order: dict[str, int] = {}
    n = 0
    for line in content.split("\n"):
        m = _ANY_ANCHOR_LINE.match(line.strip())
        if m:
            n += 1
            order[m.group(1)] = n
    return order


def find_block_text(content: str, block_id: str) -> str | None:
    """定位块 ID 对应的块文本：剥离锚点 → 重新分块 → 按序号对应。"""
    seq = _anchor_order(content).get(block_id)
    if seq is None:
        return None
    blocks = split_markdown_blocks(strip_anchor_lines(content))
    if seq > len(blocks):
        return None
    return blocks[seq - 1].text


def verify_evidence(ev: EvidenceRef, content: str) -> bool:
    """span_hash 必须与目标块一致（锚点稳定性验证）。"""
    text = find_block_text(content, ev.anchor_start)
    if text is None:
        return False
    return hashing.span_hash(text) == ev.span_hash


def content_path_for(manifest: SourceManifest, version_id: str) -> Path | None:
    for rec in manifest.versions:
        if rec.version_id == version_id:
            return Path(rec.content_path)
    return None


class EvidenceService:
    """CaptureService 使用的薄门面（模块函数的组合根）。"""

    def build(
        self, source_id: str, version_id: str, content_hash: str, markdown: str
    ) -> tuple[str, list[EvidenceRef]]:
        return build_content_with_anchors(source_id, version_id, content_hash, markdown)

    @staticmethod
    def load(path: Path) -> list[EvidenceRef]:
        return load_evidence_file(path)

    @staticmethod
    def verify(ev: EvidenceRef, content: str) -> bool:
        return verify_evidence(ev, content)
