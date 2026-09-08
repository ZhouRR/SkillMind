"""作成意図と初回 commit の ORM snapshot を回帰間で共有する。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from projectmind.db.models import Run
from projectmind.runs.creation_request import CREATION_REQUEST_FIELD, TaskRunIntent
from projectmind.runs.domain import CreateRunCommand, RunStatus, derive_task_id, request_hash


def creation_intent(*, sources: dict[str, str] | None = None) -> TaskRunIntent:
    """実資源を参照しない独立 Project/actor/task の作成意図を返す。"""

    return TaskRunIntent(
        project_id=uuid4(),
        skill_version_id=uuid4(),
        task_key="analyze",
        actor_id=uuid4(),
        input_json={"target": "main", "positions": [2, 1]},
        sources=sources or {},
    )


def creation_command(intent: TaskRunIntent, *, legacy: bool = False) -> CreateRunCommand:
    """新版と旧版の違いを request identity の有無だけに限定した command を返す。"""

    task_id = derive_task_id(skill_version_id=intent.skill_version_id, task_key=intent.task_key)
    task = {
        "task_id": str(task_id),
        "task_key": intent.task_key,
        "skill_version_id": str(intent.skill_version_id),
        "skill_snapshots": [],
    }
    if not legacy:
        task[CREATION_REQUEST_FIELD] = intent.to_json()
    return CreateRunCommand(
        project_id=intent.project_id,
        task_id=task_id,
        idempotency_key="original-request",
        input_json=deepcopy(intent.input_json),
        task_snapshot_json=task,
        permission_snapshot_json={"actor_id": str(intent.actor_id), "allowed_capabilities": []},
        selected_sources_json={},
        limits_snapshot_json={"max_turns": 20},
        skill_snapshots_json=(),
        trace_id=None,
    )


def stored_creation(command: CreateRunCommand) -> Run:
    """初回 command の hash と snapshot を持つ、未接続の Run 行を生成する。"""

    now = datetime.now(UTC)
    return Run(
        id=uuid4(),
        project_id=command.project_id,
        task_id=command.task_id,
        trigger_type="immediate",
        idempotency_key=command.idempotency_key,
        request_hash=request_hash(command),
        status=RunStatus.QUEUED.value,
        row_version=1,
        input_json=deepcopy(command.input_json),
        task_snapshot_json=deepcopy(command.task_snapshot_json),
        permission_snapshot_json=deepcopy(command.permission_snapshot_json),
        selected_sources_json=deepcopy(command.selected_sources_json),
        limits_snapshot_json=deepcopy(command.limits_snapshot_json),
        created_at=now,
        updated_at=now,
    )
