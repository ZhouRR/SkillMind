"""通用 task Run 作成 service の source 選択と snapshot 構築を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Self
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from projectmind.integrations.repository import IntegrationRepository
from projectmind.runs.domain import (
    CreatedRun,
    CreateRunCommand,
    RunStatus,
    TaskSourceSelectionError,
)
from projectmind.runs.repository import RunRepository
from projectmind.runs.service import M0_DENIED_BUILTIN_TOOLS, RunService, _resolve_selected_sources
from projectmind.skills import ResolvedTaskRun


def _resolved(
    requirements: tuple[dict[str, object], ...] = (),
) -> ResolvedTaskRun:
    """Run 作成テスト用の解決済み task を組み立てる。

    資源要求の宣言元は blueprint 一本のため、frozen manifest の
    `capability_blueprint.resource_requirements` へ直接載せる。
    """

    return ResolvedTaskRun(
        skill_id=uuid4(),
        skill_version_id=uuid4(),
        skill_key="repository-review",
        version="1.0.0",
        task_key="review-change",
        capability="repository.review",
        task_type="immediate",
        manifest_checksum="sha256:" + ("a" * 64),
        allowed_capabilities=("repository.read/v1",),
        skill_snapshot={
            "skill_version_id": "sv",
            "manifest_checksum": "sha256:x",
            "sort_order": 0,
            "manifest": {
                "capability_blueprint": {
                    "execution_preferences": {"recommended_profile": "SUPERVISED"},
                    "resource_requirements": list(requirements),
                }
            },
        },
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={"type": "object", "additionalProperties": False},
        input_schema_checksum="sha256:" + ("b" * 64),
        output_schema_checksum="sha256:" + ("c" * 64),
        task_output_schema=None,
        task_output_schema_checksum=None,
    )


_REPOSITORY_SOURCE: dict[str, object] = {
    "key": "repository-source",
    "kind": "repository",
    "required": True,
    "access": "read",
    "capabilities": ["repository.read/v1"],
    "accepted_providers": ["git"],
}
_OPTIONAL_ISSUE_SOURCE: dict[str, object] = {
    "key": "issue-source",
    "kind": "issue",
    "required": False,
    "access": "read",
    "capabilities": ["issue.read/v1"],
    "accepted_providers": ["csv", "redmine"],
}


class _NoConfiguredBindings:
    """Legacy fixture source test で持続 binding を返さない repository。"""

    async def find_configured_binding(self, **kwargs: object) -> None:
        """Project/Task binding が無い状態を返す。"""

        del kwargs
        return None


async def _no_configured_binding(
    self: IntegrationRepository, **kwargs: object
) -> None:
    """Legacy test では持続 ResourceBinding を解決しない。"""

    del self, kwargs
    return None


async def _resolve_legacy_sources(
    resolved: ResolvedTaskRun, sources: dict[str, str]
) -> dict[str, object]:
    """Legacy fixture source を新しい async binding resolver で解決する。"""

    blueprint = resolved.skill_snapshot["manifest"]["capability_blueprint"]
    selected, bindings = await _resolve_selected_sources(
        resolved,
        sources,
        blueprint=blueprint,
        project_id=uuid4(),
        integration_repository=_NoConfiguredBindings(),  # type: ignore[arg-type]
    )
    assert bindings == ()
    return selected


@pytest.mark.asyncio
async def test_resolve_selected_sources_binds_capability_and_provider() -> None:
    """受理 provider を選ぶと capability 付きの選択 snapshot を返す。"""

    selected = await _resolve_legacy_sources(
        _resolved((_REPOSITORY_SOURCE,)), {"repository-source": "git"}
    )

    assert selected == {
        "repository-source": {
            "capability": "repository.read/v1",
            "provider": "git",
            "binding_level": "LEGACY_FIXTURE",
        }
    }


@pytest.mark.asyncio
async def test_resolve_selected_sources_omits_absent_optional_source() -> None:
    """任意 source は未選択なら snapshot から除外する。"""

    selected = await _resolve_legacy_sources(_resolved((_OPTIONAL_ISSUE_SOURCE,)), {})

    assert selected == {}


@pytest.mark.asyncio
async def test_resolve_selected_sources_rejects_missing_required_source() -> None:
    """必須 source の未選択は拒否する。"""

    with pytest.raises(TaskSourceSelectionError):
        await _resolve_legacy_sources(_resolved((_REPOSITORY_SOURCE,)), {})


@pytest.mark.asyncio
async def test_resolve_selected_sources_rejects_unaccepted_provider() -> None:
    """Manifest が受理しない provider を拒否する。"""

    with pytest.raises(TaskSourceSelectionError):
        await _resolve_legacy_sources(
            _resolved((_REPOSITORY_SOURCE,)), {"repository-source": "svn"}
        )


@pytest.mark.asyncio
async def test_resolve_selected_sources_rejects_unknown_source_key() -> None:
    """Manifest に無い data source key を拒否する。"""

    with pytest.raises(TaskSourceSelectionError):
        await _resolve_legacy_sources(
            _resolved((_REPOSITORY_SOURCE,)), {"repository-source": "git", "ghost": "x"}
        )


class _Transaction:
    """テスト用の async transaction context。"""

    async def __aenter__(self) -> Self:
        """何も開かずに自身を返す。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """後始末は不要。"""

        return None


class _Session:
    """create_task_run が使う最小 session seam。"""

    async def __aenter__(self) -> Self:
        """何も接続せずに自身を返す。"""

        return self

    async def __aexit__(self, *args: object) -> None:
        """後始末は不要。"""

        return None

    def begin(self) -> _Transaction:
        """テスト用 transaction context を返す。"""

        return _Transaction()


@pytest.mark.asyncio
async def test_create_task_run_freezes_generic_snapshot() -> None:
    """通用 Run が精確 version と Manifest 由来の権限・source を snapshot へ固定する。"""

    resolved = _resolved((_REPOSITORY_SOURCE,))
    captured: dict[str, CreateRunCommand] = {}

    async def _fake_create(self: RunRepository, command: CreateRunCommand) -> CreatedRun:
        """渡された command を捕捉して固定 Run を返す。"""

        captured["command"] = command
        return CreatedRun(
            run_id=uuid4(),
            project_id=command.project_id,
            task_id=command.task_id,
            status=RunStatus.QUEUED,
            row_version=1,
            created_at=datetime(2026, 7, 9, tzinfo=UTC),
            idempotent_replay=False,
        )

    project_id = uuid4()
    actor_id = uuid4()
    with (
        patch.object(RunRepository, "create_idempotent", new=_fake_create),
        patch.object(
            IntegrationRepository,
            "find_configured_binding",
            new=_no_configured_binding,
        ),
    ):
        run = await RunService(_Session).create_task_run(  # type: ignore[arg-type]
            project_id=project_id,
            resolved=resolved,
            input_json={"target": "main"},
            sources={"repository-source": "git"},
            idempotency_key="req-1",
            trace_id="trace-1",
            actor_id=actor_id,
            actor_system_role="ADMIN",
            project_membership="ADMIN_BYPASS",
        )

    assert run.status is RunStatus.QUEUED
    command = captured["command"]
    expected_task_id = uuid5(
        NAMESPACE_URL, f"projectmind:task:{resolved.skill_version_id}:{resolved.task_key}"
    )
    assert command.task_id == expected_task_id
    assert command.task_snapshot_json["task_key"] == "review-change"
    assert command.task_snapshot_json["capability"] == "repository.review"
    assert command.task_snapshot_json["skill_version_id"] == str(resolved.skill_version_id)
    assert command.task_snapshot_json["input_schema"] == resolved.input_schema_checksum
    assert command.task_snapshot_json["input_schema_json"] == resolved.input_schema
    assert command.task_snapshot_json["output_schema_checksum"] == resolved.output_schema_checksum
    assert command.task_snapshot_json["result_kind"] == "OUTCOME_ENVELOPE"
    assert command.task_snapshot_json["task_output_schema_json"] is None
    assert command.task_snapshot_json["skill_snapshots"] == [resolved.skill_snapshot]
    # 扇出は実行時判断のため無条件に付く (計画 §23 D2)。子は Run 予算を分け合うだけで
    # 上限を増やさないので、付与そのものが費用や権限の拡大にはならない。
    assert command.permission_snapshot_json["allowed_capabilities"] == [
        "interaction.request/v1",
        "repository.read/v1",
        "subagent.dispatch/v1",
    ]
    assert command.permission_snapshot_json["denied_builtin_tools"] == list(M0_DENIED_BUILTIN_TOOLS)
    assert command.permission_snapshot_json["actor_id"] == str(actor_id)
    assert command.permission_snapshot_json["execution_profile"] == "SUPERVISED"
    assert command.selected_sources_json == {
        "repository-source": {
            "capability": "repository.read/v1",
            "provider": "git",
            "binding_level": "LEGACY_FIXTURE",
        }
    }
    assert command.limits_snapshot_json["wall_timeout_seconds"] == 900
    assert command.skill_snapshots_json == (resolved.skill_snapshot,)


@pytest.mark.asyncio
async def test_create_task_run_rejects_invalid_source_before_persistence() -> None:
    """Source 選択が不正なら永続化前に拒否する。"""

    with (
        patch.object(RunRepository, "create_idempotent") as create,
        patch.object(
            IntegrationRepository,
            "find_configured_binding",
            new=_no_configured_binding,
        ),
        pytest.raises(TaskSourceSelectionError),
    ):
        await RunService(_Session).create_task_run(  # type: ignore[arg-type]
            project_id=uuid4(),
            resolved=_resolved((_REPOSITORY_SOURCE,)),
            input_json={},
            sources={},
            idempotency_key="req-2",
            trace_id=None,
            actor_id=uuid4(),
        )
    create.assert_not_called()
