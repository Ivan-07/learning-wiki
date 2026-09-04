"""V0 端到端验收路径（方案 §7）：CLI 驱动的完整闭环。

init-vault → inbox add → process → search → evidence show
→ 删除 .learning-wiki/ → rebuild → search 结果一致
"""

import shutil

from typer.testing import CliRunner

from learning_wiki.cli.app import app
from tests.conftest import fixture_path

runner = CliRunner()


def test_v0_vertical_slice(tmp_path):
    vault = tmp_path / "TestVault"

    # 1. init-vault：空 Vault
    r = runner.invoke(app, ["init-vault", str(vault)])
    assert r.exit_code == 0, r.output
    assert (vault / "_System/Learning Wiki/Config.yaml").exists()
    assert (vault / "AGENTS.md").exists()
    assert (vault / "CLAUDE.md").exists()

    # 2. init-vault：已有 Vault 幂等，且不动既有笔记
    existing = vault / "20 Thoughts/我的想法.md"
    existing.write_text("# 我的想法\n\n用户自己的笔记。", encoding="utf-8")
    r = runner.invoke(app, ["init-vault", str(vault)])
    assert r.exit_code == 0
    assert existing.read_text(encoding="utf-8").startswith("# 我的想法")

    # 3. inbox add（文字）
    text = fixture_path("chinese_text.txt").read_text(encoding="utf-8")
    r = runner.invoke(app, ["inbox", "add", "--text", text], env={"LW_VAULT": str(vault)})
    assert r.exit_code == 0, r.output
    item_id = r.output.split("已加入 Inbox:")[1].split()[0].strip()

    # 4. inbox process
    r = runner.invoke(app, ["inbox", "process", item_id], env={"LW_VAULT": str(vault)})
    assert r.exit_code == 0, r.output
    assert "已捕获" in r.output
    _source_id = r.output.split("已捕获")[1].split()[0]

    # 5. search
    r = runner.invoke(app, ["search", "检索练习", "--trace"], env={"LW_VAULT": str(vault)})
    assert r.exit_code == 0, r.output
    assert "ev_" in r.output  # 结果含 Evidence ID

    # 6. evidence show
    import re

    m = re.search(r"ev_[0-9A-Za-z]+_v\d{4}_\d{4}", r.output)
    assert m, r.output
    r = runner.invoke(app, ["evidence", "show", m.group(0)], env={"LW_VAULT": str(vault)})
    assert r.exit_code == 0, r.output
    assert "span_hash" in r.output and "一致" in r.output

    # 7. lint / doctor
    r = runner.invoke(app, ["lint"], env={"LW_VAULT": str(vault)})
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["doctor"], env={"LW_VAULT": str(vault)})
    assert r.exit_code == 0

    # 8. 删除派生库 → rebuild → 搜索同样命中
    first_search = runner.invoke(app, ["search", "检索练习"], env={"LW_VAULT": str(vault)}).output
    shutil.rmtree(vault / ".learning-wiki")
    r = runner.invoke(app, ["rebuild"], env={"LW_VAULT": str(vault)})
    assert r.exit_code == 0, r.output
    assert "重建完成" in r.output
    second_search = runner.invoke(app, ["search", "检索练习"], env={"LW_VAULT": str(vault)}).output
    assert m.group(0) in second_search  # 同一 Evidence ID 命中
    assert first_search.count("ev_") >= 1
