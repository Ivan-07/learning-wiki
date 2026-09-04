"""完整性与健康检查：lw lint / lw doctor。

lint 至少检查（规格 12.1，M0/M1 子集）：
- manifest Schema 与未来 schema_version（只读告警）；
- manifest 中记录的文件存在与哈希一致（篡改必被发现，13.3.6）；
- Evidence 与来源版本、块 ID、span hash 一致；
- 块 ID / Evidence ID 冲突；
- Wiki frontmatter Schema。

doctor：配置、派生库、重建需要、staging 残留与告警计数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from learning_wiki.domain import hashing
from learning_wiki.domain.contracts import NoteFrontmatter
from learning_wiki.storage import evidence_store, note_parser, yaml_io
from learning_wiki.storage.source_repository import (
    SUPPORTED_SCHEMA_VERSION,
    SourceRepository,
)
from learning_wiki.storage.vault_paths import VaultPaths


@dataclass
class LintIssue:
    severity: str  # error | warning
    kind: str
    subject: str
    message: str


@dataclass
class LintReport:
    issues: list[LintIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors


class IntegrityService:
    def __init__(self, paths: VaultPaths, repo: SourceRepository, conn: Any) -> None:
        self.paths = paths
        self.repo = repo
        self.conn = conn

    # -- lint ---------------------------------------------------------------

    def lint(self) -> LintReport:
        report = LintReport()
        seen_evidence_ids: set[str] = set()
        seen_block_ids: set[str] = set()

        sources_dir = self.paths.folder("sources")
        for mpath in sorted(sources_dir.glob("*/manifest.yaml")) if sources_dir.exists() else []:
            source_id = mpath.parent.name
            self._lint_manifest(mpath, source_id, report, seen_evidence_ids, seen_block_ids)
        self._lint_wiki(report)
        return report

    def _lint_manifest(
        self,
        mpath: Any,
        source_id: str,
        report: LintReport,
        seen_evidence_ids: set[str],
        seen_block_ids: set[str],
    ) -> None:
        sdir = mpath.parent
        try:
            data = yaml_io.load_yaml(mpath)
        except Exception as exc:
            report.issues.append(LintIssue("error", "manifest_parse", source_id, str(exc)))
            return
        if int(data.get("schema_version", 1)) > SUPPORTED_SCHEMA_VERSION:
            report.issues.append(
                LintIssue(
                    "warning",
                    "schema_version_unsupported",
                    source_id,
                    f"schema_version {data['schema_version']} 高于当前支持 "
                    f"{SUPPORTED_SCHEMA_VERSION}，来源只读打开",
                )
            )
        from learning_wiki.domain.contracts import SourceManifest

        try:
            manifest = SourceManifest.model_validate(dict(data))
        except Exception as exc:
            report.issues.append(LintIssue("error", "manifest_schema", source_id, str(exc)))
            return

        version_ids = [v.version_id for v in manifest.versions]
        if len(version_ids) != len(set(version_ids)):
            report.issues.append(
                LintIssue("error", "duplicate_version", source_id, "存在重复 version_id")
            )

        for rec in manifest.versions:
            vtag = f"{source_id}/{rec.version_id}"
            # 文件存在性
            for rel in (*rec.original_paths, rec.content_path, rec.evidence_path):
                if not (sdir / rel).exists():
                    report.issues.append(
                        LintIssue(
                            "error", "missing_file", vtag, f"manifest 记录的文件不存在: {rel}"
                        )
                    )
            # 内容哈希（篡改检测）：content_hash 绑定提取内容，
            # 落盘文件需先剥离系统追加的锚点行再比对
            content_file = sdir / rec.content_path
            if content_file.exists():
                actual = evidence_store.content_hash_of_stored(
                    content_file.read_text(encoding="utf-8")
                )
                if actual != rec.content_hash:
                    report.issues.append(
                        LintIssue(
                            "error",
                            "content_hash_mismatch",
                            vtag,
                            f"content.md 哈希与 manifest 不一致（预期 {rec.content_hash}, "
                            f"实际 {actual}）——来源版本可能被篡改",
                        )
                    )
            for rel in rec.original_paths:
                ofile = sdir / rel
                if ofile.exists() and rec.original_hash:
                    actual = hashing.hash_bytes(ofile.read_bytes())
                    if actual != rec.original_hash:
                        report.issues.append(
                            LintIssue(
                                "error",
                                "original_hash_mismatch",
                                vtag,
                                f"原件哈希不一致: {rel}",
                            )
                        )
            # 证据
            content_text = content_file.read_text(encoding="utf-8") if content_file.exists() else ""
            for ev in evidence_store.load_evidence_file(sdir / rec.evidence_path):
                if ev.evidence_id in seen_evidence_ids:
                    report.issues.append(
                        LintIssue(
                            "error",
                            "duplicate_evidence_id",
                            vtag,
                            f"重复 Evidence ID: {ev.evidence_id}",
                        )
                    )
                seen_evidence_ids.add(ev.evidence_id)
                if ev.anchor_start in seen_block_ids:
                    report.issues.append(
                        LintIssue(
                            "error", "duplicate_block_id", vtag, f"重复块 ID: {ev.anchor_start}"
                        )
                    )
                seen_block_ids.add(ev.anchor_start)
                if ev.source_id != source_id or ev.version_id != rec.version_id:
                    report.issues.append(
                        LintIssue(
                            "error", "evidence_binding", vtag, f"证据绑定错误: {ev.evidence_id}"
                        )
                    )
                elif not evidence_store.verify_evidence(ev, content_text):
                    report.issues.append(
                        LintIssue(
                            "error",
                            "span_hash_mismatch",
                            vtag,
                            f"span_hash 与内容块不一致（锚点漂移或篡改）: {ev.evidence_id}",
                        )
                    )

    def _lint_wiki(self, report: LintReport) -> None:
        wiki_dir = self.paths.folder("wiki")
        if not wiki_dir.exists():
            return
        for path in sorted(wiki_dir.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            fm = note_parser.parse_frontmatter(text)
            if not fm:
                continue
            try:
                NoteFrontmatter.model_validate(fm)
            except Exception as exc:
                report.issues.append(LintIssue("warning", "note_frontmatter", path.name, str(exc)))

    # -- doctor ---------------------------------------------------------------

    def doctor(self) -> dict[str, Any]:
        sources_dir = self.paths.folder("sources")
        manifest_count = (
            len(list(sources_dir.glob("*/manifest.yaml"))) if sources_dir.exists() else 0
        )
        db_path = self.paths.db_path()
        db_ok = db_path.exists()
        indexed_sources = 0
        alerts = 0
        needs_rebuild = False
        if db_ok:
            try:
                indexed_sources = self.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
                alerts = self.conn.execute(
                    "SELECT COUNT(*) FROM integrity_alerts WHERE resolved_at IS NULL"
                ).fetchone()[0]
                needs_rebuild = manifest_count > indexed_sources
            except Exception:
                needs_rebuild = True
        staging = self.paths.dot_dir() / "staging"
        leftovers = sorted(p.name for p in staging.iterdir()) if staging.exists() else []
        return {
            "vault_root": str(self.paths.root),
            "config_ok": (self.paths.system_dir() / "Config.yaml").exists(),
            "db_ok": db_ok,
            "needs_rebuild": needs_rebuild,
            "sources_on_disk": manifest_count,
            "sources_indexed": indexed_sources,
            "open_alerts": alerts,
            "staging_leftovers": leftovers,
        }
