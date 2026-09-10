"""Worker の原 lease を Provider/SDK context へ公開せず、Tool audit の装配だけへ貸す。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from skillmind.agent.domain import RunContext
from skillmind.runs.domain import ClaimedRun, LeaseValidationError


@dataclass(slots=True, repr=False)
class ToolExecutionAuthority:
    """派生 task と捕捉済み writer が共有し、所有 scope の終了で一斉に失効する権限。"""

    claimed: ClaimedRun = field(repr=False)
    _active: bool = True

    def require_active(self) -> None:
        """ContextVar の reset だけでは残る孤立 task の遅延呼出しを拒否する。"""

        if not self._active:
            raise LeaseValidationError("Tool execution scope is no longer active")


_TOOL_AUTHORITY: ContextVar[ToolExecutionAuthority | None] = ContextVar(
    "skillmind_tool_authority", default=None,
)


@contextmanager
def bind_tool_authority(claimed: ClaimedRun) -> Iterator[None]:
    """Stream を所有する一つの task に束縛し、派生 task に原 identity だけを継承させる。"""

    authority = ToolExecutionAuthority(claimed)
    token = _TOOL_AUTHORITY.set(authority)
    try:
        yield
    finally:
        # 子 task が継承した Context と捕捉済み callback にも終了を通知する。
        authority._active = False
        # anext ごとの task に scope を置くと、別 Context で token を reset してしまう。
        _TOOL_AUTHORITY.reset(token)


def require_tool_authority(context: RunContext) -> ToolExecutionAuthority:
    """本番 MCP の作成時に原 claim を捕捉し、後から別実行の現在値を借用しない。"""

    authority = _TOOL_AUTHORITY.get()
    if authority is None:
        raise LeaseValidationError("Tool runtime has no matching Worker authority")
    claimed = authority.claimed
    if (
        context.run_id != claimed.run_id
        or context.run_attempt_id != claimed.run_attempt_id
        or context.project_id != claimed.project_id
        or context.user_id != claimed.actor_id
    ):
        raise LeaseValidationError("Tool runtime has no matching Worker authority")
    authority.require_active()
    return authority
