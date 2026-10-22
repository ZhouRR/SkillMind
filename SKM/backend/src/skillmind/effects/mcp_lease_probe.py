"""未確認の MCP 原操作を原 requestId で只読照会し、終了回執の有無だけを返す。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.mcp_tools_source import StreamableHttpMcpToolsSource
from skillmind.core.logging import log_event
from skillmind.db.models import Run
from skillmind.effects.mcp_receipt import McpOperationCommand, lookup_operation
from skillmind.runs.repository import RunRepository

# 詰まった Runner が次 Run の acquire を長く止めないよう照会時間を抑える。
PROBE_TIMEOUT_SECONDS = 15


class McpPendingOperationProbe:
    """終了回執を確認できた時だけ True を返し、再送や状態推定はしない。"""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        source: StreamableHttpMcpToolsSource,
    ) -> None:
        """原要求の再構築用 DB factory と、只読照会に使う MCP 通信を保持する。"""
        self._sessions, self._source = sessions, source

    async def ended(
        self, *, run_id: UUID, effect_id: UUID, config: Mapping[str, Any], credential: str
    ) -> bool:
        """同じ endpoint の原操作に対し、承認済み回読条件を満たす回執がある場合だけ True。"""
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                async with self._sessions() as session, session.begin():
                    run = await session.get(Run, run_id)
                    if run is None:
                        return False
                    target = await RunRepository(session).load_effect_reconciliation_target(
                        project_id=run.project_id,
                        run_id=run_id,
                        effect_execution_id=effect_id,
                    )
                command = target.command
                original = json.loads(target.config_json)
                # 別 endpoint の接続で原操作の終了を判定させない。
                if (
                    not isinstance(command, McpOperationCommand)
                    or str(httpx.URL(original["server_url"]))
                    != str(httpx.URL(config["server_url"]))
                ):
                    return False
                return (
                    await lookup_operation(self._source, dict(config), credential, command)
                    is not None
                )
        except Exception:
            # 照会不能は「終了未確認」として占有を保つ。例外本文は接続先を含み得るため出さない。
            log_event(
                logging.getLogger(__name__),
                logging.WARNING,
                "mcp.desktop.pending_probe_failed",
                run_id=run_id,
                effect_execution_id=effect_id,
            )
            return False
