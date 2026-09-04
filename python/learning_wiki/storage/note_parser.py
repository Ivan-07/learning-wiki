"""Wiki 页面解析：frontmatter、Claim Block、证据引用。

Claim Block 约定（规格 5.3）：

    结论文字…… [[10 Sources/src_x/versions/v0001/content#^ev-…]]
    <!-- lw:claim status=supported valid_at=2026-09-03 review_after=2027-09-03 -->

    ^claim-retrieval-001

解析产出 ClaimInfo（claims / claim_evidence 反向引用索引的来源）。
"""

from __future__ import annotations

import re

from learning_wiki.domain import ids
from learning_wiki.domain.contracts import ClaimInfo


def parse_frontmatter(text: str) -> dict:
    """解析 ``---`` 包围的 YAML frontmatter；无则返回空 dict。"""
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    from learning_wiki.storage import yaml_io

    return yaml_io.load_yaml_str(text[4:end]) or {}


_CLAIM_COMMENT = re.compile(r"<!--\s*lw:claim\s+(.*?)\s*-->")
_CLAIM_BLOCK = re.compile(r"^\^([A-Za-z0-9-]+)\s*$")
_EVIDENCE_LINK = re.compile(r"\[\[10 Sources/[^\]]*?#\^([A-Za-z0-9-]+)[^\]]*\]\]")
_KV = re.compile(r"([A-Za-z_]+)=([^\s]+)")


def parse_claims(text: str, note_id: str) -> list[ClaimInfo]:
    """解析全部 Claim Block。"""
    lines = text.split("\n")
    claims: list[ClaimInfo] = []
    i = 0
    while i < len(lines):
        m = _CLAIM_COMMENT.search(lines[i])
        if not m:
            i += 1
            continue
        attrs = dict(_KV.findall(m.group(1)))
        # 注释之后找最近的块 ID 行（跳过空行）
        claim_block_id = None
        j = i + 1
        while j < len(lines) and j <= i + 3:
            bm = _CLAIM_BLOCK.match(lines[j].strip())
            if bm:
                claim_block_id = bm.group(1)
                break
            if lines[j].strip():
                break
            j += 1
        if claim_block_id is None:
            i += 1
            continue
        # 注释之前（可能隔空行）的段落 = Claim 正文；向上跳过空行后收集连续非空行
        k = i - 1
        while k >= 0 and not lines[k].strip():
            k -= 1
        block: list[str] = []
        while (
            k >= 0
            and lines[k].strip()
            and not _CLAIM_BLOCK.match(lines[k].strip())
            and not lines[k].startswith("---")
        ):
            block.insert(0, lines[k])
            k -= 1
        claim_text = "\n".join(block).strip()
        citations = []
        for block_id in _EVIDENCE_LINK.findall(claim_text):
            evidence_id = ids.block_id_to_evidence_id(block_id)
            if evidence_id not in citations:
                citations.append(evidence_id)
        # status=supported 但无引用 → 按 inference 处理（诚实不变量）
        status = attrs.get("status", "supported")
        if status == "supported" and not citations:
            status = "inference"
        claims.append(
            ClaimInfo(
                claim_block_id=claim_block_id,
                note_id=note_id,
                claim_status=status,
                valid_at=attrs.get("valid_at"),
                review_after=attrs.get("review_after"),
                citations=citations,
                text=claim_text,
            )
        )
        i = j + 1
    return claims
