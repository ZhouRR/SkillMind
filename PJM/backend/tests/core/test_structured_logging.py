"""Structured logging の correlation と secret-safe allowlist を検証する。"""

from __future__ import annotations

import io
import json
import logging
from uuid import uuid4

from projectmind.core.logging import JsonLogFormatter, log_event


def test_log_event_emits_correlation_ids_without_payload_or_credentials() -> None:
    """Run correlation は JSON 化し、credential と Ticket 本文は出力しない。"""

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("projectmind.test.structured")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    run_id = uuid4()
    attempt_id = uuid4()
    session_id = uuid4()

    log_event(
        logger,
        logging.INFO,
        "run.execution.test",
        trace_id="trace-1",
        run_id=run_id,
        run_attempt_id=attempt_id,
        agent_session_id=session_id,
        password="credential-must-not-appear",
        input_json={"ticket_id": "SECRET-TICKET", "description": "private body"},
        ticket_text="private body",
    )

    payload = json.loads(stream.getvalue())
    assert payload["event"] == "run.execution.test"
    assert payload["trace_id"] == "trace-1"
    assert payload["run_id"] == str(run_id)
    assert payload["run_attempt_id"] == str(attempt_id)
    assert payload["agent_session_id"] == str(session_id)
    serialized = stream.getvalue()
    assert "credential-must-not-appear" not in serialized
    assert "SECRET-TICKET" not in serialized
    assert "private body" not in serialized
    assert "password" not in payload
    assert "input_json" not in payload


def test_log_event_emits_agent_task_brief_audit_identity() -> None:
    """Brief の監査値は allowlist 済みで、指示正文は出力対象にならない。

    allowlist に載せ忘れた field は例外ではなく黙って捨てられる。Brief の checksum と profile
    が実際に出ることをここで固定しないと、監査点が無いまま気付かれずに残る。
    """

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("projectmind.test.brief")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    log_event(
        logger,
        logging.INFO,
        "run.execution.brief",
        task_brief_checksum="sha256:" + ("a" * 64),
        execution_profile="SUPERVISED",
        task_brief={"guidance": {"required_rules": [{"text": "private rule body"}]}},
    )

    payload = json.loads(stream.getvalue())
    assert payload["task_brief_checksum"] == "sha256:" + ("a" * 64)
    assert payload["execution_profile"] == "SUPERVISED"
    assert "task_brief" not in payload
    assert "private rule body" not in stream.getvalue()
