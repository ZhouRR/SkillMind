"""SDK に依存せず、SQL 提案の修正可能な誤りを defer より前に返す。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from skillmind.agent.domain import RunContext
from skillmind.agent.proposal_files import expand_proposal_file
from skillmind.effects.postgres_native import SQL_OBSERVATION_MESSAGE, SqlObservationValidationError
from skillmind.runs.service import RunService
from skillmind.worker.tool_authority import ToolExecutionAuthority


class ProposalPreflight:
    """共有 Service の現在権限・凍結契約・Evidence 検査を両 SDK へ接続する。"""

    def __init__(
        self, service: RunService, context: RunContext, authority: ToolExecutionAuthority
    ) -> None:
        """現在の Run/Attempt と読取 workspace を捕捉する。"""
        self._service, self._context, self._authority = service, context, authority

    async def validate(
        self,
        name: str,
        arguments: Mapping[str, Any],
        tool_use_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        """固定診断だけを返し、取消・失効や raw SQL/DB 例外はモデルへ反射しない。"""
        if name != "mcp__skillmind__change_propose_v1":
            return None
        del session_id
        self._authority.require_active()
        error = None
        try:
            expanded = await expand_proposal_file(self._context, arguments)
            await self._service.validate_native_sql_proposal(
                self._authority.claimed,
                expanded,
                tool_use_id=tool_use_id,
            )
        except SqlObservationValidationError:
            error = {
                "status": "error",
                "code": "invalid_request",
                "message": SQL_OBSERVATION_MESSAGE,
                "retryable": False,
            }
        except ValueError:
            error = {
                "status": "error",
                "code": "invalid_request",
                "message": (
                    "Proposal does not match its frozen contract or scope; no write was submitted."
                ),
                "retryable": False,
            }
        except SQLAlchemyError:
            error = {
                "status": "error",
                "code": "unavailable",
                "message": "Proposal validation is unavailable; no write was submitted.",
                "retryable": True,
            }
        self._authority.require_active()
        return error
