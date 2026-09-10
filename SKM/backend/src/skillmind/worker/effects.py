"""Approved EffectExecution の Provider 実行を監督する Worker-side executor。"""

from __future__ import annotations

import logging
import time
from typing import Protocol

from skillmind.core.logging import log_event
from skillmind.effects.domain import (
    EffectExecutionStatus,
    EffectFailure,
)
from skillmind.effects.provider import EffectProviderRegistry
from skillmind.effects.redmine import (
    EffectProviderStaleError,
    EffectProviderTransportError,
    EffectProviderVerificationError,
)
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
    ) -> None:
        """Effect 専用依存、lease と retry 上限を固定する。"""

        self._effect_service = effect_service
        self._provider_registry = provider_registry
        self._secret_resolver = secret_resolver
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    async def execute(self, effect_execution_id: object) -> str:
        """Provider boundary の例外を安全分類し、必ず durable outcome へ閉じる。"""

        from uuid import UUID

        execution_id = (
            effect_execution_id
            if isinstance(effect_execution_id, UUID)
            else UUID(str(effect_execution_id))
        )
        claimed = await self._effect_service.claim_effect_execution(
            execution_id,
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            max_attempts=self._max_attempts,
        )
        if claimed is None:
            return "ignored"
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
