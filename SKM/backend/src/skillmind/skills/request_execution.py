"""元 Worker の開始 owner を、各 completion の短い許可 transaction に接続する。"""

from __future__ import annotations

from copy import deepcopy

from skillmind.skills.interpretation_requests import (
    InterpretationCallPermit,
    InterpretationRequestDeniedError,
    InterpretationRequestOwner,
)
from skillmind.skills.request_service import InterpretationRequestService


class RequestCallControl:
    """初回と一度の修復に別の許可を取り、取消や DB 不明を再試行しない。"""

    def __init__(
        self, ledger: InterpretationRequestService, owner: InterpretationRequestOwner
    ) -> None:
        """認領成功後の元 token を複製し、他の実行や Queue へ持ち出さない。"""

        self._ledger = ledger
        self._owner = deepcopy(owner)
        self._next_ordinal = 0
        self._permit: InterpretationCallPermit | None = None
        self._usable = True

    async def before_call(self, *, feedback: str | None) -> None:
        """元要求と現在会話を再検証し、許可 commit 成功の観測後だけ戻る。"""

        if not self._usable or self._permit is not None:
            raise InterpretationRequestDeniedError("Previous interpretation call has not returned")
        # rollback と commit 済み応答喪失を区別できなくても、同じ controller は再発行しない。
        self._usable = False
        permit = await self._ledger.start_call(
            self._owner, ordinal=self._next_ordinal, feedback=feedback
        )
        if permit is None:
            raise InterpretationRequestDeniedError("Interpretation call was not authorized")
        self._permit = permit
        self._usable = True

    async def returned(self) -> None:
        """return の保存が不明なら次の ordinal へ進めず、修復呼出しを止める。"""

        if not self._usable or self._permit is None:
            raise InterpretationRequestDeniedError("Interpretation call has no permit")
        self._usable = False
        await self._ledger.record_return(self._permit)
        self._permit = None
        self._next_ordinal += 1
        self._usable = True
