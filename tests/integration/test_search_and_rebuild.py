"""搜索与重建集成测试（规格 13.2、4.3）。"""

import shutil

from tests.conftest import fixture_path, make_vault


def _capture_text(vault, filename: str):
    text = fixture_path(filename).read_text(encoding="utf-8")
    item = vault.capture_service.add_to_inbox("text", payload=text)
    return vault.capture_service.process(item.item_id)


class TestSearch:
    def test_chinese_fts_conceptual(self, vault) -> None:
        _capture_text(vault, "chinese_text.txt")
        resp = vault.search_service.search("检索练习")
        assert resp.query_type == "conceptual"
        assert len(resp.hits) >= 1
        hit = resp.hits[0]
        assert hit.matched_evidence_id is not None  # 四要素：证据
        assert hit.match_reason  # 匹配原因
        assert hit.content_type == "source"  # 内容类型

    def test_two_char_query(self, vault) -> None:
        _capture_text(vault, "chinese_text.txt")
        assert len(vault.search_service.search("自测").hits) >= 1

    def test_mixed_cn_en(self, vault) -> None:
        text = fixture_path("markdown_note.md").read_text(encoding="utf-8")
        item = vault.capture_service.add_to_inbox("text", payload=text)
        vault.capture_service.process(item.item_id)
        assert len(vault.search_service.search("FSRS 算法").hits) >= 1

    def test_exact_evidence_id(self, vault) -> None:
        result = _capture_text(vault, "chinese_text.txt")
        row = vault.conn.execute(
            "SELECT evidence_id FROM evidence WHERE source_id = ? LIMIT 1",
            (result.source_id,),
        ).fetchone()
        resp = vault.search_service.search(row[0])
        assert resp.query_type == "exact"
        assert resp.hits[0].matched_evidence_id == row[0]

    def test_exact_source_id_lists_versions(self, vault) -> None:
        result = _capture_text(vault, "chinese_text.txt")
        resp = vault.search_service.search(result.source_id)
        assert resp.query_type == "exact"
        assert resp.hits[0].source_id == result.source_id

    def test_source_local_scope(self, vault) -> None:
        _capture_text(vault, "chinese_text.txt")
        other = vault.capture_service.add_to_inbox(
            "text", payload="完全无关的另一段文字，讨论烹饪与旅行。"
        )
        vault.capture_service.process(other.item_id)
        target = vault.conn.execute("SELECT source_id FROM sources LIMIT 1").fetchone()
        resp = vault.search_service.search("文字", source_id=target[0])
        assert resp.query_type == "source_local"
        assert all(h.source_id == target[0] for h in resp.hits)

    def test_open_evidence_newer_version_flag(self, vault) -> None:
        """引用旧版本合法，但应提示存在更新版本（规格 5.2）。"""
        text = fixture_path("chinese_text.txt").read_text(encoding="utf-8")
        r1 = vault.capture_service.process(
            vault.capture_service.add_to_inbox("text", payload=text).item_id
        )
        # 同内容不能建 v2 —— 用不同文本 + 直接 repo.append_version 模拟重提取
        from learning_wiki.domain import hashing
        from learning_wiki.domain.contracts import SourceVersionRecord
        from learning_wiki.storage import evidence_store

        content, evs = evidence_store.build_content_with_anchors(
            r1.source_id,
            "v0002",
            hashing.hash_text("重提取后的新正文内容。"),
            "重提取后的新正文内容。",
        )
        vault.source_repository.append_version(
            r1.source_id,
            SourceVersionRecord(
                version_id="v0002",
                created_at=vault.clock.now_iso(),
                extraction_method="plain_text",
                content_hash=hashing.hash_text("重提取后的新正文内容。"),
            ),
            content,
            evs,
            None,
            None,
        )
        # 直接经 repo 追加版本后刷新派生索引（manifest 是事实源）
        vault.indexer.upsert_source(vault.source_repository.load(r1.source_id))
        # v0001 的证据（已在索引中）应提示存在更新版本
        v1_ev = vault.conn.execute(
            "SELECT evidence_id FROM evidence WHERE version_id = 'v0001' LIMIT 1"
        ).fetchone()
        loc_v1 = vault.search_service.open_evidence(v1_ev[0])
        assert loc_v1.newer_version_available is True
        assert loc_v1.version_id == "v0001"  # 引用旧版本合法


class TestRebuild:
    def test_delete_derived_and_rebuild_equal(self, vault, frozen_clock) -> None:
        """删除 .learning-wiki/ 后重建，事实对象集合一致（规格 13.3.5）。"""
        _r1 = _capture_text(vault, "chinese_text.txt")
        text2 = fixture_path("markdown_note.md").read_text(encoding="utf-8")
        vault.capture_service.process(
            vault.capture_service.add_to_inbox("text", payload=text2).item_id
        )

        def snapshot() -> dict:
            return {
                "sources": {row[0] for row in vault.conn.execute("SELECT source_id FROM sources")},
                "versions": set(
                    vault.conn.execute(
                        "SELECT source_id, version_id, content_hash FROM source_versions"
                    )
                ),
                "evidence": set(vault.conn.execute("SELECT evidence_id, span_hash FROM evidence")),
            }

        before = snapshot()
        vault.close()

        shutil.rmtree(vault.root / ".learning-wiki")
        ctx2 = make_vault(vault.root, clock=frozen_clock)
        report = ctx2.rebuild_service.rebuild()
        assert report.errors == []
        after = {
            "sources": {row[0] for row in ctx2.conn.execute("SELECT source_id FROM sources")},
            "versions": set(
                ctx2.conn.execute("SELECT source_id, version_id, content_hash FROM source_versions")
            ),
            "evidence": set(ctx2.conn.execute("SELECT evidence_id, span_hash FROM evidence")),
        }
        assert after == before
        # 搜索同样命中，且命中的证据在重建后的证据集合中
        resp = ctx2.search_service.search("检索练习")
        assert len(resp.hits) >= 1
        evidence_ids = {eid for eid, _ in after["evidence"]}
        assert any(
            h.matched_evidence_id is not None and h.matched_evidence_id in evidence_ids
            for h in resp.hits
        )
        # lint 通过
        lint = ctx2.integrity_service.lint()
        assert lint.ok, [i.message for i in lint.errors]
        ctx2.close()

    def test_doctor_detects_needs_rebuild(self, vault) -> None:
        _capture_text(vault, "chinese_text.txt")
        vault.conn.execute("DELETE FROM sources")
        report = vault.integrity_service.doctor()
        assert report["needs_rebuild"] is True
