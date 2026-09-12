"""批准された PostgreSQL 単行変更を Worker の Effect port へ接続する。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any
from urllib.parse import quote

from skillmind.effects.database_write import (
    DATABASE_WRITE_CAPABILITY,
    DATABASE_WRITE_PROVIDER_VERSION,
    DatabaseWriteCommand,
    build_database_write,
    database_proposal_payload,
    database_row_revision,
)
from skillmind.effects.domain import (
    ClaimedEffectExecution,
    EffectEvidenceDraft,
    EffectProviderResult,
)
from skillmind.effects.postgres_write import (
    DatabaseWriteConflictError,
    DatabaseWriteReceipt,
    DatabaseWriteUncertainError,
    PostgresDatabaseWriteSource,
)
from skillmind.effects.redmine import EffectProviderStaleError, EffectProviderTransportError


class DatabaseWriteProvider:
    """提案・原 lease・凭据の再検証を必須とし、SQL を Agent に公開しない。"""

    def __init__(
        self,
        *,
        source: PostgresDatabaseWriteSource,
        authorize: Callable[[ClaimedEffectExecution, str], Awaitable[None]],
    ) -> None:
        """原実行権を再検証する共有 service を必須 port として受け取る。"""

        self._source = source
        self._authorize = authorize

    async def apply(
        self, execution: ClaimedEffectExecution, *, credential: str | None
    ) -> EffectProviderResult:
        """再 claim は最初に原回执を照会し、原 identity の直列 transaction だけを使う。"""

        # dataclass の内部 JSON も最初の await 前に固定し、外部変更で宛先を差し替えさせない。
        frozen = deepcopy(execution)
        if frozen.capability_version != DATABASE_WRITE_CAPABILITY or frozen.provider != "postgres":
            raise ValueError("EffectExecution does not target the PostgreSQL Provider")
        if frozen.integration_id is None:
            raise ValueError("Database effect requires an Integration")
        if not credential:
            raise EffectProviderTransportError("credential_unavailable", retryable=False)
        if frozen.attempt_no < 1:
            raise ValueError("Database effect has not been claimed")
        payload = database_proposal_payload(
            operation=frozen.operation,
            target=frozen.target,
            changes=frozen.changes,
            precondition=frozen.precondition,
            verification=frozen.verification,
            scope=frozen.integration_scope,
        )
        command = build_database_write(
            effect_id=frozen.effect_execution_id,
            project_id=frozen.project_id,
            run_id=frozen.run_id,
            integration_id=frozen.integration_id,
            scope=frozen.integration_scope,
            **payload,
        )

        async def authorize() -> None:
            """共有 service が元の批准・実行権・接続・凭据を各段階で復験する。"""

            await self._authorize(frozen, credential)

        try:
            await authorize()
            receipt = None
            if frozen.attempt_no > 1:
                receipt = await self._source.lookup(
                    frozen.integration_config, credential, command, authorize=authorize
                )
            if receipt is None:
                # lookup の不在だけで未実行と断定しない。元 Effect lock の取得後に再照会し、
                # 先行 transaction が生きていれば待ち、同じ主キー/原状態の CAS を守る。
                receipt = await self._source.apply(
                    frozen.integration_config, credential, command, authorize=authorize
                )
        except DatabaseWriteConflictError as error:
            raise EffectProviderStaleError("Database target or receipt has changed") from error
        except DatabaseWriteUncertainError as error:
            # 再試行先は同じ Effect。Worker は成功と扱わず、次回の原回执照会から再開する。
            raise EffectProviderTransportError(
                "database_effect_uncertain", retryable=True
            ) from error
        except PermissionError as error:
            raise EffectProviderTransportError(
                "effect_authority_revoked", retryable=False
            ) from error
        return _result(command, receipt)


def _result(command: DatabaseWriteCommand, receipt: DatabaseWriteReceipt) -> EffectProviderResult:
    """元 transaction の前後事実を Evidence とし、現在行の同値を成功根拠にしない。"""

    return EffectProviderResult(
        before=_evidence(command, receipt.before, phase="before"),
        after=_evidence(command, receipt.after, phase="after"),
        verification={
            "method": "READ_BACK",
            "matched_paths": ["/row"],
            "replayed": receipt.replayed,
            "effect_id": str(command.effect_id),
            "request_checksum": command.checksum,
            "before_revision": database_row_revision(receipt.before),
            "after_revision": database_row_revision(receipt.after),
        },
        replayed=receipt.replayed,
    )


def _evidence(
    command: DatabaseWriteCommand, row: dict[str, Any] | None, *, phase: str
) -> EffectEvidenceDraft:
    """接続先や凭据を含めず、Run 内の原回执と完全主キーを指す証拠を作る。"""

    return EffectEvidenceDraft(
        evidence_type="database",
        source_uri=(
            f"postgres://integration/{command.integration_id}/tables/"
            f"{quote(command.table, safe='')}"
        ),
        source_locator={
            "integration_id": str(command.integration_id),
            "table": command.table,
            "key": command.key,
            "effect_id": str(command.effect_id),
            "phase": phase,
        },
        content={"row": row},
        excerpt=None,
        metadata={
            "provider": "postgres",
            "provider_version": DATABASE_WRITE_PROVIDER_VERSION,
            "request_checksum": command.checksum,
            "original_transaction_receipt": True,
        },
    )
