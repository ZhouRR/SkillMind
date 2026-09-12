"""実 HTTP/台帳 transaction と共有会話検証を接続し、外部 I/O は行わない。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from jsonschema import Draft202012Validator

from skillmind.db.models import OutboxMessage
from tests.effects.test_reconciliation_repository import database as database
from tests.effects.test_reconciliation_repository import original as original
from tests.effects.test_reconciliation_request_service import harness as harness


@pytest.fixture
def connected(client, harness):
    """入口の合成 Cookie と実 service の原会話を揃え、最新検索用の親帰属を SQL に置く。"""
    h = harness
    auth = client.app.state.auth_service
    auth.actor, auth.session_token, auth.csrf_token = (
        h.access.actor,
        h.access.session_token,
        h.access.csrf_token,
    )
    client.cookies.set(client.app.state.settings.auth_session_cookie_name, auth.session_token)
    client.headers["X-CSRF-Token"] = auth.csrf_token
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"worker_dispatch_enabled": True}
    )
    client.app.state.reconciliation_requests = h.service
    h.url = f"/api/v1/projects/{h.reference.project_id}/runs/{h.reference.run_id}"
    h.collection = f"{h.url}/effects/{h.reference.effect_execution_id}/reconciliations"
    with h.database() as (_, session):
        session.execute(sa.text("CREATE TABLE runs (id CHAR(32) PRIMARY KEY, project_id CHAR(32))"))
        session.execute(
            sa.text("CREATE TABLE effect_executions (id CHAR(32) PRIMARY KEY, run_id CHAR(32))")
        )
        session.execute(
            sa.text("INSERT INTO runs VALUES (:id, :project)"),
            {"id": h.reference.run_id.hex, "project": h.reference.project_id.hex},
        )
        session.execute(
            sa.text("INSERT INTO effect_executions VALUES (:id, :run)"),
            {"id": h.reference.effect_execution_id.hex, "run": h.reference.run_id.hex},
        )
    return client, h


def test_accept_confirm_latest_are_scoped_and_exclude_private_ledger(connected):
    """POST/原 ID 確認/最新観測を実装同士で接続し、会話・owner・receipt を公開しない。"""
    client, h = connected
    assert client.get(h.collection + "/latest").json() == {"latest": None}
    body = {"request_id": str(h.original["request_id"])}
    response = client.post(h.collection, json=body)
    assert response.status_code == 202 and response.headers["cache-control"] == "no-store"
    value = response.json()
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3] / "contracts/effects/reconciliation/v1.schema.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(value)
    assert value["status"] == "QUEUED"
    assert set(value) == set(schema["properties"])
    for url in (h.collection + "/latest", f"{h.url}/effect-reconciliations/{body['request_id']}"):
        result = client.get(url)
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert result.json().get("latest", result.json()) == value
    assert client.post(h.collection, json=body).json() == value
    with h.database() as (_, session):
        assert session.scalar(sa.select(sa.func.count()).select_from(OutboxMessage)) == 1
    assert h.current.revoked_at is None


@pytest.mark.parametrize("mutation", ["csrf", "origin", "session", "member", "extra", "dispatch"])
def test_http_rejections_never_create_outbox(connected, mutation):
    """現在の CSRF/Origin/会話/所属と ID-only request、Worker availability を守る。"""
    client, h = connected
    body = {"request_id": str(h.original["request_id"])}
    expected = {
        "csrf": 403,
        "origin": 403,
        "session": 401,
        "member": 404,
        "extra": 422,
        "dispatch": 503,
    }[mutation]
    if mutation == "csrf":
        client.headers["X-CSRF-Token"] = "wrong"
    elif mutation == "origin":
        client.headers["Origin"] = "https://other.example.invalid"
    elif mutation == "session":
        h.current.revoked_at = datetime.now(UTC)
    elif mutation == "member":
        h.member.status = "INACTIVE"
    elif mutation == "extra":
        body["auth_session_id"] = str(h.current.id)
    else:
        client.app.state.settings = client.app.state.settings.model_copy(
            update={"worker_dispatch_enabled": False}
        )
    assert client.post(h.collection, json=body).status_code == expected
    with h.database() as (_, session):
        assert session.scalar(sa.select(sa.func.count()).select_from(OutboxMessage)) == 0


def test_latest_and_confirmation_do_not_cross_run_project_or_effect(connected):
    """原 Project/Run/Effect のいずれかが異なる読取は同じ 404 にし、台帳を返さない。"""
    client, h = connected
    identity = str(h.original["request_id"])
    assert client.post(h.collection, json={"request_id": identity}).status_code == 202
    for original_id in (
        h.reference.project_id,
        h.reference.run_id,
        h.reference.effect_execution_id,
    ):
        url = (h.collection + "/latest").replace(str(original_id), str(uuid4()))
        assert client.get(url).status_code == 404
    url = f"{h.url}/effect-reconciliations/{identity}"
    for original_id in (h.reference.project_id, h.reference.run_id):
        assert client.get(url.replace(str(original_id), str(uuid4()))).status_code == 404


def test_current_read_revocation_applies_to_latest_and_original_request(connected):
    """受理後の失効でも現在の HTTP actor だけを信用せず、共通会話検証が本文を拒否する。"""
    client, h = connected
    identity = str(h.original["request_id"])
    assert client.post(h.collection, json={"request_id": identity}).status_code == 202
    h.current.revoked_at = datetime.now(UTC)
    for url in (h.collection + "/latest", f"{h.url}/effect-reconciliations/{identity}"):
        assert client.get(url).status_code == 401
