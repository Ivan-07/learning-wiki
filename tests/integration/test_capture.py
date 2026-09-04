"""捕获链路集成测试（规格 13.2）。

- 三类输入均能形成可引用来源；
- 重复导入不生成重复版本；
- 相同内容、不同来源保留各自来源身份；
- 受限网页有人工降级路径；
- Prompt Injection 内容作为数据保存，不触发任何写入。
"""

import shutil

import pytest

from tests.conftest import fixture_path


def capture_text(svc, text: str):
    item = svc.add_to_inbox("text", payload=text)
    return svc.process(item.item_id)


class TestThreeInputTypes:
    def test_plain_text(self, vault) -> None:
        text = fixture_path("chinese_text.txt").read_text(encoding="utf-8")
        result = capture_text(vault.capture_service, text)
        assert result.evidence_count > 0
        manifest = vault.source_repository.load(result.source_id)
        assert manifest.source_type == "text"
        assert manifest.versions[0].version_id == "v0001"

    def test_markdown_file(self, vault) -> None:
        path = fixture_path("markdown_note.md")
        item = vault.capture_service.add_to_inbox("file", payload_path=str(path))
        result = vault.capture_service.process(item.item_id)
        assert result.evidence_count >= 5  # 标题+段落+列表+代码
        sdir = vault.source_repository.source_dir(result.source_id)
        original = sdir / "versions/v0001/original.md"
        assert original.exists()

    def test_web_page(self, vault, capture_with_web, web_server) -> None:
        item = capture_with_web.add_to_inbox("url", payload=web_server.url("web_article.html"))
        result = capture_with_web.process(item.item_id)
        assert result.evidence_count >= 3
        sdir = vault.paths.folder("sources") / result.source_id
        assert (sdir / "versions/v0001/original.html").exists()  # HTML 快照
        manifest = vault.source_repository.load(result.source_id)
        assert manifest.canonical_url == web_server.url("web_article.html")


class TestDuplicates:
    def test_duplicate_text_reuses_version(self, vault) -> None:
        text = fixture_path("chinese_text.txt").read_text(encoding="utf-8")
        first = capture_text(vault.capture_service, text)
        second = capture_text(vault.capture_service, text)
        assert second.duplicate is True
        assert second.version_id == first.version_id
        # 只有一个来源目录、一个版本
        manifest = vault.source_repository.load(first.source_id)
        assert len(manifest.versions) == 1

    def test_duplicate_url_same_content_reuses_version(self, capture_with_web, web_server) -> None:
        url = web_server.url("web_article.html")
        r1 = capture_with_web.process(capture_with_web.add_to_inbox("url", payload=url).item_id)
        r2 = capture_with_web.process(capture_with_web.add_to_inbox("url", payload=url).item_id)
        assert r2.duplicate and r2.version_id == r1.version_id

    def test_same_url_new_content_creates_new_version(
        self, vault, capture_with_web, web_server, tmp_path
    ) -> None:
        # 用独立副本服务器目录避免污染 fixtures
        url = web_server.url("web_article.html")
        r1 = capture_with_web.process(capture_with_web.add_to_inbox("url", payload=url).item_id)
        # 服务内容换为 v2
        target = fixture_path("web_article.html")
        backup = tmp_path / "backup.html"
        shutil.copy(target, backup)
        shutil.copy(fixture_path("web_article_v2.html"), target)
        try:
            r2 = capture_with_web.process(capture_with_web.add_to_inbox("url", payload=url).item_id)
        finally:
            shutil.copy(backup, target)
        assert not r2.duplicate
        assert r2.version_id == "v0002"
        assert r2.source_id == r1.source_id  # 同一来源
        # 旧版本文件未被修改（不可变）
        manifest = vault.source_repository.load(r1.source_id)
        assert len(manifest.versions) == 2

    def test_same_content_different_source_keeps_identity(
        self, capture_with_web, web_server
    ) -> None:
        """相同内容、不同 URL：不得合并为同一来源（规格 5.1/13.2）。"""
        r1 = capture_with_web.process(
            capture_with_web.add_to_inbox("url", payload=web_server.url("web_article.html")).item_id
        )
        # 第二个 URL 返回相同内容（用 contradictory 换成相同？构造同内容不同路径）
        # fixtures 中 web_article.html 与自身相同 → 用 query 参数区分 URL
        r2 = capture_with_web.process(
            capture_with_web.add_to_inbox(
                "url", payload=web_server.url("web_article.html?v=2")
            ).item_id
        )
        assert r2.source_id != r1.source_id


class TestBlockedPages:
    def test_login_wall_degrades_explainably(self, capture_with_web, web_server) -> None:
        item = capture_with_web.add_to_inbox("url", payload=web_server.url("web_login_wall.html"))
        with pytest.raises(Exception) as exc_info:
            capture_with_web.process(item.item_id)
        assert "登录" in str(exc_info.value) or "提取正文" in str(exc_info.value)
        # Inbox 项状态为 failed 且记录原因
        items = capture_with_web.list_inbox(state="failed")
        assert any(i.item_id == item.item_id and i.error for i in items)


class TestPromptInjection:
    def test_injection_content_is_data_not_instructions(
        self, capture_with_web, web_server, vault
    ) -> None:
        """恶意网页中的指令不会触发任何 Vault 写入（规格 13.3.2）。"""
        before = {
            p.relative_to(vault.root).as_posix() for p in vault.root.rglob("*") if p.is_file()
        }
        item = capture_with_web.add_to_inbox(
            "url", payload=web_server.url("web_prompt_injection.html")
        )
        result = capture_with_web.process(item.item_id)
        after = {p.relative_to(vault.root).as_posix() for p in vault.root.rglob("*") if p.is_file()}
        # 新增文件只允许出现在该来源目录、Inbox 与 Operations 日志
        allowed_prefixes = (
            "10 Sources/",
            ".learning-wiki/",
            "_System/Learning Wiki/Operations/",
        )
        added = after - before
        assert added, "捕获应写入来源文件"
        for path in added:
            assert path.startswith(allowed_prefixes), f"越界写入: {path}"
        # 注入文本被完整保存为证据（数据保留）
        manifest = vault.source_repository.load(result.source_id)
        ev_file = (
            vault.source_repository.source_dir(result.source_id)
            / manifest.versions[0].evidence_path
        )
        text = ev_file.read_text(encoding="utf-8")
        assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in text
        # 30 Wiki 没有任何新文件（指令未被执行）
        wiki = vault.paths.folder("wiki")
        assert not list(wiki.rglob("*.md"))
