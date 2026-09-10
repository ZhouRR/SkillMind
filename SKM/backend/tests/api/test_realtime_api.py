"""SSE transport seam の delta 選別と event ID 契約を検証する。"""

from __future__ import annotations

import json
from uuid import uuid4

from skillmind.api.routes.realtime import _parse_realtime_delta, _sse_message


def test_realtime_delta_parser_rejects_cross_run_and_malformed_messages() -> None:
    """Redis channel の不正 message が他 Run の SSE へ混入しない。"""

    run_id = uuid4()
    valid = {
        "run_id": str(run_id),
        "run_attempt_id": str(uuid4()),
        "agent_session_id": str(uuid4()),
        "sequence": 8,
        "event_type": "TEXT_DELTA",
        "occurred_at": "2026-07-02T13:00:00Z",
        "payload": {"text": "partial"},
        "trace_id": None,
    }

    assert _parse_realtime_delta(json.dumps(valid), run_id=run_id, after=7) == valid
    valid["run_id"] = str(uuid4())
    assert _parse_realtime_delta(json.dumps(valid), run_id=run_id, after=7) is None
    assert _parse_realtime_delta("not-json", run_id=run_id, after=7) is None


def test_realtime_sse_message_does_not_advance_persistent_event_id() -> None:
    """一時 delta が EventSource の PostgreSQL replay cursor を進めない。"""

    message = _sse_message(
        sequence=8,
        event_type="text.delta",
        data={"sequence": 8, "event_type": "TEXT_DELTA"},
        persistent=False,
    )

    assert message.startswith("event: text.delta\n")
    assert "id:" not in message
