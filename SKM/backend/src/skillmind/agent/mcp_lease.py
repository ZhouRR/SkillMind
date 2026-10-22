"""同じ MCP デスクトップを Run 間で共有せず、不明操作の占有を保持する。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.core.hashing import sha256_hex
from skillmind.core.logging import log_event
from skillmind.db.models import ChangeProposal, EffectExecution, McpDesktopLease, Run

_TERMINAL_RUN_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


class McpDesktopBusyError(RuntimeError):
    """別 Run または未確認操作がデスクトップを使用している。"""


class PendingOperationProbe(Protocol):
    """未確認の原操作が遠端で終了済みかを只読で確認する port。"""

    async def ended(
        self, *, run_id: UUID, effect_id: UUID, config: Mapping[str, Any], credential: str
    ) -> bool:
        """原 requestId の終了回執を確認できた時だけ True。不明・失敗は False。"""
        ...


class McpDesktopLeases:
    """endpoint の正規化識別子を PostgreSQL で直列化し、Worker 間でも保持する。"""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        probe: PendingOperationProbe | None = None,
    ) -> None:
        """業務アプリへの I/O を持たない原 DB factory と、任意の終了確認 port を保持する。"""
        self._sessions = sessions
        self._probe = probe

    async def _release_if_ended(
        self,
        key: str,
        run_id: UUID,
        begin_effect: UUID | None,
        cancel_target: UUID | None,
        config: Mapping[str, Any],
        credential: str,
    ) -> None:
        """旧操作の終了回執を遠端で確認できた時だけ pending を解消する。"""
        assert self._probe is not None
        async with self._sessions() as session:
            lease = await session.get(McpDesktopLease, key)
            if (
                lease is None
                or lease.pending_effect_id is None
                or lease.pending_effect_id in {begin_effect, cancel_target}
            ):
                return
            owner, pending = lease.run_id, lease.pending_effect_id
            if owner != run_id:
                previous = await session.get(Run, owner)
                # 実行中の別 Run の操作は終了回執があっても奪わない。
                if previous is None or previous.status not in _TERMINAL_RUN_STATES:
                    return
        # 遠端 I/O は行ロックを持たない DB transaction の外で行う。
        if not await self._probe.ended(
            run_id=owner, effect_id=pending, config=config, credential=credential
        ):
            return
        async with self._sessions() as session, session.begin():
            lease = (
                await session.scalars(
                    select(McpDesktopLease)
                    .where(McpDesktopLease.endpoint_hash == key)
                    .with_for_update()
                )
            ).one()
            if lease.run_id == owner and lease.pending_effect_id == pending:
                lease.pending_effect_id = None
                log_event(
                    logging.getLogger(__name__),
                    logging.INFO,
                    "mcp.desktop.pending_released",
                    run_id=owner,
                    effect_execution_id=pending,
                    reason_code="probe_confirmed",
                )

    async def acquire(
        self,
        endpoint: str,
        run_id: UUID,
        *,
        begin_effect: UUID | None = None,
        cancel_target: UUID | None = None,
        config: Mapping[str, Any] | None = None,
        credential: str | None = None,
    ) -> dict[str, Any]:
        """原 Run が終わり、遠端呼出しも確認済みの場合だけ次 Run へ占有を移す。

        config と credential がある場合は、未確認の旧操作を先に只読で照会し、終了回執が
        得られた時だけ占有を解く。照会に失敗しても占有は保持する。
        """
        key = sha256_hex(str(httpx.URL(endpoint)))
        if self._probe is not None and config is not None and credential:
            await self._release_if_ended(
                key, run_id, begin_effect, cancel_target, config, credential
            )
        async with self._sessions() as session, session.begin():
            await session.execute(
                insert(McpDesktopLease)
                .values(
                    endpoint_hash=key,
                    run_id=run_id,
                    pending_effect_id=None,
                )
                .on_conflict_do_nothing(index_elements=[McpDesktopLease.endpoint_hash])
            )
            lease = (
                await session.scalars(
                    select(McpDesktopLease)
                    .where(
                        McpDesktopLease.endpoint_hash == key,
                    )
                    .with_for_update()
                )
            ).one()
            if lease.run_id != run_id:
                previous = await session.get(Run, lease.run_id)
                if (
                    lease.pending_effect_id is not None
                    or previous is None
                    or previous.status not in _TERMINAL_RUN_STATES
                ):
                    raise McpDesktopBusyError("MCP desktop is reserved by another Run")
                lease.run_id = run_id
            if begin_effect is not None:
                if lease.pending_effect_id not in {None, begin_effect, cancel_target}:
                    raise McpDesktopBusyError("MCP desktop has an unconfirmed operation")
                lease.pending_effect_id = begin_effect
            return {
                "lease_ref": f"mcp-desktop:{key}:{run_id}",
                "run_id": str(run_id),
                "status": "HELD_BY_RUN",
                "pending_operation": lease.pending_effect_id is not None,
                "scope": "SKM_ENDPOINT_ONLY",
                "external_exclusivity": "NOT_VERIFIED",
            }

    async def require_original_effect(
        self, run_id: UUID, integration_id: UUID, request_id: str
    ) -> None:
        """別 Run の UUID を知っていても結果の読取・取消を許可しない。"""
        async with self._sessions() as session:
            effect = (
                await session.scalars(
                    select(EffectExecution)
                    .join(ChangeProposal, ChangeProposal.id == EffectExecution.proposal_id)
                    .where(
                        EffectExecution.id == UUID(request_id),
                        EffectExecution.run_id == run_id,
                        ChangeProposal.integration_id == integration_id,
                        ChangeProposal.capability_version == "mcp.call/v1",
                        ChangeProposal.operation == "call",
                    )
                )
            ).one_or_none()
            if effect is None:
                raise McpDesktopBusyError("MCP request does not belong to the original Run")

    async def confirm(self, endpoint: str, run_id: UUID, effect_id: UUID) -> None:
        """原操作の終了を回読できた時だけ pending を解消し、Run の占有は継続する。"""
        key = sha256_hex(str(httpx.URL(endpoint)))
        async with self._sessions() as session, session.begin():
            lease = (
                await session.scalars(
                    select(McpDesktopLease)
                    .where(
                        McpDesktopLease.endpoint_hash == key,
                    )
                    .with_for_update()
                )
            ).one()
            if lease.run_id != run_id or lease.pending_effect_id != effect_id:
                raise McpDesktopBusyError("MCP operation ownership changed")
            lease.pending_effect_id = None
