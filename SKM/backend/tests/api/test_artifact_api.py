"""Artifact の現在認可、原 metadata/bytes と静的失敗を実 API で検証する。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request

from skillmind.api.problems import ProblemException
from skillmind.api.routes import artifacts as routes
from skillmind.artifacts.domain import ArtifactContent, ArtifactIntegrityError, ArtifactMetadata
from skillmind.core.hashing import sha256_hex
from skillmind.runs.domain import CreatedRun, RunStatus
from tests.api.fakes import DeniedProjectAuthorizationService, FakeAuthService, FakeRunService


class ArtifactReadFake:
    """現在の workspace/Storage を持たず、合成された保存値だけを返す fake。"""

    def __init__(self, content: ArtifactContent) -> None:
        """呼出し回数と失敗を観測し、認可前に読まれていないことを確認する。"""

        self.content: ArtifactContent | None = content
        self.expected = content.metadata
        self.failure: Exception | None = None
        self.calls: list[tuple[UUID, UUID, str | None]] = []

    async def list_metadata(
        self, *, project_id: UUID, run_id: UUID,
    ) -> tuple[ArtifactMetadata, ...]:
        """Project/Run の複合 identity を消費し、保存 metadata のみ返す。"""

        self.calls.append((project_id, run_id, None))
        assert (project_id, run_id) == (self.expected.project_id, self.expected.run_id)
        if self.failure is not None:
            raise self.failure
        return (self.content.metadata,) if self.content is not None else ()

    async def get_content(
        self, *, project_id: UUID, run_id: UUID, artifact_ref: str,
    ) -> ArtifactContent | None:
        """原 reference 以外を別の Artifact へ置換しない。"""

        self.calls.append((project_id, run_id, artifact_ref))
        assert (project_id, run_id) == (self.expected.project_id, self.expected.run_id)
        if self.failure is not None:
            raise self.failure
        return self.content if artifact_ref == self.expected.artifact_ref else None


def _install(
    client: TestClient, *, data: bytes = b"Synthetic report.\n", path: str = "output/report.txt",
) -> tuple[str, ArtifactReadFake]:
    """共有認証 fixture を維持し、保存 Run/Artifact だけ外部接続なしで差し替える。"""

    now = datetime(2026, 9, 10, tzinfo=UTC)
    metadata = ArtifactMetadata(
        artifact_ref="art_report", project_id=uuid4(), run_id=uuid4(), tool_call_id=uuid4(),
        evidence_ref="ev_report", path=path, size_bytes=len(data), mime_type="text/plain",
        checksum=f"sha256:{sha256_hex(data)}", created_at=now,
    )
    run_service = FakeRunService()
    run_service.created_run = CreatedRun(
        run_id=metadata.run_id, project_id=metadata.project_id, task_id=uuid4(),
        status=RunStatus.SUCCEEDED, row_version=1, created_at=now, idempotent_replay=False,
    )
    service = ArtifactReadFake(ArtifactContent(metadata=metadata, content=data))
    app = cast(FastAPI, client.app)
    app.state.run_service = run_service
    app.state.artifact_service = service
    return f"/api/v1/projects/{metadata.project_id}/runs/{metadata.run_id}/artifacts", service


def test_list_artifacts_exposes_exact_saved_metadata_without_content(client: TestClient) -> None:
    """十項目だけ返し、正文、内部 locator、現在の file を公開しない。"""

    base, service = _install(client)
    response = client.get(base)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    payload = response.json()
    assert len(payload) == 1 and set(payload[0]) == set(asdict(service.expected))
    assert payload[0]["artifact_ref"] == "art_report"
    assert payload[0]["created_at"] == "2026-09-10T00:00:00Z"
    assert "Synthetic report" not in response.text
    assert service.calls == [(service.expected.project_id, service.expected.run_id, None)]


@pytest.mark.parametrize("data", [b"", b"<script>neverExecute()</script>", "合成本文\n".encode(),
                                   b"x" * 1_048_576])
def test_download_returns_complete_exact_bytes_as_safe_plain_attachment(
    client: TestClient, data: bytes,
) -> None:
    """空/HTML/非 ASCII/最大 byte も再変換せず、Range と条件付き header で断片化しない。"""

    base, _ = _install(client, data=data, path='output/合成"report.html')
    response = client.get(f"{base}/art_report/content", headers={
        "Range": "bytes=0-1", "If-None-Match": "*",
    })

    assert response.status_code == 200 and response.content == data
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert int(response.headers["content-length"]) == len(data)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment; filename=")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert "content-range" not in response.headers and "location" not in response.headers


@pytest.mark.parametrize("operation", ["list", "content"])
@pytest.mark.parametrize("denial", ["unauthenticated", "project", "run", "url_project"])
def test_access_rejection_happens_before_any_artifact_lookup(
    client: TestClient, operation: str, denial: str,
) -> None:
    """存在/越権の差を本文で公開せず、両 Project にアクセスできても URL の帰属を守る。"""

    base, service = _install(client)
    app = cast(FastAPI, client.app)
    if denial == "unauthenticated":
        app.state.auth_service = FakeAuthService(unauthorized=True)
    elif denial == "project":
        app.state.project_service = DeniedProjectAuthorizationService()
    elif denial == "run":
        base = base.replace(str(service.expected.run_id), str(uuid4()))
    else:
        base = base.replace(str(service.expected.project_id), str(uuid4()))
    response = client.get(base if operation == "list" else f"{base}/art_report/content")

    assert response.status_code == (401 if denial == "unauthenticated" else 404)
    assert response.headers["cache-control"] == "no-store"
    assert "x-content-type-options" not in response.headers
    assert service.calls == []


@pytest.mark.parametrize("operation", ["list", "content"])
@pytest.mark.parametrize("status", [403, 422])
def test_dependency_and_path_failures_have_only_promised_security_headers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operation: str, status: int,
) -> None:
    """共通 dependency と path 拒否に本文読取側の nosniff を宣言だけで補わない。"""

    base, service = _install(client)
    if status == 403:
        project_service = cast(FastAPI, client.app).state.project_service
        monkeypatch.setattr(project_service, "get_project", AsyncMock(side_effect=ProblemException(
            status=403, title="Forbidden", detail="Access is forbidden.", code="forbidden",
        )))
    else:
        base = base.replace(str(service.expected.run_id), "invalid-run-id")
    response = client.get(base if operation == "list" else f"{base}/art_report/content")

    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert "x-content-type-options" not in response.headers
    assert service.calls == []


@pytest.mark.parametrize("operation", ["list", "content"])
@pytest.mark.parametrize("failure,code,status", [
    (ArtifactIntegrityError("synthetic-private-never-public"), "artifact_content_invalid", 409),
    (SQLAlchemyError("synthetic-private-never-public"), "artifact_storage_unavailable", 503),
    (TimeoutError("synthetic-private-never-public"), "artifact_storage_unavailable", 503),
    (ConnectionError("synthetic-private-never-public"), "artifact_storage_unavailable", 503),
])
def test_storage_failures_are_static_problems_without_fallback(
    client: TestClient, operation: str, failure: Exception, code: str, status: int,
) -> None:
    """破損と DB 故障を区別し、例外の正文や他の保存場所を応答に持ち込まない。"""

    base, service = _install(client)
    service.failure = failure
    response = client.get(base if operation == "list" else f"{base}/art_report/content")

    assert response.status_code == status
    assert response.json()["code"] == code
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "synthetic-private-never-public" not in response.text
    assert len(service.calls) == 1


def test_missing_artifact_is_not_an_empty_success(client: TestClient) -> None:
    """未知 reference を別の保存結果や空 byte に置き換えない。"""

    base, service = _install(client)
    assert client.get(f"{base}/art_other/content").json()["code"] == "artifact_not_found"
    service.content = None
    assert client.get(base).json() == []
    assert client.get(f"{base}/art_report/content").status_code == 404


def test_readonly_viewer_needs_no_csrf_to_read_saved_artifact(client: TestClient) -> None:
    """読取 capability は発行者だけに限定せず、現在の Project 認可を利用する。"""

    base, _ = _install(client)
    auth = FakeAuthService()
    auth.actor = replace(auth.actor, system_role="VIEWER")
    cast(FastAPI, client.app).state.auth_service = auth
    client.headers.pop("X-CSRF-Token", None)
    assert client.get(base).status_code == 200
    assert client.get(f"{base}/art_report/content").status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list", "content"])
async def test_request_cancellation_is_not_reclassified_as_storage_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    """応答を所有する task の取消は伝播し、503 や代替 byte を作らない。"""

    _base, service = _install(client)
    app = cast(FastAPI, client.app)
    monkeypatch.setattr(routes, "_authorize_run", AsyncMock())
    monkeypatch.setattr(service, "list_metadata", AsyncMock(side_effect=asyncio.CancelledError))
    monkeypatch.setattr(service, "get_content", AsyncMock(side_effect=asyncio.CancelledError))
    request = Request({"type": "http", "app": app})
    with pytest.raises(asyncio.CancelledError):
        if operation == "list":
            await routes.list_artifacts(
                request, Response(), service.expected.project_id, service.expected.run_id,
                app.state.auth_service.actor,
            )
        else:
            await routes.download_artifact(
                request, service.expected.project_id, service.expected.run_id,
                app.state.auth_service.actor, "art_report",
            )
