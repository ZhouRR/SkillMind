"""空の専用 Redis Unix socket で保守と実行の独立性を検証する。DB/モデルには接続しない。"""
from __future__ import annotations

import argparse
import asyncio
import json
from time import monotonic
from uuid import uuid4

from arq.connections import RedisSettings, create_pool
from arq.worker import create_worker, func
from skillmind.core.settings import Settings
from skillmind.runs.domain import PendingOutboxMessage, RelayResult
from skillmind.worker.settings import MaintenanceWorkerSettings, WorkerSettings, relay_outbox


class OneDispatch:
    """原 dispatch publisher に一件の合成 Outbox を渡す。"""

    def __init__(self):
        """別 queue への配送完了を観測する。"""
        self.published = asyncio.Event()
        self.identity = uuid4()

    async def relay_once(self, *, topics, publisher):
        """DB の代わりに固定 message を一度配送する。"""
        assert "run.dispatch.requested/v1" in topics
        await publisher(PendingOutboxMessage(
            message_id=uuid4(), aggregate_type="run", aggregate_id=self.identity,
            topic="run.dispatch.requested/v1", payload={"run_id": str(self.identity)},
            publish_attempts=0,
        ))
        self.published.set()
        return RelayResult(selected=1, published=1, failed=0)


async def probe(socket: str) -> None:
    """双方向の飢餓と、保守 pool の既定 Queue への誤配送を実 ARQ で検出する。"""
    connection = RedisSettings(unix_socket_path=socket)
    business = await create_pool(connection, default_queue_name=WorkerSettings.queue_name)
    maintenance = await create_pool(
        connection, default_queue_name=MaintenanceWorkerSettings.queue_name,
    )
    assert await business.dbsize() == 0, "Use a fresh, isolated Redis process"
    held = asyncio.Event()
    unblock_tick = asyncio.Event()
    running = asyncio.Event()
    unblock_run = asyncio.Event()
    continued = asyncio.Event()
    calls = []
    dispatch = OneDispatch()

    async def hold_tick(ctx):
        """遅い保守 job を再現し、その間に業務が進むことを観測する。"""
        held.set()
        await unblock_tick.wait()

    async def execute_fixture(ctx, identity):
        """実行 slot を占有する合成 job と後続 job を区別する。"""
        calls.append(identity)
        if len(calls) == 1:
            running.set()
            await unblock_run.wait()
        else:
            assert identity == str(dispatch.identity)
            continued.set()

    async def setup_maintenance(ctx):
        """モデル/DB を配線せず、実 relay の宛先設定だけを与える。"""
        ctx.update(settings=Settings(_env_file=None, worker_dispatch_enabled=True,
                                    queue_name=WorkerSettings.queue_name),
                   maintenance_only=True, outbox_relay=dispatch)

    workers = [
        create_worker(WorkerSettings, redis_pool=business, on_startup=None, on_shutdown=None,
                      functions=[func(execute_fixture, name="execute_run")], handle_signals=False),
        create_worker(MaintenanceWorkerSettings, redis_pool=maintenance,
                      on_startup=setup_maintenance, on_shutdown=None, cron_jobs=(),
                      functions=[func(hold_tick, name="hold_tick"), relay_outbox],
                      handle_signals=False),
    ]
    tasks = [asyncio.create_task(worker.async_run()) for worker in workers]
    try:
        async with asyncio.timeout(15):
            await maintenance.enqueue_job("hold_tick")
            await held.wait()
            start = monotonic()
            await business.enqueue_job("execute_run", "first")
            await running.wait()
            initial_delay = monotonic() - start
            assert not unblock_tick.is_set()
            unblock_tick.set()
            # 実行 job が未完了でも保守側の relay が次の業務 job を配送できる。
            await maintenance.enqueue_job("relay_outbox")
            await dispatch.published.wait()
            assert not unblock_run.is_set()
            assert await business.zcard(WorkerSettings.queue_name) >= 1
            held.clear()
            unblock_tick.clear()
            await maintenance.enqueue_job("hold_tick")
            await held.wait()
            start = monotonic()
            unblock_run.set()
            await continued.wait()
            continuation_delay = monotonic() - start
            assert not unblock_tick.is_set()
            assert len(calls) == 2
            assert initial_delay < 2 and continuation_delay < 2
            print(json.dumps({"initial_delay_seconds": initial_delay,
                              "continuation_delay_seconds": continuation_delay,
                              "maintenance_during_run": True, "run_during_maintenance": True}))
    finally:
        unblock_tick.set()
        unblock_run.set()
        await asyncio.gather(*(job for w in workers for job in w.tasks.values()),
                             return_exceptions=True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for worker in workers:
            await worker.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-socket", required=True)
    asyncio.run(probe(parser.parse_args().redis_socket))
