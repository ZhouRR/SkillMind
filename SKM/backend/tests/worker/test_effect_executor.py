"""ApprovedEffectExecutor の claim、例外分類、finalize 境界を検証する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from skillmind.effects.domain import (
    ClaimedEffectExecution,
    EffectExecutionStatus,
    EffectFailure,
    EffectProviderResult,
    StoredEffectExecution,
)
from skillmind.effects.provider import (
    EffectProviderDefinition,
    EffectProviderRegistry,
)
from skillmind.effects.redmine import (
    EffectProviderStaleError,
    EffectProviderTransportError,
    EffectProviderVerificationError,
)
from skillmind.integrations.secrets import DeploymentSecretResolver
from skillmind.worker.effects import ApprovedEffectExecutor


def _claimed() -> ClaimedEffectExecution:
    """Worker が適用できる最小 effect snapshot を返す。"""

    return ClaimedEffectExecution(
        effect_execution_id=uuid4(),
        proposal_id=uuid4(),
        proposal_ref="cp_worker001",
        approval_id=uuid4(),
        run_id=uuid4(),
        run_segment_id=uuid4(),
        run_attempt_id=uuid4(),
        agent_session_id=uuid4(),
        project_id=uuid4(),
        integration_id=uuid4(),
        binding_id=uuid4(),
        capability_version="issue.update/v1",
        operation="set status",
        target={"locator": "42"},
        changes=({"path": "/fields/status_id", "action": "SET", "value": 3},),
        precondition={"revision": "rev-1"},
        verification={"method": "READ_BACK", "paths": ["/fields/status_id"]},
        idempotency_key="effect:worker:0001",
        request_fingerprint="f" * 64,
        provider="redmine",
        integration_revision=1,
        integration_scope={"issue_ids": ["42"], "field_keys": ["status_id"]},
        integration_config={"base_url": "https://redmine.example.test"},
        secret_reference_id=None,
        lease_token="lease-token",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        attempt_no=1,
    )


class MemoryEffectService:
    """Effect transaction port の呼び出しを記録する test double。"""

    def __init__(self, claimed: ClaimedEffectExecution | None) -> None:
        """Claim 結果を固定する。"""

        self.claimed = claimed
        self.finalized: tuple[EffectProviderResult | None, EffectFailure | None] | None = None

    async def claim_effect_execution(
        self,
        effect_execution_id: UUID,
        *,
        worker_id: str,
        lease_seconds: int,
        max_attempts: int,
    ) -> ClaimedEffectExecution | None:
        """Worker policy が渡ることを確認し、固定 claim を返す。"""

        assert isinstance(effect_execution_id, UUID)
        assert worker_id == "worker-test"
        assert lease_seconds == 30
        assert max_attempts == 3
        return self.claimed

    async def resolve_secret_reference(self, claimed: ClaimedEffectExecution) -> None:
        """Secret 不要 Provider 用に None を返す。"""

        assert claimed is self.claimed
        return None

    async def finalize_effect_execution(
        self,
        claimed: ClaimedEffectExecution,
        *,
        result: EffectProviderResult | None,
        failure: EffectFailure | None,
        duration_ms: int,
    ) -> StoredEffectExecution:
        """分類結果を保持し、対応する terminal read model を返す。"""

        assert claimed is self.claimed
        assert duration_ms >= 0
        self.finalized = (result, failure)
        status = failure.status if failure is not None else EffectExecutionStatus.APPLIED
        now = datetime.now(UTC)
        return StoredEffectExecution(
            effect_execution_id=claimed.effect_execution_id,
            proposal_id=claimed.proposal_id,
            run_id=claimed.run_id,
            approval_id=claimed.approval_id,
            tool_call_id=uuid4(),
            status=status,
            provider=claimed.provider,
            provider_version="redmine-cas/v1",
            before_ref="ev_before" if result is not None else None,
            after_ref="ev_after" if result is not None else None,
            verification=dict(result.verification) if result is not None else {},
            error={"code": failure.code} if failure is not None else None,
            attempt_no=claimed.attempt_no,
            executed_at=now,
            created_at=now,
            updated_at=now,
        )


class RaisingProvider:
    """指定した結果または例外を返す effect Provider。"""

    def __init__(self, outcome: EffectProviderResult | BaseException) -> None:
        """apply 時の outcome を保持する。"""

        self.outcome = outcome
        self.calls = 0

    async def apply(
        self, execution: ClaimedEffectExecution, *, credential: str | None
    ) -> EffectProviderResult:
        """呼び出しを記録し、固定 outcome を返す。"""

        assert execution.capability_version == "issue.update/v1"
        assert credential is None
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _executor(
    service: MemoryEffectService, provider: RaisingProvider
) -> ApprovedEffectExecutor:
    """Secret 不要の登録済み test Provider を持つ executor を返す。"""

    return ApprovedEffectExecutor(
        effect_service=service,  # type: ignore[arg-type]
        provider_registry=EffectProviderRegistry(
            (
                EffectProviderDefinition(
                    capability_version="issue.update/v1",
                    provider="redmine",
                    provider_version="redmine-cas/v1",
                    implementation=provider,
                    requires_secret=False,
                ),
            )
        ),
        secret_resolver=DeploymentSecretResolver(),
        worker_id="worker-test",
        lease_seconds=30,
        max_attempts=3,
    )


@pytest.mark.asyncio
async def test_unclaimable_execution_never_reaches_provider() -> None:
    """未承認、既処理または別 Worker lease の effect は Provider 前で無視する。"""

    service = MemoryEffectService(None)
    provider = RaisingProvider(RuntimeError("must not run"))

    outcome = await _executor(service, provider).execute(uuid4())

    assert outcome == "ignored"
    assert provider.calls == 0
    assert service.finalized is None


@pytest.mark.parametrize(
    ("error", "status", "code", "retryable"),
    [
        (EffectProviderStaleError("stale"), EffectExecutionStatus.STALE, "target_stale", False),
        (
            EffectProviderVerificationError("mismatch"),
            EffectExecutionStatus.VERIFICATION_FAILED,
            "read_back_verification_failed",
            False,
        ),
        (
            EffectProviderTransportError("provider_unavailable", retryable=True),
            EffectExecutionStatus.FAILED,
            "provider_unavailable",
            True,
        ),
        (
            RuntimeError("external body must not escape"),
            EffectExecutionStatus.FAILED,
            "effect_provider_failed",
            False,
        ),
    ],
)
@pytest.mark.asyncio
async def test_provider_failures_are_classified_before_persistence(
    error: BaseException,
    status: EffectExecutionStatus,
    code: str,
    retryable: bool,
) -> None:
    """外部例外本文を保存せず、stable code/status/retryability へ閉じる。"""

    service = MemoryEffectService(_claimed())

    outcome = await _executor(service, RaisingProvider(error)).execute(
        service.claimed.effect_execution_id if service.claimed is not None else uuid4()
    )

    assert outcome == status.value
    assert service.finalized is not None
    result, failure = service.finalized
    assert result is None
    assert failure == EffectFailure(status=status, code=code, retryable=retryable)


class _RepositoryProvider:
    """repository.write/v1 の Provider 呼び出しを記録する test double。"""

    def __init__(self, outcome: EffectProviderResult | BaseException) -> None:
        """返す結果と凭据記録を初期化する。"""

        self.outcome = outcome
        self.credentials: list[str | None] = []

    async def apply(
        self, execution: ClaimedEffectExecution, *, credential: str | None
    ) -> EffectProviderResult:
        """capability と凭据の受け渡しを検証しつつ固定 outcome を返す。"""

        assert execution.capability_version == "repository.write/v1"
        self.credentials.append(credential)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _repository_executor(
    service: MemoryEffectService, provider: _RepositoryProvider
) -> ApprovedEffectExecutor:
    """凭据必須で登録した repository.write Provider を持つ executor を返す。"""

    return ApprovedEffectExecutor(
        effect_service=service,  # type: ignore[arg-type]
        provider_registry=EffectProviderRegistry(
            (
                EffectProviderDefinition(
                    capability_version="repository.write/v1",
                    provider="git",
                    provider_version="git-branch-commit/v1",
                    implementation=provider,
                    # push は凭据必須。読取が匿名でも書き込みは必ず SecretReference を要する。
                    requires_secret=True,
                ),
            )
        ),
        secret_resolver=DeploymentSecretResolver(),
        worker_id="worker-test",
        lease_seconds=30,
        max_attempts=3,
    )


@pytest.mark.asyncio
async def test_repository_write_without_credential_fails_before_touching_the_remote() -> None:
    """凭据必須の write Provider は SecretReference 無しで実行させない (計画 §20 R3)。"""

    claimed = replace(
        _claimed(), capability_version="repository.write/v1", provider="git"
    )
    service = MemoryEffectService(claimed)
    provider = _RepositoryProvider(RuntimeError("provider must not be reached"))

    outcome = await _repository_executor(service, provider).execute(
        claimed.effect_execution_id
    )

    assert outcome == EffectExecutionStatus.FAILED.value
    assert provider.credentials == []
    assert service.finalized is not None
    _, failure = service.finalized
    assert failure == EffectFailure(
        status=EffectExecutionStatus.FAILED,
        code="effect_policy_or_configuration_invalid",
        retryable=False,
    )


@pytest.mark.asyncio
async def test_repository_branch_conflict_is_a_terminal_failure() -> None:
    """branch 衝突は再試行しても解けないため、retryable=False で閉じる。"""

    claimed = replace(
        _claimed(),
        capability_version="repository.write/v1",
        provider="git",
        secret_reference_id=None,
    )
    service = MemoryEffectService(claimed)
    provider = _RepositoryProvider(
        EffectProviderTransportError("target_branch_conflict", retryable=False)
    )
    executor = ApprovedEffectExecutor(
        effect_service=service,  # type: ignore[arg-type]
        provider_registry=EffectProviderRegistry(
            (
                EffectProviderDefinition(
                    capability_version="repository.write/v1",
                    provider="git",
                    provider_version="git-branch-commit/v1",
                    implementation=provider,
                    requires_secret=False,
                ),
            )
        ),
        secret_resolver=DeploymentSecretResolver(),
        worker_id="worker-test",
        lease_seconds=30,
        max_attempts=3,
    )

    outcome = await executor.execute(claimed.effect_execution_id)

    assert outcome == EffectExecutionStatus.FAILED.value
    assert service.finalized is not None
    _, failure = service.finalized
    assert failure == EffectFailure(
        status=EffectExecutionStatus.FAILED,
        code="target_branch_conflict",
        retryable=False,
    )
