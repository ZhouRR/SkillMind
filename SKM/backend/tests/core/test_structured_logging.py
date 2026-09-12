"""Structured logging の correlation と secret-safe allowlist を検証する。"""

from __future__ import annotations

import io
import json
import logging
from uuid import uuid4

import pytest
from skillmind.core.logging import JsonLogFormatter, log_event


@pytest.mark.parametrize("name", ["mcp.client.streamable_http", "httpx", "httpcore.connection"])
def test_transport_diagnostics_never_publish_remote_values(name: str) -> None:
    """SDK 診断の URL・Session ID・本文を、通常/例外 level とも出力しない。"""
    record = logging.LogRecord(
        name,
        logging.ERROR,
        "",
        0,
        "Remote detail %s",
        ("fixture-private-value",),
        None,
    )
    payload = json.loads(JsonLogFormatter().format(record))
    assert payload["event"] == "external_transport.diagnostic"
    assert payload["logger"] == name and payload["level"] == "ERROR"
    assert "fixture-private-value" not in str(payload)


def test_log_event_emits_correlation_ids_without_payload_or_credentials() -> None:
    """Run correlation は JSON 化し、credential と Ticket 本文は出力しない。"""

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("skillmind.test.structured")
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
    logger = logging.getLogger("skillmind.test.brief")
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


def test_interpretation_diagnostics_keep_correlation_without_unregistered_content(caplog) -> None:
    """既存の候補検証ログも実行キー/試行番号を失わず、診断本文は許可しない。"""

    logger = logging.getLogger("skillmind.test.interpretation")
    identity = {"request_id": uuid4(), "skill_source_id": uuid4(),
                "parent_interpretation_id": uuid4(), "interpretation_id": uuid4()}
    with caplog.at_level(logging.WARNING, logger=logger.name):
        log_event(
            logger, logging.WARNING, "skill.interpret.candidate_invalid",
            **identity, execution_key="sha256:" + "a" * 64, attempt=1,
            error_code="invalid_json", provider_error_kind="ProcessError",
            provider_exit_code=17, provider_result_subtype="error_during_execution",
            stderr="fixture-private-stderr", errors=["fixture-private-error"],
            result="fixture-private-result", password="fixture-private-password",
        )
    serialized = JsonLogFormatter().format(caplog.records[-1])
    payload = json.loads(serialized)
    for key, value in identity.items():
        assert payload[key] == str(value)
    assert payload["execution_key"] == "sha256:" + "a" * 64
    assert payload["attempt"] == 1
    assert payload["provider_error_kind"] == "ProcessError"
    assert payload["provider_exit_code"] == 17
    assert payload["provider_result_subtype"] == "error_during_execution"
    assert "fixture-private" not in serialized
