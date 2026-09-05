"""Project access 検証付きで永続 replay と realtime delta を統合する SSE route を提供する。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from projectmind.api.auth_dependencies import ReadActor
from projectmind.api.routes.runs import authorized_run
from projectmind.runs.domain import TERMINAL_RUN_STATUSES
from projectmind.runs.realtime import run_realtime_channel
from projectmind.runs.service import RunService

router = APIRouter()


def _sse_message(
    *, sequence: int, event_type: str, data: dict[str, Any], persistent: bool = True
) -> str:
    """RunEvent を SSE wire format へ変換する。"""

    event_id = f"id: {sequence}\n" if persistent else ""
    return (
        f"{event_id}event: {event_type}\n"
        f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _parse_realtime_delta(raw: str, *, run_id: UUID, after: int) -> dict[str, Any] | None:
    """Redis message を同じ Run の新しい TEXT_DELTA だけへ制限する。"""

    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    sequence = value.get("sequence")
    payload = value.get("payload")
    if (
        value.get("run_id") != str(run_id)
        or value.get("event_type") != "TEXT_DELTA"
        or not isinstance(sequence, int)
        or sequence <= after
        or not isinstance(payload, dict)
        or not isinstance(payload.get("text"), str)
        or not payload["text"]
    ):
        return None
    return value


@router.get(
    "/runs/{run_id}/events",
    responses={404: {"description": "Run not found"}},
    tags=["runs"],
)
async def run_events(
    request: Request,
    run_id: UUID,
    actor: ReadActor,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Run 所属 Project access 後に PostgreSQL/Redis SSE stream を開始する。"""

    run = await authorized_run(request, actor, run_id)
    # Header と query の両方を受け付け、browser 再接続と手動 replay の契約を両立する。
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))
    heartbeat = request.app.state.settings.sse_heartbeat_seconds
    service: RunService = request.app.state.run_service

    terminal_statuses = frozenset(status.value for status in TERMINAL_RUN_STATUSES)
    initially_finished = run.status in TERMINAL_RUN_STATUSES

    async def stream() -> Any:
        """DB event と一時 delta を sequence で統合し、待機中は heartbeat を送る。"""

        persisted_sequence = after
        realtime_sequence = after
        last_activity = monotonic()
        last_database_poll = 0.0
        redis: Redis = request.app.state.redis
        pubsub = redis.pubsub()
        realtime_available = True
        run_finished = initially_finished
        try:
            try:
                # DB poll より先に subscribe し、接続開始直後の delta 取りこぼし窓を閉じる。
                await pubsub.subscribe(run_realtime_channel(run_id))
            except RedisError:
                realtime_available = False

            while not await request.is_disconnected():
                events = []
                polled = False
                if monotonic() - last_database_poll >= 1:
                    events = await service.list_events(run_id, after=persisted_sequence)
                    last_database_poll = monotonic()
                    polled = True
                for event in events:
                    persisted_sequence = event.sequence
                    last_activity = monotonic()
                    yield _sse_message(
                        sequence=event.sequence,
                        event_type=event.event_type.lower().replace("_", "."),
                        data={
                            "run_id": str(event.run_id),
                            "run_attempt_id": (
                                str(event.run_attempt_id) if event.run_attempt_id else None
                            ),
                            "agent_session_id": (
                                str(event.agent_session_id) if event.agent_session_id else None
                            ),
                            "sequence": event.sequence,
                            "event_type": event.event_type,
                            "occurred_at": event.occurred_at.isoformat(),
                            "payload": event.payload,
                            "trace_id": event.trace_id,
                        },
                    )
                    if (
                        event.event_type == "RUN_SNAPSHOT"
                        and event.payload.get("status") in terminal_statuses
                    ):
                        run_finished = True
                if run_finished and polled and not events:
                    # 終態 event を配信し切った後は stream を閉じ、DB polling を止める。
                    return

                message = None
                if realtime_available:
                    try:
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True,
                            timeout=0.25,
                        )
                    except RedisError:
                        # Redis 通知障害時も PostgreSQL replay と heartbeat は継続する。
                        realtime_available = False
                if message is not None and isinstance(message.get("data"), str):
                    realtime = _parse_realtime_delta(
                        message["data"],
                        run_id=run_id,
                        after=max(persisted_sequence, realtime_sequence),
                    )
                    if realtime is not None:
                        realtime_sequence = realtime["sequence"]
                        last_activity = monotonic()
                        yield _sse_message(
                            sequence=realtime_sequence,
                            event_type="text.delta",
                            data=realtime,
                            # EventSource reconnect は最後の永続 event から再開させる。
                            persistent=False,
                        )
                if not events and monotonic() - last_activity >= heartbeat:
                    last_activity = monotonic()
                    yield f": heartbeat {datetime.now(UTC).isoformat()}\n\n"
                if not realtime_available:
                    await asyncio.sleep(1)
        finally:
            close_pubsub = cast(Callable[[], Awaitable[None]], pubsub.aclose)
            await close_pubsub()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
