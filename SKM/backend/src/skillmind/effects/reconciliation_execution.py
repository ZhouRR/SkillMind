"""認領 commit 済みの原要求だけを只読 reader と観測保存へ渡す。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from uuid import UUID

from skillmind.effects.reconciliation_request_service import ReconciliationRequestService
from skillmind.effects.reconciliation_requests import ReconciliationRequestOwner
from skillmind.effects.reconciliation_service import EffectReconciliationService


class EffectReconciliationExecutor:
    """Queue から要求 ID だけを受け、apply/文書公開/Run 续行を持たない。"""

    def __init__(
        self,
        *,
        requests: ReconciliationRequestService,
        reader: EffectReconciliationService,
    ) -> None:
        """認証台帳と只読 service を接続する。秘密は reader の内部だけにある。"""
        self.requests, self.reader = requests, reader

    async def _interrupted(self, owner: ReconciliationRequestOwner, *, code: str) -> None:
        """停止記録は有界の best effort とし、DB 不明なら期限回収へ残す。"""
        with suppress(Exception):
            async with asyncio.timeout(5):
                await self.requests.fail(owner, code=code)

    async def execute(self, request_id: UUID) -> str:
        """一つの owner が一回照会し、重複配送では観測も外部呼出しも作らない。"""
        # commit の返却が不明な場合は owner が無い。再 claim や別 ID へ退避しない。
        async with asyncio.timeout(15):
            owner = await self.requests.claim(request_id)
        if owner is None:
            return "not_claimed"

        async def authorize() -> None:
            """reader の各許可点でも現在の原要求 owner と受理対象を検証する。"""
            await self.requests.authorize(owner)

        try:
            async with asyncio.timeout(45):
                observation = await self.reader.observe(
                    owner.request.reference,
                    accepted_target_checksum=owner.request.target_checksum,
                    authorize_request=authorize,
                )
                await self.requests.finish(owner, observation)
            return "observed"
        except asyncio.CancelledError:
            await self._interrupted(owner, code="lookup_interrupted")
            raise
        except Exception:
            # DB/remote 例外の本文や raw receipt は Queue result に出さず、原要求へ記録する。
            await self._interrupted(owner, code="lookup_unavailable")
            return "unavailable"
