"""MCP server 集成测试：工具注册、读工具、提案写边界。

写工具只能创建待审批提案——「应用」必须由用户在 TTY 下经 CLI 完成
（规格 8.2：MCP 不提供直接应用 Wiki 提案的工具）。
"""

import asyncio

from tests.conftest import fixture_path


def _capture(vault):
    text = fixture_path("chinese_text.txt").read_text(encoding="utf-8")
    item = vault.capture_service.add_to_inbox("text", payload=text)
    return vault.capture_service.process(item.item_id)


class TestMcpServer:
    def test_tools_registered(self, vault) -> None:
        from learning_wiki.mcp.server import create_server

        server = create_server(vault.root)
        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}
        expected = {
            "lw_status",
            "lw_search",
            "lw_read_source_version",
            "lw_read_evidence",
            "lw_get_wiki_context",
            "lw_list_update_candidates",
            "lw_lint",
            "lw_submit_proposal",
            "lw_validate_proposal",
            # 学习机制升级（§8.1）
            "lw_create_goal",
            "lw_get_goal",
            "lw_start_diagnostic",
            "lw_get_next_activity",
            "lw_submit_attempt",
            "lw_submit_confidence",
            "lw_submit_feedback",
            "lw_propose_misconception",
            "lw_update_misconception",
            "lw_create_application_challenge",
            "lw_record_application_prediction",
            "lw_record_application_outcome",
            "lw_get_capability_evidence",
            "lw_list_revalidation_tasks",
        }
        assert expected <= names
        # 不提供直接应用 / 删除 / 直接达成类工具
        assert not any("apply" in n or "delete" in n or "achieve" in n for n in names)

    def test_search_and_read_evidence(self, vault) -> None:
        from learning_wiki.mcp.server import create_server

        r = _capture(vault)
        ev = vault.conn.execute("SELECT evidence_id FROM evidence LIMIT 1").fetchone()[0]
        server = create_server(vault.root)

        result = asyncio.run(server.call_tool("lw_search", {"query": "检索练习"}))
        assert "检索练习" in str(result)
        result = asyncio.run(server.call_tool("lw_read_evidence", {"evidence_id": ev}))
        assert "text" in str(result)
        result = asyncio.run(
            server.call_tool(
                "lw_read_source_version",
                {"source_id": r.source_id, "version_id": "v0001"},
            )
        )
        assert "认知心理学" in str(result)

    def test_submit_proposal_creates_pending_only(self, vault) -> None:
        """写边界：提交提案只产生 pending 状态，不写目标文件。"""
        import re

        from learning_wiki.mcp.server import create_server

        _capture(vault)
        ev = vault.conn.execute("SELECT evidence_id FROM evidence LIMIT 1").fetchone()[0]
        server = create_server(vault.root)

        result = asyncio.run(
            server.call_tool(
                "lw_submit_proposal",
                {
                    "reason": "MCP 测试提案",
                    "operations": [
                        {
                            "operation": "create",
                            "path": "30 Wiki/Concepts/MCP测试.md",
                            "content": (
                                "---\nnote_id: mcp_test\ntype: concept\n"
                                "title: MCP测试\ncreated_at: 2026-09-03\n"
                                "updated_at: 2026-09-03\n---\n\n测试正文\n"
                            ),
                        }
                    ],
                    "cited_evidence_ids": [ev],
                },
            )
        )
        s = str(result)
        assert "proposal_id" in s
        pid = re.search(r"prop_[0-9A-Z]+", s).group(0)
        # 只创建待审批提案，目标文件未被写
        assert vault.proposal_repository.state_of(pid) == "pending"
        assert not vault.paths.resolve("30 Wiki/Concepts/MCP测试.md").exists()

    def test_submit_proposal_with_bad_evidence_flagged(self, vault) -> None:
        """无证据提案在返回的 validation 中被标记（Agent 能看到失败原因）。"""
        from learning_wiki.mcp.server import create_server

        _capture(vault)
        server = create_server(vault.root)
        result = asyncio.run(
            server.call_tool(
                "lw_submit_proposal",
                {
                    "reason": "无证据提案",
                    "operations": [
                        {
                            "operation": "create",
                            "path": "30 Wiki/Concepts/无证据.md",
                            "content": "正文\n",
                        }
                    ],
                    "cited_evidence_ids": ["ev_NONEXIST_v0001_0001"],
                },
            )
        )
        assert "不存在" in str(result)

    def test_get_wiki_context(self, vault) -> None:
        """先应用一个提案，再经 MCP 获取 Wiki 上下文。"""
        from learning_wiki.mcp.server import create_server

        _capture(vault)
        ev = vault.conn.execute("SELECT evidence_id FROM evidence LIMIT 1").fetchone()[0]
        result = asyncio.run(
            server_call(
                vault,
                "lw_submit_proposal",
                {
                    "reason": "沉淀",
                    "operations": [
                        {
                            "operation": "create",
                            "path": "30 Wiki/Concepts/上下文测试.md",
                            "content": (
                                "---\nnote_id: ctx_test\ntype: concept\ntitle: 上下文测试\n"
                                "created_at: 2026-09-03\nupdated_at: 2026-09-03\n---\n\n"
                                f"检索练习改善保持。[[10 Sources/x#^{ev.replace('_', '-')}]]\n\n"
                                "<!-- lw:claim status=supported -->\n\n^claim-ctx-001\n"
                            ),
                        }
                    ],
                    "cited_evidence_ids": [ev],
                },
            )
        )
        import re

        pid = re.search(r"prop_[0-9A-Z]+", str(result)).group(0)
        vault.proposal_applier.apply(pid)

        server = create_server(vault.root)
        result = asyncio.run(server.call_tool("lw_get_wiki_context", {"query": "上下文测试"}))
        s = str(result)
        assert "ctx_test" in s
        assert "claim-ctx-001" in s


async def server_call(vault, tool: str, args: dict) -> object:
    from learning_wiki.mcp.server import create_server

    server = create_server(vault.root)
    return await server.call_tool(tool, args)
