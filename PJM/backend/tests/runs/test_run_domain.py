"""Run idempotency と state machine の domain rule を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from projectmind.runs.domain import (
    ALLOWED_RUN_TRANSITIONS,
    CreateRunCommand,
    InvalidRunTransitionError,
    RunStatus,
    plan_run_transition,
    request_hash,
)


def create_command(*, ticket_id: str = "JAF-1234") -> CreateRunCommand:
    """Hash test 用の固定 snapshot command を生成する。"""

    project_id = uuid4()
    task_id = uuid4()
    return CreateRunCommand(
        project_id=project_id,
        task_id=task_id,
        idempotency_key="request-1",
        input_json={"ticket_id": ticket_id, "nested": {"b": 2, "a": 1}},
        task_snapshot_json={"task_id": str(task_id)},
        permission_snapshot_json={"allowed": ["issue.read/v1"]},
        selected_sources_json={"issue_source": "redmine"},
        limits_snapshot_json={"max_turns": 20},
        trace_id=None,
    )


def test_request_hash_is_stable_for_equivalent_json() -> None:
    """JSON key 順序が異なっても idempotency fingerprint が変わらないことを確認する。"""

    command = create_command()
    reordered = CreateRunCommand(
        project_id=command.project_id,
        task_id=command.task_id,
        idempotency_key=command.idempotency_key,
        input_json={"nested": {"a": 1, "b": 2}, "ticket_id": "JAF-1234"},
        task_snapshot_json=command.task_snapshot_json,
        permission_snapshot_json=command.permission_snapshot_json,
        selected_sources_json=command.selected_sources_json,
        limits_snapshot_json=command.limits_snapshot_json,
        trace_id=None,
    )

    assert request_hash(command) == request_hash(reordered)


def test_request_hash_changes_when_input_changes() -> None:
    """同じ idempotency key でも request 内容変更を検出できることを確認する。"""

    original = create_command(ticket_id="JAF-1234")
    changed = CreateRunCommand(
        project_id=original.project_id,
        task_id=original.task_id,
        idempotency_key=original.idempotency_key,
        input_json={"ticket_id": "JAF-9999", "nested": {"b": 2, "a": 1}},
        task_snapshot_json=original.task_snapshot_json,
        permission_snapshot_json=original.permission_snapshot_json,
        selected_sources_json=original.selected_sources_json,
        limits_snapshot_json=original.limits_snapshot_json,
        trace_id=None,
    )

    assert request_hash(original) != request_hash(changed)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current, targets in ALLOWED_RUN_TRANSITIONS.items()
        for target in targets
    ],
)
def test_all_declared_run_transitions_are_accepted(current: RunStatus, target: RunStatus) -> None:
    """State machine に宣言した全 edge が version を一つ進めることを確認する。"""

    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    transition = plan_run_transition(
        current=current,
        target=target,
        row_version=3,
        started_at=None,
        finished_at=None,
        now=now,
    )

    assert transition.status is target
    assert transition.row_version == 4
    assert transition.started_at == (now if target is RunStatus.RUNNING else None)
    assert transition.finished_at == (
        now if target in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED} else None
    )


@pytest.mark.parametrize("terminal", [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED])
def test_terminal_run_cannot_be_reopened(terminal: RunStatus) -> None:
    """終態 Run から新しい実行状態へ戻れないことを確認する。"""

    with pytest.raises(InvalidRunTransitionError):
        plan_run_transition(
            current=terminal,
            target=RunStatus.PREPARING,
            row_version=2,
            started_at=None,
            finished_at=datetime.now(UTC),
            now=datetime.now(UTC),
        )
