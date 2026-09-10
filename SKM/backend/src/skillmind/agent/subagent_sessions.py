"""扇出の子 Session を `agent_sessions` へ保存する port と PostgreSQL 実装 (計画 §23 P3b)。

子 Session の event は主 Session の一回の ToolCall へ畳まれる (D3)。畳んだ結果として、
**この行が一路ごとの唯一の追跡点**になる——ここが無いと `subagent.dispatch/v1` の response が
返す `agent_session_id` はどこからも引けない値になり、監査で「どの路が何を読んだか」を辿れない。

一組の branch は**一 transaction でまとめて**保存する。部分的に書けてしまうと、失敗した路だけが
欠けた記録が残り、「二つの面しか見ていない」が「二つの面は全部見た」と読める。これは P3a で
潰した誤読と同じ形なので、保存側でも起こさない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.db.models import AgentSession
from skillmind.runs.domain import AgentSessionKind, SessionContinuationMode

# branch の結末を AgentSession の lifecycle status へ写す。新しい語彙は増やさない——
# 打ち切りと取消の区別は response と Evidence が持っており、行の役目は lifecycle だけ。
_BRANCH_SESSION_STATUS = {
    "COMPLETED": "CLOSED",
    "FAILED": "FAILED",
    "TIMED_OUT": "INTERRUPTED",
    "CANCELLED": "INTERRUPTED",
}


@dataclass(frozen=True, slots=True)
class SubagentSessionDraft:
    """保存前の一 branch の子 Session。

    engine / model / cwd / 各 version は持たない。子 context は親を `replace()` で狭めたものなので
    これらは構造的に親と同一で、draft 側に持たせると**親と食い違った値を書ける余地**だけが増える。
    保存時に親行から引き継ぐ。
    """

    branch_key: str
    outcome: str
    # SDK が session を開く前に落ちた branch は None。捏造した UUID を入れない。
    sdk_session_id: UUID | None


class SubagentSessionRecorder(Protocol):
    """子 Session を永続化する port。"""

    async def record_branches(
        self,
        *,
        run_id: UUID,
        run_attempt_id: UUID,
        drafts: Sequence[SubagentSessionDraft],
    ) -> tuple[UUID, ...]:
        """drafts を要求順のまま保存し、採番された agent_session_id を同じ順で返す。"""

        ...


class PostgresSubagentSessionRecorder:
    """子 Session を一 transaction で `agent_sessions` へ書く。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Transaction ごとの session factory を保持する。"""

        self._session_factory = session_factory

    async def record_branches(
        self,
        *,
        run_id: UUID,
        run_attempt_id: UUID,
        drafts: Sequence[SubagentSessionDraft],
    ) -> tuple[UUID, ...]:
        """親 PRIMARY Session の下へ子行を並べ、採番順に ID を返す。"""

        if not drafts:
            return ()
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            parent = (
                await session.scalars(
                    select(AgentSession).where(
                        AgentSession.run_attempt_id == run_attempt_id,
                        AgentSession.session_kind == AgentSessionKind.PRIMARY.value,
                    )
                )
            ).one_or_none()
            if parent is None:
                # 親が無いのに子だけを残すと、Run 詳細で宙に浮いた session 行になる。
                raise SubagentSessionRecordingError(
                    "Sub-agent dispatch has no primary Agent session to attach to"
                )
            rows = [
                AgentSession(
                    id=uuid4(),
                    run_id=run_id,
                    run_attempt_id=run_attempt_id,
                    run_segment_id=parent.run_segment_id,
                    sdk_session_id=draft.sdk_session_id,
                    parent_session_id=parent.id,
                    continuation_mode=SessionContinuationMode.BRANCH.value,
                    checkpoint_checksum=None,
                    engine_options_checksum=parent.engine_options_checksum,
                    engine=parent.engine,
                    session_kind=AgentSessionKind.SUBAGENT.value,
                    cwd=parent.cwd,
                    sdk_version=parent.sdk_version,
                    cli_version=parent.cli_version,
                    model=parent.model,
                    status=_BRANCH_SESSION_STATUS.get(draft.outcome, "FAILED"),
                    # branch の対応だけを保持する。子の用量が主に含まれる保証はなく、空 cost
                    # から零消費や Run 共通残額を導出してはならない (予算設計 R02)。
                    usage_json={"branch_key": draft.branch_key},
                    cost_json={},
                    created_at=now,
                    updated_at=now,
                )
                for draft in drafts
            ]
            session.add_all(rows)
            return tuple(row.id for row in rows)


class SubagentSessionRecordingError(RuntimeError):
    """子 Session を保存できる状態でないことを表す。"""


__all__ = [
    "PostgresSubagentSessionRecorder",
    "SubagentSessionDraft",
    "SubagentSessionRecorder",
    "SubagentSessionRecordingError",
]
