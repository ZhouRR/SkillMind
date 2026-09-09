"""新しい内部観測保存形の往復と厳格境界を、SDK/DB 起動なしで検証する。"""

from __future__ import annotations

import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from projectmind.agent.claude_metering import capture_invocation
from projectmind.agent.metering import (
    AgentInvocation,
    AgentInvocationMode,
    InvocationOptions,
    ResultUsageObservation,
    UsageValue,
    UsageValueKind,
)
from projectmind.core.hashing import canonical_json, sha256_hex
from tests.agent.test_claude_engine import _run_context


@pytest.mark.parametrize(
    "first_module",
    [
        "projectmind.db.models",
        "projectmind.runs.budget",
        "projectmind.runs.budget_store",
        "projectmind.runs.budget_execution",
        "projectmind.runs.repository_budgets",
        "projectmind.agent.metering",
    ],
)
def test_invocation_binding_imports_do_not_depend_on_package_order(first_module: str) -> None:
    """Codec の型参照が DB→Agent eager export の循環を復活させない。"""

    subprocess.run(
        [
            sys.executable,
            "-c",
            "from projectmind.core.settings import Settings\n"
            "Settings.model_config['env_file'] = None\n"
            f"import {first_module}\n"
            "from projectmind.db.models import RunBudgetObservation\n"
            "from projectmind.runs.budget_store import PostgresRunBudgetStore\n"
            "from projectmind.runs.budget_execution import BudgetInvocationRecorder\n"
            "from projectmind.agent.metering import AgentInvocation\n"
            "from projectmind.agent.engine import ClaudeAgentSdkEngine\n",
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )


def invocation(tmp_path: Path) -> AgentInvocation:
    """実際の原値表現を持つ一回の新規呼出しを固定する。"""

    session_id = str(uuid4())
    return capture_invocation(
        _run_context(tmp_path),
        session_id=session_id,
        prompt="Frozen synthetic instruction",
        options=ClaudeAgentOptions(
            session_id=session_id,
            max_turns=5,
            max_budget_usd=0.1,
            model="fixture-model",
            output_format={"type": "json_schema", "schema": {"type": "object"}},
        ),
        mode=AgentInvocationMode.INITIAL,
    )


def test_invocation_roundtrip_preserves_every_bound_field(tmp_path: Path) -> None:
    """保存/再読取で呼出し ID、指令、実 options と原コスト型を変えない。"""

    original = invocation(tmp_path)
    payload = original.to_json()
    restored = AgentInvocation.from_json(payload)
    assert restored == original
    assert restored.checksum == sha256_hex(canonical_json(payload)) == original.checksum
    assert restored.options.checksum == sha256_hex(canonical_json(payload["options"]))
    assert restored.options.max_budget_usd.value == (0.1).hex()
    assert restored.options.max_budget_usd.kind is UsageValueKind.BINARY64
    payload["options"]["max_turns"] = 20
    assert restored.options.max_turns == original.options.max_turns == 5


@pytest.mark.parametrize(
    "key",
    [
        "version",
        "invocation_id",
        "project_id",
        "run_id",
        "run_attempt_id",
        "user_id",
        "session_id",
        "mode",
        "parent_session_id",
        "prompt_checksum",
        "options",
        "sdk_version",
        "cli_version",
    ],
)
def test_invocation_requires_all_original_fields(tmp_path: Path, key: str) -> None:
    """欠落を今日の context やランダム ID で埋めて原束縛と扱わない。"""

    payload = invocation(tmp_path).to_json()
    del payload[key]
    with pytest.raises(ValueError):
        AgentInvocation.from_json(payload)


@pytest.mark.parametrize(
    "path,value",
    [
        (("version",), "agent-invocation/v2"),
        (("extra",), "untrusted"),
        (("invocation_id",), None),
        (("project_id",), 1),
        (("run_id",), "not-a-uuid"),
        (("mode",), "REPLACE"),
        (("parent_session_id",), "not-a-uuid"),
        (("prompt_checksum",), "sha256:" + "a" * 64),
        (("sdk_version",), "invalid\nversion"),
        (("options", "extra"), "untrusted"),
        (("options", "max_turns"), True),
        (("options", "max_turns"), 0),
        (("options", "max_turns"), 2**63),
        (("options", "max_turns"), 1.0),
        (("options", "model"), {"unexpected": "object"}),
        (("options", "continue_conversation"), 0),
        (("options", "continue_conversation"), True),
        (("options", "fork_session"), 1),
        (("options", "fork_session"), True),
        (("options", "output_format_checksum"), ""),
        (("options", "max_budget_usd", "kind"), "DECIMAL"),
        (("options", "max_budget_usd", "value"), "nan"),
        (("options", "max_budget_usd", "extra"), 1),
    ],
)
def test_invalid_saved_invocation_is_rejected_before_binding(
    tmp_path: Path, path: tuple[str, ...], value: Any
) -> None:
    """壊れた観測や未定義版を有効な原実行として再解釈しない。"""

    payload = deepcopy(invocation(tmp_path).to_json())
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        AgentInvocation.from_json(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"prompt_checksum": "b" * 64},
        {"sdk_version": "0.2.111"},
        {"cli_version": "2.1.192"},
        {"invocation_id": uuid4()},
        {"project_id": uuid4()},
        {"run_id": uuid4()},
        {"run_attempt_id": uuid4()},
        {"user_id": uuid4()},
    ],
)
def test_each_invocation_identity_change_changes_checksum(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    """同じ Session でも別指令・別呼出し・別主体を同じ束縛へ混ぜない。"""

    original = invocation(tmp_path)
    assert replace(original, **changes).checksum != original.checksum


@pytest.mark.parametrize(
    "kind,value",
    [
        (UsageValueKind.MISSING, None),
        (UsageValueKind.INVALID, None),
        (UsageValueKind.INTEGER, 0),
        (UsageValueKind.INTEGER, 2**63 - 1),
        (UsageValueKind.BINARY64, (0.1).hex()),
    ],
)
def test_usage_value_roundtrip_keeps_missing_invalid_and_binary64_distinct(
    kind: UsageValueKind, value: int | str | None
) -> None:
    """原型の保存で欠測や既に丸められた浮動小数を精確な零/整数にしない。"""

    original = UsageValue(kind, value)
    assert UsageValue.from_json(original.to_json()) == original


def test_raw_observation_has_its_own_version_and_no_settlement_fields(tmp_path: Path) -> None:
    """原観測の保存形に normalized report、金銭単位や停止の主張を混入させない。"""

    original = invocation(tmp_path)
    observed = ResultUsageObservation(
        original,
        UsageValue(UsageValueKind.INTEGER, 3),
        UsageValue(UsageValueKind.MISSING),
    )
    assert observed.to_json() == {
        "version": "sdk-result-observation/v1",
        "invocation": original.to_json(),
        "turns": {"kind": "INTEGER", "value": 3},
        "cost_usd": {"kind": "MISSING", "value": None},
    }
    assert InvocationOptions.from_json(original.options.to_json()) == original.options
    with pytest.raises(ValueError):
        replace(observed, turns=UsageValue(UsageValueKind.BINARY64, (3.0).hex()))
