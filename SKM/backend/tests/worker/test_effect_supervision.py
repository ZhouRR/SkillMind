"""実 executor の並行心拍・失権・timeout と清理待機を、制御可能な Provider/DB port で検証する。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from skillmind.effects.domain import (
    EffectEvidenceDraft,
    EffectLeaseValidationError,
    EffectProviderResult,
)
from skillmind.effects.provider import EffectProviderDefinition, EffectProviderRegistry
from skillmind.integrations.secrets import DeploymentSecretResolver
from skillmind.worker.effects import ApprovedEffectExecutor
from tests.worker.test_effect_executor import MemoryEffectService, _claimed


class SupervisedService(MemoryEffectService):
    """心拍と finalize の待機位置を制御する。本物の認可は repository 回帰で検証する。"""

    def __init__(self):
        """文書 effect の claim と複数段階の停止点を用意する。"""
        super().__init__(
            replace(
                _claimed(),
                capability_version="document.write/v1",
                provider="project-library",
                integration_id=None,
            )
        )
        self.heartbeats = 0
        self.renewed = asyncio.Event()
        self.error = None
        self.finalizing = asyncio.Event()
        self.finish = asyncio.Event()
        self.finished = asyncio.Event()

    async def heartbeat_effect_execution(self, claimed, *, provider_version, lease_seconds):
        """同じ claim/version/lease duration だけを受け付け、必要なら失権を返す。"""
        assert claimed is self.claimed
        assert provider_version == "project-library-receipt/v2" and lease_seconds == 30
        self.heartbeats += 1
        self.renewed.set()
        if self.error:
            raise self.error
        return datetime.now(UTC) + timedelta(seconds=lease_seconds)

    async def finalize_effect_execution(self, *args, **kwargs):
        """finalize 待機中にも心拍を要求し、応答が終わった位置を通知する。"""
        self.finalizing.set()
        await self.finish.wait()
        result = await super().finalize_effect_execution(*args, **kwargs)
        self.finished.set()
        return result


class WaitingProvider:
    """送信や commit は模さず、所有 coroutine の完了/清理だけを観測する。"""

    def __init__(self):
        """処理、清理の開始と解放を別 Event にする。"""
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.clean_release = asyncio.Event()
        self.clean_release.set()
        self.cleaned = asyncio.Event()

    async def apply(self, execution, *, credential):
        """停止要求を受けても清理の完了までは戻らない Provider を再現する。"""
        self.entered.set()
        try:
            await self.release.wait()
            evidence = EffectEvidenceDraft(
                "document",
                "project-document://fixture/result",
                {},
                {},
                None,
                {},
            )
            return EffectProviderResult(evidence, evidence, {}, False)
        finally:
            self.cleaning.set()
            await self.clean_release.wait()
            self.cleaned.set()


def executor(service, provider, *, timeout=2):
    """短い検査間隔だけを注入し、実 supervisor/例外分類/TaskGroup を通す。"""
    return ApprovedEffectExecutor(
        effect_service=service,
        provider_registry=EffectProviderRegistry(
            (
                EffectProviderDefinition(
                    "document.write/v1",
                    "project-library",
                    "project-library-receipt/v2",
                    provider,
                    False,
                    supervised=True,
                ),
            )
        ),
        secret_resolver=DeploymentSecretResolver(),
        worker_id="worker-test",
        lease_seconds=30,
        max_attempts=3,
        heartbeat_interval_seconds=0.01,
        wall_timeout_seconds=timeout,
    )


async def observed(event):
    """失敗した test を無期限に待たず、必要な段階の到達を待つ。"""
    await asyncio.wait_for(event.wait(), timeout=1)


async def test_heartbeat_covers_provider_and_finalization_until_confirmed_response():
    """I/O と最終 DB 待機の両方で原 lease を維持し、全監督 task が終了してから戻る。"""
    service, provider = SupervisedService(), WaitingProvider()
    task = asyncio.create_task(
        executor(service, provider).execute(service.claimed.effect_execution_id)
    )
    await observed(provider.entered)
    await observed(service.renewed)
    provider.release.set()
    await observed(service.finalizing)
    count = service.heartbeats
    service.renewed.clear()
    await observed(service.renewed)
    assert service.heartbeats > count
    service.finish.set()
    assert await task == "APPLIED"
    assert provider.cleaned.is_set() and service.finished.is_set()


@pytest.mark.parametrize("error", [EffectLeaseValidationError("revoked"), OSError("unavailable")])
async def test_renewal_failure_stops_provider_and_waits_for_owned_cleanup(error):
    """取消/DB 応答未知で新段階へ進めず、失権した Worker は結果を finalize しない。"""
    service, provider = SupervisedService(), WaitingProvider()
    service.error = error
    provider.clean_release.clear()
    task = asyncio.create_task(
        executor(service, provider).execute(service.claimed.effect_execution_id)
    )
    await observed(provider.cleaning)
    assert not task.done() and service.finalized is None
    provider.clean_release.set()
    with pytest.raises(EffectLeaseValidationError, match="renewal failed"):
        await task
    assert provider.cleaned.is_set() and not service.finalizing.is_set()


async def test_wall_deadline_stops_work_even_while_heartbeat_succeeds():
    """心拍の成功を無期限延長とせず、timeout 後も遠端未実行の結果を捏造しない。"""
    service, provider = SupervisedService(), WaitingProvider()
    with pytest.raises(EffectLeaseValidationError, match="requires reconciliation"):
        await executor(service, provider, timeout=0.06).execute(service.claimed.effect_execution_id)
    assert service.heartbeats > 0 and provider.cleaned.is_set()
    assert service.finalized is None


async def test_stalled_renewal_stops_at_last_confirmed_lease(monkeypatch):
    """延長要求が応答しない場合、総 timeout でなく確認済み lease 到期で両 task を止める。"""
    service, provider = SupervisedService(), WaitingProvider()
    service.claimed = replace(
        service.claimed, lease_expires_at=datetime.now(UTC) + timedelta(seconds=0.08)
    )
    heartbeat_cleaned = asyncio.Event()

    async def heartbeat(*args, **kwargs):
        """DB の無応答を再現し、取消後に所有 task が残らないことを記録する。"""
        service.renewed.set()
        try:
            await asyncio.Event().wait()
        finally:
            heartbeat_cleaned.set()

    monkeypatch.setattr(service, "heartbeat_effect_execution", heartbeat)
    with pytest.raises(EffectLeaseValidationError, match="renewal failed"):
        await asyncio.wait_for(
            executor(service, provider).execute(service.claimed.effect_execution_id), timeout=1
        )
    assert service.renewed.is_set() and heartbeat_cleaned.is_set()
    assert provider.cleaned.is_set() and service.finalized is None


async def test_external_task_cancellation_waits_for_cleanup_without_finalizing_failure():
    """ARQ/上位 task の取消も owned coroutine の清理を待ち、失敗結果で事実を上書きしない。"""
    service, provider = SupervisedService(), WaitingProvider()
    provider.clean_release.clear()
    task = asyncio.create_task(
        executor(service, provider).execute(service.claimed.effect_execution_id)
    )
    await observed(provider.entered)
    task.cancel()
    await observed(provider.cleaning)
    assert not task.done()
    provider.clean_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cleaned.is_set() and service.finalized is None


async def test_heartbeat_racing_committed_finalization_does_not_replace_success(monkeypatch):
    """finalize が先に lease を閉じた場合、遅い heartbeat の拒否だけで成功を失敗にしない。"""
    service, provider = SupervisedService(), WaitingProvider()
    heartbeat_release = asyncio.Event()

    async def heartbeat(*args, **kwargs):
        """原結果の返却後に初めて失効した lease の読取を終える。"""
        service.renewed.set()
        await heartbeat_release.wait()
        raise EffectLeaseValidationError("Already finalized")

    monkeypatch.setattr(service, "heartbeat_effect_execution", heartbeat)
    provider.release.set()
    task = asyncio.create_task(
        executor(service, provider).execute(service.claimed.effect_execution_id)
    )
    await observed(service.finalizing)
    await observed(service.renewed)
    service.finish.set()
    await observed(service.finished)
    heartbeat_release.set()
    assert await task == "APPLIED"
    assert service.finalized[1] is None
