"""保存済み Run の初回要求を検証し、現在の資源へ到達せず再送を判定する。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

from skillmind.db.models import Run
from skillmind.documents.snapshot import DOCUMENT_READ_CAPABILITY
from skillmind.runs.creation_request import CREATION_REQUEST_FIELD, TaskRunIntent
from skillmind.runs.domain import (
    CreateRunCommand,
    IdempotencyConflictError,
    derive_task_id,
    request_hash,
)


def validate_creation_replay(run: Run, requested: TaskRunIntent) -> None:
    """要求と初回の保存事実の両方を照合し、不明な歴史を現在値で補わない。"""

    original = stored_creation_intent(run)
    if original.fingerprint() != requested.fingerprint():
        raise IdempotencyConflictError(
            "Idempotency-Key is already associated with a different request."
        )


def stored_creation_intent(run: Run) -> TaskRunIntent:
    """原要求を歴史形式と hash の共通規則で検証し、読取と削除保護にも再利用する。"""

    try:
        command = _stored_command(run)
        if CREATION_REQUEST_FIELD in command.task_snapshot_json:
            original = TaskRunIntent.from_json(command.task_snapshot_json[CREATION_REQUEST_FIELD])
        else:
            original = _legacy_intent(run, command)
        if (
            original.project_id != run.project_id
            or derive_task_id(
                skill_version_id=original.skill_version_id, task_key=original.task_key
            )
            != run.task_id
            or request_hash(command) != run.request_hash
        ):
            raise ValueError("Stored request integrity could not be verified")
    except (ValueError, TypeError, KeyError) as error:
        # 内部 snapshot や resource locator を Problem detail へ漏らさない。
        raise IdempotencyConflictError(
            "The stored request cannot be safely replayed; "
            "confirm the existing Run before starting another."
        ) from error
    return original


def _stored_command(run: Run) -> CreateRunCommand:
    """初回 commit の可変でない列から hash 検証用 command を復元する。"""

    if not all(
        isinstance(value, dict)
        for value in (
            run.task_snapshot_json,
            run.input_json,
            run.permission_snapshot_json,
            run.selected_sources_json,
            run.limits_snapshot_json,
        )
    ):
        raise ValueError("Stored Run snapshots must be objects")
    task = deepcopy(run.task_snapshot_json)
    skills = task.get("skill_snapshots", [])
    if not isinstance(skills, list) or any(not isinstance(item, dict) for item in skills):
        raise ValueError("Stored Skill snapshots are invalid")
    selected = deepcopy(run.selected_sources_json)
    if CREATION_REQUEST_FIELD not in task:
        # 旧 hash は Run binding の ID/checksum 追記より前に計算された。この三 field だけが
        # 初期化で付加された事実であり、他の metadata は除去・再解決せず旧 hash で確認する。
        for source in selected.values():
            if isinstance(source, dict) and "integration_id" in source:
                for key in ("binding_id", "binding_checksum", "binding_capability"):
                    source.pop(key, None)
    return CreateRunCommand(
        project_id=run.project_id,
        task_id=run.task_id,
        idempotency_key=run.idempotency_key,
        input_json=deepcopy(run.input_json),
        task_snapshot_json=task,
        permission_snapshot_json=deepcopy(run.permission_snapshot_json),
        selected_sources_json=selected,
        limits_snapshot_json=deepcopy(run.limits_snapshot_json),
        trace_id=None,
        skill_snapshots_json=tuple(skills),
    )


def _legacy_intent(run: Run, command: CreateRunCommand) -> TaskRunIntent:
    """旧 task 作成経路が残した明示選択/default の証拠だけを取り出す。"""

    sources: dict[str, str] = {}
    for key, value in command.selected_sources_json.items():
        if not isinstance(value, dict):
            raise ValueError("Legacy resource choice is ambiguous")
        if value.get("capability") == DOCUMENT_READ_CAPABILITY:
            sources[key] = _string(value, "candidate_key")
        elif "integration_id" in value and "source_binding_id" in value:
            source_binding = value["source_binding_id"]
            if source_binding is None:
                sources[key] = _string(value, "candidate_key")
            elif isinstance(source_binding, str):
                UUID(source_binding)
                # 明示 override では source_binding_id は None。値がある場合だけ省略された
                # default 選択と証明でき、現在の Project/Task default は調べない。
            else:
                raise ValueError("Legacy default binding identity is invalid")
        else:
            raise ValueError("Legacy resource choice cannot be determined")
    return TaskRunIntent(
        project_id=run.project_id,
        skill_version_id=UUID(_string(command.task_snapshot_json, "skill_version_id")),
        task_key=_string(command.task_snapshot_json, "task_key"),
        actor_id=UUID(_string(command.permission_snapshot_json, "actor_id")),
        input_json=command.input_json,
        sources=sources,
    )


def _string(value: dict[str, Any], key: str) -> str:
    """復元に必要な文字列が無いとき、推測で既定値を補わず拒否する。"""

    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError("Legacy request identity is missing")
    return result
