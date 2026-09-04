"""M2 集成测试：提案校验、安全写入、崩溃恢复、Git。

对应规格 13.2 / 13.3：
- Wiki 提案只能引用存在的 Evidence；
- 用户拒绝提案后目标文件不变；
- 目标文件被用户修改后，旧提案无法应用；
- 提案应用每个步骤后强制终止进程，重启后可恢复或安全停止；
- Git commit 不包含用户已有的无关修改。
"""

import subprocess
import sys

import pytest

from learning_wiki.domain import hashing, ids
from learning_wiki.domain.contracts import ChangeProposal, ProposalOperation
from learning_wiki.proposals.applier import ProposalApplyError
from tests.conftest import fixture_path


def capture_text(vault):
    text = fixture_path("chinese_text.txt").read_text(encoding="utf-8")
    item = vault.capture_service.add_to_inbox("text", payload=text)
    return vault.capture_service.process(item.item_id)


def first_evidence_id(vault, source_id: str) -> str:
    return vault.conn.execute(
        "SELECT evidence_id FROM evidence WHERE source_id = ? LIMIT 1", (source_id,)
    ).fetchone()[0]


def wiki_note_content(evidence_id: str) -> str:
    return f"""---
schema_version: 1
note_id: concept_retrieval_practice
type: concept
title: 检索练习
status: reviewed
created_at: 2026-09-03
updated_at: 2026-09-03
---

检索练习能够改善延迟保持。[[10 Sources/x/versions/v0001/content#^{evidence_id.replace("_", "-")}]]

<!-- lw:claim status=supported valid_at=2026-09-03 review_after=2027-09-03 -->

^claim-retrieval-001
"""


def make_proposal(vault, path: str, content: str, evidence_id: str) -> ChangeProposal:
    return ChangeProposal(
        proposal_id=ids.new_proposal_id(),
        created_at=vault.clock.now_iso(),
        created_by="agent",
        reason="新来源补充了检索练习的适用边界",
        trigger_source_versions=[],
        cited_evidence_ids=[evidence_id],
        risk="medium",
        operations=[
            ProposalOperation(
                operation="create",
                path=path,
                result_hash=hashing.hash_bytes(content.encode("utf-8")),
                content=content,
            )
        ],
    )


class TestProposalValidation:
    def test_valid_create_proposal(self, vault) -> None:
        r = capture_text(vault)
        ev = first_evidence_id(vault, r.source_id)
        p = make_proposal(vault, "30 Wiki/Concepts/检索练习.md", wiki_note_content(ev), ev)
        v = vault.proposal_validator.validate(p)
        assert v.errors == [], v.errors

    def test_proposal_with_missing_evidence_rejected(self, vault) -> None:
        """无证据的 Agent 提案不能通过验证（规格 13.3.9）。"""
        p = make_proposal(vault, "30 Wiki/Concepts/x.md", "正文\n", "ev_NONEXIST_v0001_0001")
        v = vault.proposal_validator.validate(p)
        assert "引用的 Evidence 不存在: ev_NONEXIST_v0001_0001" in v.errors
        assert v.citations_valid is False

    def test_path_outside_wiki_rejected(self, vault) -> None:
        r = capture_text(vault)
        ev = first_evidence_id(vault, r.source_id)
        p = make_proposal(vault, "20 Thoughts/私有.md", "正文\n", ev)
        v = vault.proposal_validator.validate(p)
        assert any("路径不合法" in e for e in v.errors)
        assert v.paths_valid is False

    def test_base_hash_must_match_current(self, vault) -> None:
        """基于过期内容生成的提案：base_hash 校验失败 + expired_conflict。"""
        path = "30 Wiki/Concepts/检索练习.md"
        content = wiki_note_content("ev_x_v0001_0001")
        target = vault.paths.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        # 用户随后修改了文件 → 提案的 base_hash 过期
        target.write_text(content + "\n用户后来加的一行。\n", encoding="utf-8")
        stale_op = ProposalOperation(
            operation="patch",
            path=path,
            base_hash=hashing.hash_text(content),
            result_hash=hashing.hash_bytes((content + "\n新增。\n").encode("utf-8")),
            patch="",  # 内容无所谓，base_hash 已过期
        )
        p = ChangeProposal(
            proposal_id=ids.new_proposal_id(),
            created_at=vault.clock.now_iso(),
            reason="stale",
            operations=[stale_op],
        )
        v = vault.proposal_validator.validate(p)
        assert any("base_hash 过期" in e for e in v.errors)
        assert v.base_hashes_valid is False


class TestApply:
    def test_apply_creates_note_and_indexes_claims(self, vault) -> None:
        r = capture_text(vault)
        ev = first_evidence_id(vault, r.source_id)
        path = "30 Wiki/Concepts/检索练习.md"
        p = make_proposal(vault, path, wiki_note_content(ev), ev)
        vault.proposal_repository.save(p)

        outcome = vault.proposal_applier.apply(p.proposal_id)
        assert outcome.applied
        # 文件写入
        target = vault.paths.resolve(path)
        assert target.exists()
        assert "检索练习" in target.read_text(encoding="utf-8")
        # 提案移到 applied/
        assert vault.proposal_repository.state_of(p.proposal_id) == "applied"
        # claims 反向引用索引
        claim = vault.conn.execute(
            "SELECT * FROM claims WHERE claim_block_id = 'claim-retrieval-001'"
        ).fetchone()
        assert claim is not None
        ce = vault.conn.execute(
            "SELECT * FROM claim_evidence WHERE claim_block_id = 'claim-retrieval-001'"
        ).fetchone()
        assert ce is not None and ce["evidence_id"] == ev
        # Wiki 可被搜索
        assert len(vault.search_service.search("检索练习").hits) >= 1

    def test_reject_leaves_target_unchanged(self, vault) -> None:
        """用户拒绝提案后目标文件不变（规格 13.2）。"""
        r = capture_text(vault)
        ev = first_evidence_id(vault, r.source_id)
        p = make_proposal(vault, "30 Wiki/Concepts/x.md", wiki_note_content(ev), ev)
        vault.proposal_repository.save(p)
        vault.proposal_repository.move_to(p.proposal_id, "rejected")
        assert not (vault.paths.resolve("30 Wiki/Concepts/x.md")).exists()
        assert vault.proposal_repository.state_of(p.proposal_id) == "rejected"

    def test_apply_conflict_when_target_modified(self, vault) -> None:
        """目标文件被用户修改后，旧提案无法应用（哈希冲突阻止覆盖）。"""
        r = capture_text(vault)
        ev = first_evidence_id(vault, r.source_id)
        path = "30 Wiki/Concepts/检索练习.md"
        content = wiki_note_content(ev)
        p = make_proposal(vault, path, content, ev)
        vault.proposal_repository.save(p)
        # 用户在 apply 前手动创建了同名文件
        target = vault.paths.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("用户自己的版本。\n", encoding="utf-8")

        with pytest.raises(ProposalApplyError):
            vault.proposal_applier.apply(p.proposal_id)
        # 目标未被覆盖
        assert target.read_text(encoding="utf-8") == "用户自己的版本。\n"
        assert vault.proposal_repository.state_of(p.proposal_id) == "pending"

    def test_move_to_archive(self, vault) -> None:
        r = capture_text(vault)
        ev = first_evidence_id(vault, r.source_id)
        path = "30 Wiki/Concepts/检索练习.md"
        content = wiki_note_content(ev)
        p = make_proposal(vault, path, content, ev)
        vault.proposal_repository.save(p)
        vault.proposal_applier.apply(p.proposal_id)
        # 第二个提案：归档该页面
        target = vault.paths.resolve(path)
        p2 = ChangeProposal(
            proposal_id=ids.new_proposal_id(),
            created_at=vault.clock.now_iso(),
            reason="过期归档",
            operations=[
                ProposalOperation(
                    operation="move_to_archive",
                    path=path,
                    base_hash=hashing.hash_bytes(target.read_bytes()),
                    result_hash=hashing.hash_bytes(target.read_bytes()),
                )
            ],
        )
        vault.proposal_repository.save(p2)
        vault.proposal_applier.apply(p2.proposal_id)
        assert not target.exists()
        archived = vault.paths.folder("archive") / "检索练习.md"
        assert archived.exists()


CRASH_SCRIPT = """
import sys
sys.path.insert(0, {python_path!r})
from pathlib import Path
from tests.conftest import make_vault
from learning_wiki.domain.clock import FrozenClock
from datetime import datetime

vault = make_vault(Path({vault_root!r}), clock=FrozenClock(datetime(2026, 9, 3)))
proposal_id = {proposal_id!r}
vault.proposal_applier.apply(proposal_id)
print("completed")
"""


class TestCrashRecovery:
    """每个步骤后强制终止进程，重启后可恢复或安全停止（规格 13.3.4）。"""

    def _run_with_crash(self, vault, proposal_id: str, crash_point: str) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                CRASH_SCRIPT.format(
                    python_path="python",
                    vault_root=str(vault.root),
                    proposal_id=proposal_id,
                ),
            ],
            capture_output=True,
            text=True,
            cwd=".",
            env={"LW_TEST_CRASH_POINT": crash_point, "PATH": "/usr/bin:/bin"},
        )
        # 崩溃注入生效 → 进程被杀（137）；否则正常完成
        assert proc.returncode in (137, 0), proc.stderr

    def _apply_in_subprocess(self, vault, proposal_id: str) -> None:
        self._run_with_crash(vault, proposal_id, "none")

    def test_crash_after_step0_then_recover(self, vault) -> None:
        r = capture_text(vault)
        ev = first_evidence_id(vault, r.source_id)
        path = "30 Wiki/Concepts/检索练习.md"
        # 两个 create 步骤的提案（两个文件）
        p = ChangeProposal(
            proposal_id=ids.new_proposal_id(),
            created_at=vault.clock.now_iso(),
            reason="两个文件",
            cited_evidence_ids=[ev],
            operations=[
                ProposalOperation(
                    operation="create",
                    path=path,
                    result_hash=hashing.hash_bytes(wiki_note_content(ev).encode("utf-8")),
                    content=wiki_note_content(ev),
                ),
                ProposalOperation(
                    operation="create",
                    path="30 Wiki/Concepts/第二页.md",
                    result_hash=hashing.hash_bytes("第二页内容\n".encode()),
                    content="第二页内容\n",
                ),
            ],
        )
        vault.proposal_repository.save(p)
        vault.close()

        # 崩溃：第一个文件写入后
        self._run_with_crash(vault, p.proposal_id, "apply:after_step_0")

        # 重启 → 恢复扫描 → 幂等续跑完成
        from datetime import datetime

        from learning_wiki.domain.clock import FrozenClock
        from tests.conftest import make_vault

        ctx2 = make_vault(vault.root, clock=FrozenClock(datetime(2026, 9, 3)))
        reports = ctx2.proposal_recovery.scan_and_recover()
        assert reports and reports[0].outcome == "resumed_applied"
        assert ctx2.paths.resolve(path).exists()
        assert ctx2.paths.resolve("30 Wiki/Concepts/第二页.md").exists()
        assert ctx2.proposal_repository.state_of(p.proposal_id) == "applied"
        ctx2.close()

    def test_crash_before_any_step_resets_pending(self, vault) -> None:
        r = capture_text(vault)
        ev = first_evidence_id(vault, r.source_id)
        p = make_proposal(vault, "30 Wiki/Concepts/检索练习.md", wiki_note_content(ev), ev)
        vault.proposal_repository.save(p)
        vault.close()

        self._run_with_crash(vault, p.proposal_id, "apply:before_step_0")

        from datetime import datetime

        from learning_wiki.domain.clock import FrozenClock
        from tests.conftest import make_vault

        ctx2 = make_vault(vault.root, clock=FrozenClock(datetime(2026, 9, 3)))
        reports = ctx2.proposal_recovery.scan_and_recover()
        assert reports[0].outcome == "reset_pending"
        assert ctx2.proposal_repository.state_of(p.proposal_id) == "pending"
        assert not ctx2.paths.resolve("30 Wiki/Concepts/检索练习.md").exists()
        ctx2.close()


class TestCli:
    def test_apply_requires_tty(self, vault) -> None:
        """非 TTY 环境调用 proposal apply 必须失败（规格 13.3.7）。"""
        from typer.testing import CliRunner

        from learning_wiki.cli.app import app

        runner = CliRunner()
        r = runner.invoke(app, ["proposal", "apply", "prop_x"], env={"LW_VAULT": str(vault.root)})
        assert r.exit_code == 1
        assert "非 TTY" in r.output


class TestGit:
    def test_commit_only_proposal_paths(self, vault) -> None:
        """Git commit 不包含用户已有的无关修改（规格 13.2/13.3.8）。"""
        subprocess.run(["git", "init", "-q"], cwd=vault.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=vault.root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=vault.root, check=True)
        # 用户自己的无关未暂存修改
        unrelated = vault.root / "20 Thoughts/note.md"
        unrelated.write_text("用户笔记\n", encoding="utf-8")

        r = capture_text(vault)
        ev = first_evidence_id(vault, r.source_id)
        path = "30 Wiki/Concepts/检索练习.md"
        p = make_proposal(vault, path, wiki_note_content(ev), ev)
        vault.proposal_repository.save(p)
        # 先应用提案（文件存在后才能提交）
        vault.proposal_applier.apply(p.proposal_id)

        from learning_wiki.git.git_adapter import GitAdapter

        git = GitAdapter(vault.root, enabled=True)
        commit = git.commit_proposal(p.proposal_id, [path])
        # pathspec 边界：本次提交只含提案路径，不含用户的无关修改
        committed = [
            line.strip()
            for line in subprocess.run(
                [
                    "git",
                    "-C",
                    str(vault.root),
                    "-c",
                    "core.quotepath=false",
                    "show",
                    "--name-only",
                    "--pretty=format:",
                    commit,
                ],
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            if line.strip()
        ]
        assert committed == [path]
        # 用户无关修改仍留在工作区（未被提交）
        remaining = subprocess.run(
            ["git", "-C", str(vault.root), "status", "--porcelain"],
            capture_output=True,
            text=True,
        ).stdout
        assert "20 Thoughts" in remaining  # 用户无关修改未被提交

    def test_dirty_staged_rejected(self, vault) -> None:
        """用户已有 staged changes 时自动 Git commit 必须拒绝（13.3.8）。"""
        subprocess.run(["git", "init", "-q"], cwd=vault.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=vault.root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=vault.root, check=True)
        user_file = vault.root / "20 Thoughts/note.md"
        user_file.write_text("用户笔记\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(vault.root), "add", "--", "20 Thoughts/note.md"], check=True
        )

        from learning_wiki.git.git_adapter import GitAdapter, GitDirtyError

        git = GitAdapter(vault.root, enabled=True)
        with pytest.raises(GitDirtyError):
            git.commit_proposal("prop_x", ["30 Wiki/x.md"])
