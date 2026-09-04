"""真实复杂 Markdown 的无损性回归测试。

背景：简单 fixture 测试全过，但真实文档（嵌套代码块、表格、复杂结构）
曾暴露「块列表重组丢空行 → content_hash/span_hash 校验失败」的缺陷。
此测试用真实规格书（1376 行、277 证据块）防回归。
"""

from tests.conftest import fixture_path


def test_real_world_document_lossless(vault) -> None:
    """捕获真实复杂文档后：lint 零错误（content_hash 与全部 span_hash 一致）。"""
    text = fixture_path("spec_real_world.md").read_text(encoding="utf-8")
    item = vault.capture_service.add_to_inbox("text", payload=text)
    result = vault.capture_service.process(item.item_id)

    assert result.evidence_count > 200  # 真实文档的证据块规模

    report = vault.integrity_service.lint()
    assert report.ok, [i.message for i in report.errors][:5]

    # 删库重建后依然无损（事实源完整性）
    import shutil

    from tests.conftest import make_vault

    vault.close()
    shutil.rmtree(vault.root / ".learning-wiki")
    ctx2 = make_vault(vault.root)
    report2 = ctx2.integrity_service.lint()
    assert report2.ok, [i.message for i in report2.errors][:5]
    ctx2.close()
