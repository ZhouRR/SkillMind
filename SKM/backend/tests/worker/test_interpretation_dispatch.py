"""原要求 ID のみの Outbox 配送と旧 job の拒否を検証する。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from skillmind.core.settings import Settings
from skillmind.worker.settings import (
    adjust_skill_interpretation_job,
    execute_interpretation_request_job,
    interpret_skill_source_job,
    relay_outbox,
)
from tests.worker.test_worker_dispatch import CapturingRelay


@pytest.mark.asyncio
async def test_relay_delivers_only_the_durable_request_identity() -> None:
    """Queue payload の入力や actor は参照せず、aggregate ID だけを配送する。"""

    relay = CapturingRelay()
    redis = MagicMock(publish=AsyncMock(), enqueue_job=AsyncMock())
    result = await relay_outbox(
        {
            "settings": Settings(worker_dispatch_enabled=True, _env_file=None),
            "redis": redis,
            "outbox_relay": relay,
            "skill_service": MagicMock(),
        }
    )
    assert result["dispatch"] == "enabled"
    assert "skill.interpret.dispatch/v1" in relay.topics
    call = redis.enqueue_job.await_args
    assert call.args[0] == "execute_interpretation_request_job"
    assert len(call.args) == 2 and UUID(call.args[1]).int != 0
    assert set(call.kwargs) == {"_job_id", "_queue_name"}
    assert call.kwargs["_job_id"].startswith("interpret-dispatch:")


@pytest.mark.asyncio
async def test_job_uses_request_service_and_legacy_jobs_never_start_models() -> None:
    """新 job は DB 起点の service を使い、旧 actor/入力 job は明示拒否する。"""

    service = MagicMock(execute_interpretation_request=AsyncMock(return_value=None))
    ctx = {
        "settings": Settings(worker_dispatch_enabled=True, _env_file=None),
        "skill_service": service,
    }
    request_id = uuid4()
    result = await execute_interpretation_request_job(ctx, str(request_id))
    assert result == {"status": "no_result", "request_id": str(request_id)}
    call = service.execute_interpretation_request.await_args
    assert call.args == (request_id,)
    assert set(call.kwargs) == {"event_factory"}
    for job in (interpret_skill_source_job, adjust_skill_interpretation_job):
        assert await job(ctx, {"actor_id": str(uuid4())}) == {
            "status": "rejected",
            "reason": "legacy_interpretation_job",
        }
    assert service.execute_interpretation_request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["invalid", str(UUID(int=0)), {"request_id": str(uuid4())}])
async def test_invalid_queue_identity_cannot_claim(identity: object) -> None:
    """破損 job に要求を補造せず、service の取得前に拒否する。"""

    result = await execute_interpretation_request_job(
        {
            "settings": Settings(worker_dispatch_enabled=True, _env_file=None),
        },
        identity,
    )  # type: ignore[arg-type]
    assert result["status"] == "rejected"


@pytest.mark.asyncio
async def test_disabled_dispatch_does_not_claim() -> None:
    """Worker gate が閉じている間は原要求へ副作用を加えない。"""

    result = await execute_interpretation_request_job(
        {
            "settings": Settings(worker_dispatch_enabled=False, _env_file=None),
        },
        str(uuid4()),
    )
    assert result["status"] == "disabled"
