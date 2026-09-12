"""Approved EffectExecution の Provider 実行を監督する Worker-side executor。"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import UTC, datetime
from typing import Protocol

from skillmind.core.logging import log_event
from skillmind.effects.domain import (
    ClaimedEffectExecution,
    EffectExecutionStatus,
    EffectFailure,
    EffectLeaseValidationError,
)
from skillmind.effects.provider import EffectProviderRegistry
from skillmind.effects.redmine import (
    EffectProviderStaleError,
    EffectProviderTransportError,
    EffectProviderVerificationError,
)
from skillmind.effects.release import ExecutionFeatureDisabledError
from skillmind.effects.service import EffectService
from skillmind.integrations.secrets import DeploymentSecretResolver, SecretResolutionError

logger = logging.getLogger(__name__)


class EffectExecutor(Protocol):
    """Queue job が effect identity を引き渡す Worker-side port。"""

    async def execute(self, effect_execution_id: object) -> str:
        """Effect を claim/apply/finalize し、処理結果の分類を返す。"""

        ...


class ApprovedEffectExecutor:
    """Approval と scope を再検証した snapshot だけを別 Provider registry で実行する。"""

    def __init__(
        self,
        *,
        effect_service: EffectService,
        provider_registry: EffectProviderRegistry,
        secret_resolver: DeploymentSecretResolver,
        worker_id: str,
        lease_seconds: int,
        max_attempts: int,
        heartbeat_interval_seconds: float | None = None,
        wall_timeout_seconds: float = 300,
    ) -> None:
        """Effect 専用依存、lease と retry 上限を固定する。"""

        interval = (
            lease_seconds / 3 if heartbeat_interval_seconds is None else heartbeat_interval_seconds
        )
        if (
            lease_seconds <= 0 or max_attempts <= 0
            or not math.isfinite(interval) or not 0 < interval < lease_seconds
            or not math.isfinite(wall_timeout_seconds) or wall_timeout_seconds <= 0
        ):
            raise ValueError("Effect supervision limits are invalid")
        self._effect_service = effect_service
        self._provider_registry = provider_registry
        self._secret_resolver = secret_resolver
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._heartbeat_interval_seconds = interval
        self._wall_timeout_seconds = wall_timeout_seconds

    async def execute(self, effect_execution_id: object) -> str:
        """通常結果を永続化し、監督失敗では原実行の照合を回収処理へ委ねる。"""

        from uuid import UUID

        execution_id = (
            effect_execution_id
            if isinstance(effect_execution_id, UUID)
            else UUID(str(effect_execution_id))
        )
        try:
            claimed = await self._effect_service.claim_effect_execution(
                execution_id,
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
                max_attempts=self._max_attempts,
            )
        except ExecutionFeatureDisabledError:
            return "disabled"
        if claimed is None:
            return "ignored"
        definition = self._provider_registry.snapshot().get(
            (claimed.capability_version, claimed.provider)
        )
        if definition is not None and definition.supervised:
            return await self._supervise(claimed, provider_version=definition.provider_version)
        return await self._execute_claimed(claimed)

    async def _supervise(self, claimed: ClaimedEffectExecution, *, provider_version: str) -> str:
        """Provider と finalize を同じ心拍の寿命に置き、失権時は所有 coroutine の清理を待つ。"""

        done = asyncio.Event()

        async def execute() -> str:
            """finalize 応答まで監督し、commit 直後の heartbeat race を終了済みと区別する。"""

            try:
                return await self._execute_claimed(claimed)
            finally:
                done.set()

        try:
            async with asyncio.timeout(self._wall_timeout_seconds), asyncio.TaskGroup() as tasks:
                operation = tasks.create_task(execute())
                tasks.create_task(self._heartbeat(claimed, done, provider_version=provider_version))
            return operation.result()
        except TimeoutError as error:
            # ローカル清理完了は遠端未書込の証明ではない。原 lease/receipt の回収へ委ねる。
            raise EffectLeaseValidationError(
                "Effect execution timed out; original result requires reconciliation"
            ) from error
        except ExceptionGroup as group:
            if len(group.exceptions) == 1:
                raise group.exceptions[0] from None
            raise EffectLeaseValidationError(
                "Effect supervision could not confirm completion"
            ) from None

    async def _heartbeat(
        self, claimed: ClaimedEffectExecution, done: asyncio.Event, *, provider_version: str,
    ) -> None:
        """批准/取消/actor/元 lease を再検査し、失敗した heartbeat から実行権を推定しない。"""

        expires_at = claimed.lease_expires_at
        while True:
            # 確認済み lease の残時間だけを使う。延長 commit の未応答を成功とは扱わない。
            remaining = (expires_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                if done.is_set():
                    return
                raise EffectLeaseValidationError("Effect authority renewal failed")
            try:
                async with asyncio.timeout(remaining):
                    try:
                        await asyncio.wait_for(
                            done.wait(), timeout=self._heartbeat_interval_seconds,
                        )
                        return
                    except TimeoutError:
                        pass
                    expires_at = await self._effect_service.heartbeat_effect_execution(
                        claimed, provider_version=provider_version,
                        lease_seconds=self._lease_seconds,
                    )
            except Exception:
                # finalize 直後だけは正常終了。待機中の失権は sibling を止める。
                if done.is_set():
                    return
                raise EffectLeaseValidationError("Effect authority renewal failed") from None

    async def _execute_claimed(self, claimed: ClaimedEffectExecution) -> str:
        """Provider の通常 outcome を分類し、原 lease を持つ finalize へ引き渡す。"""

        started = time.monotonic()
        result = None
        failure = None
        try:
            definition = self._provider_registry.resolve(
                capability_version=claimed.capability_version,
                provider=claimed.provider,
            )
            reference = await self._effect_service.resolve_secret_reference(claimed)
            credential = self._secret_resolver.resolve(reference) if reference is not None else None
            if definition.requires_secret and credential is None:
                raise SecretResolutionError("Effect Provider requires a SecretReference")
            result = await definition.implementation.apply(
                claimed,
                credential=credential,
            )
        except EffectProviderStaleError:
            failure = EffectFailure(
                status=EffectExecutionStatus.STALE,
                code="target_stale",
                retryable=False,
            )
        except EffectProviderVerificationError:
            failure = EffectFailure(
                status=EffectExecutionStatus.VERIFICATION_FAILED,
                code="read_back_verification_failed",
                retryable=False,
            )
        except EffectProviderTransportError as error:
            failure = EffectFailure(
                status=EffectExecutionStatus.FAILED,
                code=error.code,
                retryable=error.retryable,
            )
        except (SecretResolutionError, LookupError, ValueError):
            failure = EffectFailure(
                status=EffectExecutionStatus.FAILED,
                code="effect_policy_or_configuration_invalid",
                retryable=False,
            )
        except Exception:
            # Provider は外部 system boundary。未知例外の本文を log/DB へ反射せず型にも依存しない
            # 安定 code へ閉じ、秘密や response body の漏洩を防ぐ。
            failure = EffectFailure(
                status=EffectExecutionStatus.FAILED,
                code="effect_provider_failed",
                retryable=False,
            )
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        stored = await self._effect_service.finalize_effect_execution(
            claimed,
            result=result,
            failure=failure,
            duration_ms=duration_ms,
        )
        log_event(
            logger,
            logging.INFO,
            "effect.execution.completed",
            run_id=claimed.run_id,
            proposal_id=claimed.proposal_id,
            effect_execution_id=claimed.effect_execution_id,
            attempt_no=claimed.attempt_no,
            status=stored.status.value,
        )
        return stored.status.value
