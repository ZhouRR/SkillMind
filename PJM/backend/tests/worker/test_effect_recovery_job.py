"""Run/effect/proposal lease recovery cron の集計と policy 引数を検証する。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from projectmind.worker.settings import recover_expired_leases


class MemoryRunRecovery:
    """RunAttempt と通常 interaction recovery 件数を返す test service。"""

    async def recover_expired_attempts(self, *, limit: int) -> int:
        """Batch limit を確認して固定件数を返す。"""

        assert limit == 25
        return 2

    async def recover_expired_interactions(self, *, limit: int) -> int:
        """通常 interaction timeout も同じ batch 上限で回収する。"""

        assert limit == 25
        return 3


class MemoryEffectRecovery:
    """Effect lease と approval timeout の回収件数を返す test service。"""

    async def recover_expired_effects(self, *, limit: int, max_attempts: int) -> int:
        """Effect retry 上限を Worker settings から受け取る。"""

        assert limit == 25
        assert max_attempts == 3
        return 4

    async def recover_expired_proposals(self, *, limit: int) -> int:
        """未回答 approval も同じ batch 上限で回収する。"""

        assert limit == 25
        return 1


@pytest.mark.asyncio
async def test_recovery_cron_counts_all_durable_timeouts() -> None:
    """四種の durable recovery が一つも取り残されず監査集計へ入る。"""

    result = await recover_expired_leases(
        {
            "run_service": MemoryRunRecovery(),
            "effect_service": MemoryEffectRecovery(),
            "settings": SimpleNamespace(outbox_batch_size=25, run_max_attempts=3),
            "worker_id": "worker-test",
        }
    )

    assert result == {
        "status": "ok",
        "recovered": 10,
        "recovered_runs": 2,
        "recovered_interactions": 3,
        "recovered_effects": 4,
        "recovered_proposals": 1,
    }
