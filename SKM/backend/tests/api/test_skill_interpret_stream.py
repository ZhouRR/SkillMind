"""Interpret SSE stream の event 選別と wire format を純関数として検証する。"""

from __future__ import annotations

import json

from skillmind.api.routes.skills import (
    _interpret_sse_message,
    _parse_interpret_event,
    _stream_failure,
)


def test_parse_interpret_event_rejects_cross_execution_and_unknown_events() -> None:
    """他 execution・未知 event・不正 JSON を SSE へ通さない。"""

    key = "sha256:" + ("0" * 64)
    valid = {
        "event": "interpret.delta",
        "execution_key": key,
        "occurred_at": "2026-07-12T07:30:00Z",
        "data": {"text": "partial"},
    }

    assert _parse_interpret_event(json.dumps(valid), execution_key=key) == valid
    # 他 execution key の message は混入しない。
    other = {**valid, "execution_key": "sha256:" + ("1" * 64)}
    assert _parse_interpret_event(json.dumps(other), execution_key=key) is None
    # 未登録 event 名は弾く。
    unknown = {**valid, "event": "interpret.unregistered"}
    assert _parse_interpret_event(json.dumps(unknown), execution_key=key) is None
    # data 欠落・非 JSON も弾く。
    assert _parse_interpret_event(json.dumps({**valid, "data": None}), execution_key=key) is None
    assert _parse_interpret_event("not-json", execution_key=key) is None
    assert _parse_interpret_event(None, execution_key=key) is None


def test_interpret_sse_message_uses_named_event_frame() -> None:
    """SSE frame は event 名を named event として持ち、data を 1 行へ収める。"""

    key = "sha256:" + ("0" * 64)
    frame = _interpret_sse_message({
        "event": "interpret.completed",
        "execution_key": key,
        "occurred_at": "2026-07-12T07:31:00Z",
        "data": {"status": "PREVIEW_READY"},
    })

    assert frame.startswith("event: interpret.completed\n")
    assert frame.endswith("\n\n")
    payload = frame.split("data: ", 1)[1].strip()
    assert json.loads(payload)["data"]["status"] == "PREVIEW_READY"


def test_stream_failure_does_not_claim_a_persistent_failure() -> None:
    """Stream 停止は model 未開始や持久 FAILED を意味しない。"""

    key = "sha256:" + ("0" * 64)
    failure = _stream_failure(key, "stream_timeout")

    assert failure["event"] == "interpret.disconnected"
    assert failure["execution_key"] == key
    assert failure["data"]["error_code"] == "stream_timeout"
    assert failure["data"]["interpretation_id"] is None
