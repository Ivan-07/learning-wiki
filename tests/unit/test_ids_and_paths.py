"""ID 规则与路径防护测试（规格 13.1）。"""

import pytest

from learning_wiki.domain import ids
from learning_wiki.domain.contracts import VaultConfig
from learning_wiki.storage.vault_paths import PathForbiddenError, VaultPaths


def test_source_id_format_and_sortability() -> None:
    a, b = ids.new_source_id(), ids.new_source_id()
    assert a.startswith("src_") and b.startswith("src_")
    assert ids.is_valid_source_id(a)
    assert a < b  # ULID 时间可排序


def test_evidence_and_block_id_pair() -> None:
    src = "src_" + "01JABCDEFGHXXXXZZZZZZZZWW"  # 26 位 ULID
    ev = ids.evidence_id_for(src, "v0001", 42)
    block = ids.block_id_for(src, "v0001", 42)
    # evidence_id / block_id 使用完整 ULID（生成器保证唯一，截断会碰撞）
    assert ev == "ev_01JABCDEFGHXXXXZZZZZZZZWW_v0001_0042"
    assert block == "ev-01JABCDEFGHXXXXZZZZZZZZWW-v0001-0042"
    assert ids.evidence_id_to_block_id(ev) == block
    assert ids.is_valid_evidence_id(ev)


def test_same_millisecond_sources_do_not_collide() -> None:
    """同一毫秒内生成的两个 source_id，证据 ID 不得冲突（真实缺陷回归测试）。"""
    a, b = ids.new_source_id(), ids.new_source_id()
    ev_a = ids.evidence_id_for(a, "v0001", 1)
    ev_b = ids.evidence_id_for(b, "v0001", 1)
    assert ev_a != ev_b


def test_next_version_id() -> None:
    assert ids.next_version_id([]) == "v0001"
    assert ids.next_version_id(["v0001"]) == "v0002"
    assert ids.next_version_id(["v0001", "v0003"]) == "v0004"


BAD_PATHS = ["../escape.md", "/etc/passwd", "a/../../b.md"]


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_path_escape_blocked(tmp_path, bad: str) -> None:
    vp = VaultPaths(tmp_path, VaultConfig())
    with pytest.raises(PathForbiddenError):
        vp.resolve(bad)


def test_symlink_escape_blocked(tmp_path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside)
    vp = VaultPaths(tmp_path, VaultConfig())
    with pytest.raises(PathForbiddenError):
        vp.resolve("link/x.md")


def test_ensure_inside_allowed_dirs(tmp_path) -> None:
    vp = VaultPaths(tmp_path, VaultConfig())
    wiki = vp.folder("wiki")
    ok = vp.ensure_inside("30 Wiki/Concepts/a.md", [wiki])
    assert ok == tmp_path / "30 Wiki/Concepts/a.md"
    with pytest.raises(PathForbiddenError):
        vp.ensure_inside("20 Thoughts/a.md", [wiki])
