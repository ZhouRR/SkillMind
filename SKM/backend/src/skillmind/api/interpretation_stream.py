"""Skill 解釈の SSE 配信を扱い、HTTP/Redis の処理を業務 Service と分離する。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast

from fastapi import Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from skillmind.api.problems import ProblemException
from skillmind.skills.interpretation_requests import InterpretationRequestSnapshot
from skillmind.skills.realtime import INTERPRET_EVENT_NAMES, interpret_channel, interpret_event_data

# Interpret job の実行上限より長く保ち、stream 停止を業務 FAILED と混同しない。
_INTERPRET_STREAM_MAX_SECONDS = 1500.0


async def interpretation_events(
    request: Request, *, redis: Redis, heartbeat: float, execution_key: str,
    confirm: Callable[[], Awaitable[InterpretationRequestSnapshot]],
) -> AsyncIterator[str]:
    """帰属確認済み channel を購読して再確認し、通知欠落も DB の read で補う。"""

    pubsub = None
    try:
        try:
            current = await confirm()
        except ProblemException:
            return
        if current.execution_key != execution_key:
            return
        terminal = request_terminal_event(current)
        if terminal is not None:
            yield interpret_sse_message(terminal)
            return
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(interpret_channel(execution_key))
        except RedisError:
            yield interpret_sse_message(stream_failure(execution_key, "stream_unavailable"))
            return
        deadline = monotonic() + _INTERPRET_STREAM_MAX_SECONDS
        last_activity = monotonic()
        while monotonic() < deadline:
            if await request.is_disconnected():
                return
            try:
                current = await confirm()
            except ProblemException:
                return
            if current.execution_key != execution_key:
                return
            terminal = request_terminal_event(current)
            if terminal is not None:
                yield interpret_sse_message(terminal)
                return
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            except RedisError:
                yield interpret_sse_message(
                    stream_failure(execution_key, "stream_unavailable")
                )
                return
            # Redis 待ちの間に失効した会話へ prompt/delta を配信しない。
            try:
                current = await confirm()
            except ProblemException:
                return
            if current.execution_key != execution_key:
                return
            terminal = request_terminal_event(current)
            if terminal is not None:
                yield interpret_sse_message(terminal)
                return
            if message is None:
                if monotonic() - last_activity >= heartbeat:
                    last_activity = monotonic()
                    yield f": heartbeat {datetime.now(UTC).isoformat()}\n\n"
                continue
            value = parse_interpret_event(message.get("data"), execution_key=execution_key)
            if value is None or value["event"] in {
                "interpret.completed", "interpret.failed",
                "interpret.unknown", "interpret.disconnected",
            }:
                continue
            last_activity = monotonic()
            yield interpret_sse_message(value)
        yield interpret_sse_message(stream_failure(execution_key, "stream_timeout"))
    finally:
        if pubsub is not None:
            close_pubsub = cast(Callable[[], Awaitable[None]], pubsub.aclose)
            await close_pubsub()


def request_terminal_event(value: InterpretationRequestSnapshot) -> dict[str, Any] | None:
    """DB の確定/不明状態を通知へ写し、Pub/Sub の自己申告を終態としない。"""

    if value.status in {"QUEUED", "RUNNING"}:
        return None
    name = (
        "interpret.completed" if value.status == "SUCCEEDED"
        else "interpret.unknown" if value.status == "UNKNOWN" else "interpret.failed"
    )
    return interpret_event_data(
        event=name, execution_key=value.execution_key,
        data={
            "request_id": str(value.request_id), "request_status": value.status,
            "interpretation_id": str(value.interpretation_id) if value.interpretation_id else None,
            "status": "PREVIEW_READY" if value.status == "SUCCEEDED" else None,
            "error_code": value.error_code,
        },
    )


def interpret_sse_message(value: dict[str, Any]) -> str:
    """進行 event を named SSE frame へ変換する。"""

    return (
        f"event: {value['event']}\n"
        f"data: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def parse_interpret_event(raw: Any, *, execution_key: str) -> dict[str, Any] | None:
    """Pub/Sub message を同じ execution の既知 event だけへ制限する。"""

    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(value, dict)
        or value.get("execution_key") != execution_key
        or value.get("event") not in INTERPRET_EVENT_NAMES
        or not isinstance(value.get("data"), dict)
    ):
        return None
    return value


def stream_failure(execution_key: str, code: str) -> dict[str, Any]:
    """Stream 停止を通知し、持久 FAILED や model 未開始を補造しない。"""

    return interpret_event_data(
        event="interpret.disconnected",
        execution_key=execution_key,
        data={"interpretation_id": None, "status": None, "error_code": code, "reused": False},
    )
