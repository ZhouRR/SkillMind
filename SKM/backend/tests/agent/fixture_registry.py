"""合成 Provider を明示的に使う test 専用 registry を構築する。"""

from __future__ import annotations

from pathlib import Path

from skillmind.agent.context_builder import (
    ContractStore,
    _change_propose_tool_definition,
    _interaction_tool_definition,
)
from skillmind.agent.tool_gateway import ToolDefinition, ToolRegistry
from tests.agent.fixture_providers import CsvFixtureIssueProvider, GitFixtureRepositoryProvider

PROVIDER_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/generic-read-providers"


def create_fixture_tool_registry(contracts: ContractStore) -> ToolRegistry:
    """本番 registry と分離し、offline test だけに CSV/Git を登録する。"""

    definitions = []
    for capability, provider, implementation in (
        ("issue.read/v1", "csv", CsvFixtureIssueProvider(PROVIDER_FIXTURES / "issues.csv")),
        (
            "repository.read/v1",
            "git",
            GitFixtureRepositoryProvider(PROVIDER_FIXTURES / "repository"),
        ),
    ):
        definitions.append(ToolDefinition(
            capability=capability,
            description="Read deterministic test data",
            request_schema=contracts.load(f"tools/{capability}/request.schema.json"),
            response_schema=contracts.load(f"tools/{capability}/response.schema.json"),
            error_schema=contracts.load(f"tools/{capability}/error.schema.json"),
            providers={provider: implementation},
        ))
    return ToolRegistry((
        *definitions,
        _interaction_tool_definition(contracts),
        _change_propose_tool_definition(contracts),
    ))
