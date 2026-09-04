"""完整性对抗测试（规格 13.3.6、13.3.10）。"""

import pytest

from learning_wiki.storage.source_repository import UnsupportedSchemaError
from tests.conftest import fixture_path


def _capture(vault):
    text = fixture_path("chinese_text.txt").read_text(encoding="utf-8")
    item = vault.capture_service.add_to_inbox("text", payload=text)
    return vault.capture_service.process(item.item_id)


def test_tampered_content_detected_by_hash(vault) -> None:
    """修改来源文件后，内容哈希仍能发现篡改（13.3.6）。"""
    result = _capture(vault)
    manifest = vault.source_repository.load(result.source_id)
    content = (
        vault.source_repository.source_dir(result.source_id) / manifest.versions[0].content_path
    )
    # 篡改正文（保持锚点行结构，伪造 mtime）
    import os

    text = content.read_text(encoding="utf-8")
    content.write_text(text.replace("测试效应", "被篡改的结论"), encoding="utf-8")
    stat = os.stat(content)
    os.utime(content, (stat.st_atime, stat.st_mtime))

    report = vault.integrity_service.lint()
    kinds = {i.kind for i in report.errors}
    assert "content_hash_mismatch" in kinds


def test_tampered_original_detected(vault) -> None:
    result = _capture(vault)
    manifest = vault.source_repository.load(result.source_id)
    original = (
        vault.source_repository.source_dir(result.source_id)
        / manifest.versions[0].original_paths[0]
    )
    original.write_bytes("被篡改的原件".encode())
    report = vault.integrity_service.lint()
    assert "original_hash_mismatch" in {i.kind for i in report.errors}


def test_span_tamper_detected(vault) -> None:
    """改写证据对应的内容块（保持锚点行）→ span_hash 不一致。"""
    result = _capture(vault)
    manifest = vault.source_repository.load(result.source_id)
    content_file = (
        vault.source_repository.source_dir(result.source_id) / manifest.versions[0].content_path
    )
    lines = content_file.read_text(encoding="utf-8").split("\n")
    # 修改第一个块 ID 行之上的块文本
    for i, line in enumerate(lines):
        if line.startswith("^ev-"):
            lines[i - 1] = "被改写的段落文本，与 span_hash 不再一致。"
            break
    content_file.write_text("\n".join(lines), encoding="utf-8")
    report = vault.integrity_service.lint()
    assert "span_hash_mismatch" in {i.kind for i in report.errors}


def test_future_schema_version_readonly(vault) -> None:
    """无法识别的未来 schema 版本：只读打开，不得降级覆盖（13.3.10）。"""
    result = _capture(vault)
    mpath = vault.source_repository.manifest_path(result.source_id)
    text = mpath.read_text(encoding="utf-8")
    mpath.write_text(text.replace("schema_version: 1", "schema_version: 99", 1), encoding="utf-8")

    # repo 拒绝加载写入路径（只读）
    with pytest.raises(UnsupportedSchemaError):
        vault.source_repository.load(result.source_id)
    # lint 给出告警（不 error：只读打开是合法状态）
    report = vault.integrity_service.lint()
    assert "schema_version_unsupported" in {i.kind for i in report.issues}
