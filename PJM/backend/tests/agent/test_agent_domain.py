"""SDK 非依存の AgentEngine domain 契約と不変条件を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from projectmind.agent.domain import AgentEvent, AgentEventType, RunLimits, RunWorkspace


def test_workspace_rejects_path_outside_run_root(tmp_path: Path) -> None:
    """cwd を security boundary と誤認せず、全 path を Run root 内へ固定する。"""

    with pytest.raises(ValueError, match="under the run root"):
        root = tmp_path / "run-1"
        RunWorkspace(
            root=root,
            cwd=tmp_path / "escape",
            input_dir=root / "input",
            output_dir=root / "output",
            temp_dir=root / "temp",
        )


def test_run_limits_reject_unbounded_values() -> None:
    """上限値の欠落をゼロで表現して無制限実行になることを防ぐ。"""

    with pytest.raises(ValueError, match="must be positive"):
        RunLimits(max_turns=0, wall_timeout_seconds=900, max_output_bytes=1024)


def test_agent_event_requires_positive_sequence() -> None:
    """Run 内 event sequence がゼロへ戻る不正な event を拒否する。"""

    with pytest.raises(ValueError, match="sequence must be positive"):
        AgentEvent(
            run_id=uuid4(),
            run_attempt_id=uuid4(),
            agent_session_id=str(uuid4()),
            sequence=0,
            occurred_at=datetime.now(UTC),
            event_type=AgentEventType.SESSION_STARTED,
            payload={},
        )
