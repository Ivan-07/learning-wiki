"""证据块构建与锚点稳定性测试（规格 5.2、13.3.1）。"""

from learning_wiki.domain import hashing
from learning_wiki.storage import evidence_store

SAMPLE = """# 标题一

第一段正文。

- 列表项 A
- 列表项 B

```python
print("代码块")
```

结尾段。"""


def test_split_blocks_types() -> None:
    blocks = evidence_store.split_markdown_blocks(SAMPLE)
    types = [b.anchor_type for b in blocks]
    assert types == ["heading", "paragraph", "list_item", "code", "paragraph"]


def test_build_content_and_evidence_roundtrip() -> None:
    blocks = evidence_store.split_markdown_blocks(SAMPLE)
    content, evs = evidence_store.build_content_with_anchors(
        "src_01JABCDEFGHXXXXZZZZZZZZWWW", "v0001", hashing.hash_text(SAMPLE), SAMPLE
    )
    assert len(evs) == len(blocks) == 5
    assert content.count("^ev-01JABCDEFGHXXXXZZZZZZZZWWW-v0001-") == 5
    # 无损性：剥离锚点行后逐行还原原文（写入统一补尾换行）
    assert evidence_store.strip_anchor_lines(content) == SAMPLE + "\n"
    # 每条证据：锚点在文件中存在且 span_hash 与块文本一致
    for ev in evs:
        assert evidence_store.verify_evidence(ev, content)


def test_span_hash_survives_anchor_append() -> None:
    """锚点行是系统追加的：剥离后 content_hash 与提取内容一致。"""
    content, _evs = evidence_store.build_content_with_anchors(
        "src_01JABCDEFGHXXXXZZZZZZZZWWW", "v0001", "sha256:x", SAMPLE
    )
    assert evidence_store.content_hash_of_stored(content) == hashing.hash_text(SAMPLE)


def test_block_lookup_returns_block_text() -> None:
    blocks = evidence_store.split_markdown_blocks(SAMPLE)
    content, evs = evidence_store.build_content_with_anchors(
        "src_01JABCDEFGHXXXXZZZZZZZZWWW", "v0001", "sha256:x", SAMPLE
    )
    assert evidence_store.find_block_text(content, evs[1].anchor_start) == blocks[1].text


def test_v2_reorder_keeps_v1_anchors_stable() -> None:  # 规格 13.3.1 精神
    """段落顺序改变不影响已有 v1 证据的验证（v1 文件本身不变）。"""
    content_v1, evs_v1 = evidence_store.build_content_with_anchors(
        "src_01JABCDEFGHXXXXZZZZZZZZWWW", "v0001", "sha256:x", SAMPLE
    )
    # v2: 顺序打乱，生成新的 v2 证据
    reordered = "\n\n".join(
        reversed([b.text for b in evidence_store.split_markdown_blocks(SAMPLE)])
    )
    content_v2, evs_v2 = evidence_store.build_content_with_anchors(
        "src_01JABCDEFGHXXXXZZZZZZZZWWW", "v0002", "sha256:y", reordered
    )
    assert all(evidence_store.verify_evidence(e, content_v1) for e in evs_v1)
    assert all(evidence_store.verify_evidence(e, content_v2) for e in evs_v2)
    assert evs_v1[0].evidence_id != evs_v2[0].evidence_id  # 重新提取不复用旧 ID
