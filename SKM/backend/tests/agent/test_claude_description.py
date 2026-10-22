"""Claude CLI の 2048 文字上限に対し、提案入口の必須要件が届くことを検証する。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from skillmind.agent.claude_tool_schema import DESCRIPTION_LIMIT, fit_claude_description
from skillmind.agent.contract_store import ContractStore
from skillmind.agent.tool_catalog import _change_propose_tool_definition, create_run_tool_registry

CONTRACTS = ContractStore(Path(__file__).resolve().parents[3] / "contracts")


def test_description_within_the_limit_is_unchanged():
    assert fit_claude_description("short", limit=10) == "short"


def test_long_description_is_cut_at_a_paragraph_boundary():
    text = "first paragraph.\nsecond paragraph that is long"

    assert fit_claude_description(text, limit=30) == "first paragraph."


def test_long_description_without_paragraphs_is_cut_at_a_sentence_boundary():
    text = "One sentence. Another sentence that runs past the limit."

    assert fit_claude_description(text, limit=30) == "One sentence."


def test_description_without_any_boundary_is_cut_at_the_limit():
    assert fit_claude_description("x" * 50, limit=20) == "x" * 20


def test_proposal_core_requirements_fit_in_the_published_description():
    full = _change_propose_tool_definition(CONTRACTS).description

    published = fit_claude_description(full)

    # 全 effect の形式を含む完全版は Codex 用に残し、Claude へは必須要件の段落を届ける。
    assert len(full) > DESCRIPTION_LIMIT
    assert len(published) <= DESCRIPTION_LIMIT
    for requirement in (
        "precondition {revision}",
        "evidence_refs",
        "request_file, expected_hash",
        "Do not mix the two forms",
        "never change.propose/v1 itself",
        "minimum_risk is not an input field",
        "task brief",
    ):
        assert requirement in published


def test_no_other_tool_description_is_cut():
    registry = create_run_tool_registry(
        CONTRACTS, document_source=Mock(), mcp_tools_provider=Mock(), mcp_query_provider=Mock(),
        mcp_download_provider=Mock(), mcp_provider=Mock(), database_provider=Mock(),
        http_provider=Mock(), native_database_provider=Mock(), audit_export_provider=Mock(),
        artifact_append_provider=Mock(), artifact_materialize_provider=Mock(),
        document_files_provider=Mock(), document_readiness_provider=Mock(),
        subagent_provider=Mock(), mcp_tools_enabled=True,
    )

    for definition in registry._definitions.values():
        published = fit_claude_description(definition.description)
        assert len(published) <= DESCRIPTION_LIMIT
        if definition.capability != "change.propose/v1":
            assert published == definition.description, definition.capability
