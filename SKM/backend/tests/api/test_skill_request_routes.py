"""実要求 service と認可を HTTP/SSE に接続し、DB/Redis 接続だけを合成する。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from starlette.requests import Request

from skillmind.api.problems import ProblemException
from skillmind.api.routes.skills import stream_interpretation_events
from skillmind.db.models import OutboxMessage
from tests.skills.interpretation_request_harness import RequestSession
from tests.skills.test_request_execution import CompletionClient, accept, service


def connect(client: TestClient, session: RequestSession):
    """入口の actor と本番 service の会話を同じ合成資格へ揃える。"""

    completion = CompletionClient(session)
    workflow = service(session, completion)
    client.app.state.skill_service = workflow
    auth = client.app.state.auth_service
    auth.actor = session.access.actor
    auth.session_token = session.access.session_token
    auth.csrf_token = session.access.csrf_token
    client.cookies.set(client.app.state.settings.auth_session_cookie_name, auth.session_token)
    client.headers["X-CSRF-Token"] = auth.csrf_token
    return workflow, completion


def streaming_request(client: TestClient, session: RequestSession) -> Request:
    """本物の cookie/request state 投影を使い、接続切断だけを局部制御する。"""

    cookie = client.app.state.settings.auth_session_cookie_name
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "app": client.app,
            "headers": [(b"cookie", f"{cookie}={session.access.session_token}".encode())],
            "state": {"request_id": str(uuid4())},
        }
    )
    request.is_disconnected = AsyncMock(return_value=False)
    return request


@pytest.mark.asyncio
async def test_http_accept_confirmation_and_content_dedup_use_durable_identity(client) -> None:
    """POST は Outbox と一度だけ受理し、確認は資格/本文を返さず model を呼ばない。"""

    session = RequestSession()
    workflow, completion = connect(client, session)
    original = await accept(session, workflow)
    url = f"/api/v1/skill-interpretation-requests/{original.request_id}"
    response = client.get(url)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {
        "request_id",
        "skill_source_id",
        "status",
        "execution_key",
        "interpretation_id",
        "error_code",
    }
    response = client.post(
        f"/api/v1/skill-sources/{original.skill_source_id}/interpretation-requests",
        json={"request_id": str(uuid4())},
    )
    assert response.status_code == 200
    assert response.json()["request_id"] == str(original.request_id)
    assert len(session.requests) == 1 and not completion.calls
    assert len([row for row in session.rows if isinstance(row, OutboxMessage)]) == 1
    session.auth_session.revoked_at = datetime.now(UTC)
    assert client.get(url).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
async def test_sse_rejects_missing_or_foreign_request_before_redis(client, foreign: bool) -> None:
    """不存在と組織外を同じ 404 にし、通知 channel に触れない。"""

    session = RequestSession()
    workflow, _ = connect(client, session)
    original = await accept(session, workflow)
    identity = uuid4()
    if foreign:
        session.requests[0].organization_id = uuid4()
        identity = original.request_id
    redis = MagicMock(aclose=AsyncMock())
    client.app.state.redis = redis
    with pytest.raises(ProblemException) as caught:
        await stream_interpretation_events(
            streaming_request(client, session), identity, session.access.actor
        )
    assert caught.value.status == 404
    redis.pubsub.assert_not_called()


@pytest.mark.asyncio
async def test_sse_replays_database_terminal_without_redis_subscription(client) -> None:
    """終態は DB から再配信し、通知配送の成功を必要条件にしない。"""

    session = RequestSession()
    workflow, _ = connect(client, session)
    original = await accept(session, workflow)
    await workflow.execute_interpretation_request(original.request_id)
    pubsub = MagicMock(subscribe=AsyncMock(side_effect=RedisError()), aclose=AsyncMock())
    client.app.state.redis = MagicMock(pubsub=MagicMock(return_value=pubsub), aclose=AsyncMock())
    response = await stream_interpretation_events(
        streaming_request(client, session), original.request_id, session.access.actor
    )
    frames = [frame async for frame in response.body_iterator]
    assert len(frames) == 1 and "event: interpret.completed" in frames[0]
    assert str(session.requests[0].interpretation_id) in frames[0]
    client.app.state.redis.pubsub.assert_not_called()
    pubsub.subscribe.assert_not_awaited()
    pubsub.aclose.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", [False, True])
async def test_sse_ignores_forged_terminal_and_reauthenticates_after_wait(
    client, revoke: bool
) -> None:
    """通知の終態は採用せず、Redis 待機中の失効後に本文を送らない。"""

    session = RequestSession()
    workflow, _ = connect(client, session)
    original = await accept(session, workflow)
    calls = 0

    async def next_message(**kwargs):
        """一回目は偽通知または撤権、二回目は接続故障を注入する。"""

        nonlocal calls
        calls += 1
        if calls > 1:
            raise RedisError("synthetic disconnection")
        if revoke:
            session.auth_session.revoked_at = datetime.now(UTC)
        return {
            "data": json.dumps(
                {
                    "event": "interpret.delta" if revoke else "interpret.completed",
                    "execution_key": original.execution_key,
                    "data": {"text": "must not be delivered", "interpretation_id": str(uuid4())},
                }
            )
        }

    pubsub = MagicMock(
        subscribe=AsyncMock(),
        aclose=AsyncMock(),
        get_message=AsyncMock(side_effect=next_message),
    )
    client.app.state.redis = MagicMock(pubsub=MagicMock(return_value=pubsub), aclose=AsyncMock())
    response = await stream_interpretation_events(
        streaming_request(client, session), original.request_id, session.access.actor
    )
    frames = [frame async for frame in response.body_iterator]
    if revoke:
        assert frames == [] and calls == 1
    else:
        assert len(frames) == 1 and "interpret.disconnected" in frames[0] and calls == 2
    assert session.requests[0].status == "QUEUED"
    pubsub.aclose.assert_awaited_once()


def test_request_body_required_and_old_entrypoints_rejected(client) -> None:
    """旧 Web/API の組合せで actor-only job へ黙って後退しない。"""

    identity = uuid4()
    new_path = f"/api/v1/skill-sources/{identity}/interpretation-requests"
    for body in ({}, {"request_id": str(uuid4()), "actor_id": str(uuid4())}):
        assert client.post(new_path, json=body).status_code == 422
    assert client.post(f"/api/v1/skill-sources/{identity}/interpret").status_code == 404
    assert client.post(f"/api/v1/skill-interpretations/{identity}/adjust").status_code == 404
