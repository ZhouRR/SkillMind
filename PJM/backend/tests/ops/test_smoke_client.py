"""認証付き M0 smoke client の secret-safe transport を検証する。"""

from __future__ import annotations

import io
from email.message import Message
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock
from urllib.request import Request
from uuid import UUID, uuid4

import pytest

from projectmind.ops import dynamic_contract_acceptance as acceptance
from projectmind.ops import smoke
from projectmind.ops.smoke import SmokeApiClient, SmokeTask


class FakeHttpResponse(io.BytesIO):
    """urllib response の JSON body、header、context manager を再現する。"""

    def __init__(self, body: str, cookies: tuple[str, ...] = ()) -> None:
        """固定 JSON と複数 Set-Cookie header を保持する。"""

        super().__init__(body.encode())
        self.headers = Message()
        for cookie in cookies:
            self.headers.add_header("Set-Cookie", cookie)

    def __enter__(self) -> FakeHttpResponse:
        """urllib response と同じ context manager 値を返す。"""

        return self

    def __exit__(self, *_args: object) -> None:
        """BytesIO を閉じる。"""

        self.close()


def test_smoke_login_replays_secure_cookie_and_csrf_on_internal_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内部 HTTP でも API 発行 cookie と CSRF を unsafe request へ引き継ぐ。"""

    responses = iter((
        FakeHttpResponse(
            '{"csrf_token":"login-token"}',
            ("projectmind_login_csrf=login-token; Secure; HttpOnly; Path=/",),
        ),
        FakeHttpResponse(
            '{"csrf_token":"session-csrf","user":{}}',
            (
                "__Host-projectmind_session=session-token; Secure; HttpOnly; Path=/",
                "projectmind_login_csrf=; Max-Age=0; Path=/",
            ),
        ),
        FakeHttpResponse('{"project_id":null}'),
    ))
    requests: list[Request] = []

    def fake_urlopen(request: Request, *, timeout: int) -> FakeHttpResponse:
        """Request を記録し、順番に固定 response を返す。"""

        assert timeout == 30
        requests.append(request)
        return next(responses)

    monkeypatch.setattr(smoke, "urlopen", fake_urlopen)
    client = smoke.SmokeApiClient("http://127.0.0.1:8000")

    client.login(email="admin@example.com", password="secret value")
    client.request_json(
        "/api/v1/users/me/project-preference",
        method="PUT",
        body={"project_id": None},
    )

    unsafe_request = requests[-1]
    assert unsafe_request.get_header("Origin") == "http://127.0.0.1:8000"
    assert unsafe_request.get_header("X-csrf-token") == "session-csrf"
    assert unsafe_request.get_header("Cookie") == "__Host-projectmind_session=session-token"


def test_wait_for_sse_event_returns_only_requested_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model delta を保持せず、指定 field の終端 event だけを返す。"""

    response = FakeHttpResponse(
        "event: interpret.delta\n"
        'data: {"event":"interpret.delta","data":{"delta":"private output"}}\n\n'
        "event: interpret.completed\n"
        'data: {"event":"interpret.completed","data":{"interpretation_id":"done"}}\n\n'
    )

    def fake_urlopen(request: Request, *, timeout: int) -> FakeHttpResponse:
        """SSE 用 response と固定 timeout を返す。"""

        assert request.get_header("Accept") == "text/event-stream"
        assert timeout == 30
        return response

    monkeypatch.setattr(smoke, "urlopen", fake_urlopen)
    client = smoke.SmokeApiClient("http://127.0.0.1:8000")

    event = client.wait_for_sse_event(
        "/api/v1/interpretations/stream/key",
        event_field="event",
        event_names=frozenset({"interpret.completed", "interpret.failed"}),
        deadline=float("inf"),
    )

    assert event == {
        "event": "interpret.completed",
        "data": {"interpretation_id": "done"},
    }


def test_smoke_password_file_requires_owner_only_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非対話 password file の group/other 公開を fail closed にする。"""

    password_file = tmp_path / "smoke-password"
    password_file.write_text("secret value\n", encoding="utf-8")
    password_file.chmod(0o644)
    monkeypatch.setenv("PROJECTMIND_SMOKE_PASSWORD_FILE", str(password_file))

    with pytest.raises(RuntimeError, match="must not be readable"):
        smoke._read_smoke_password()

    password_file.chmod(0o600)
    assert smoke._read_smoke_password() == "secret value"


def test_smoke_password_never_uses_environment_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Password 本文を environment variable から読む互換経路を追加しない。"""

    monkeypatch.delenv("PROJECTMIND_SMOKE_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(smoke.sys.stdin, "isatty", Mock(return_value=False))
    monkeypatch.setenv("PROJECTMIND_SMOKE_PASSWORD", "must-not-be-read")

    with pytest.raises(RuntimeError, match="TTY or PROJECTMIND_SMOKE_PASSWORD_FILE"):
        smoke._read_smoke_password()


class FakeAcceptanceClient:
    """動的 Contract の公開 API lifecycle を固定 response で再現する。"""

    def __init__(self, *, warning: bool = False) -> None:
        """固定 identity と request 記録領域を初期化する。"""

        self.source_id = str(uuid4())
        self.parent_id = str(uuid4())
        self.adjusted_id = str(uuid4())
        self.version_id = str(uuid4())
        self.warning = warning
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self.wait_count = 0

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Path ごとの acceptance response を返し、呼出順を記録する。"""

        del headers
        self.requests.append((method, path, body))
        if path.endswith("/skill-imports"):
            return {"skill_source_id": self.source_id}
        if path.endswith("?force_regenerate=true"):
            return {"status": "queued", "execution_key": "sha256:" + "a" * 64}
        if path.endswith(f"/{self.parent_id}/adjust"):
            return {"status": "queued", "execution_key": "sha256:" + "b" * 64}
        if path.endswith(f"/{self.parent_id}/execution"):
            return {
                "status": "PREVIEW_READY",
                "interpretation_id": self.parent_id,
                "parent_interpretation_id": None,
            }
        if path.endswith(f"/{self.adjusted_id}/execution"):
            return {
                "status": "PREVIEW_READY",
                "interpretation_id": self.adjusted_id,
                "parent_interpretation_id": self.parent_id,
            }
        if path.endswith(f"/{self.adjusted_id}/draft"):
            findings = (
                [{"code": "unexpected", "severity": "warning"}] if self.warning else []
            )
            return {
                "status": "DRAFT",
                "gate_passed": True,
                "gate_findings": findings,
                "skill_version_id": self.version_id,
            }
        if path.endswith(f"/{self.version_id}/publish"):
            return {"status": "PUBLISHED"}
        if method == "PUT" and path.endswith(f"/skill-versions/{self.version_id}"):
            return {
                "project_id": str(uuid4()),
                "skill_version": {"skill_version_id": self.version_id},
            }
        if path.endswith("/tasks"):
            return {
                "tasks": [
                    {
                        "skill_version_id": self.version_id,
                        "task_key": "review-repository-file",
                        "input_schema": {
                            "$schema": "https://json-schema.org/draft/2020-12/schema",
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["revision", "path", "review_focus"],
                            "properties": {
                                "revision": {"type": "string"},
                                "path": {"type": "string"},
                                "review_focus": {"type": "string"},
                            },
                        },
                        "readiness": {
                            "level": "RUNNABLE",
                            "requirements": [
                                {
                                    "key": "repository-source",
                                    "kind": "repository",
                                    "required": True,
                                    "access": "read",
                                    "status": "AVAILABLE",
                                    "reason": "bound",
                                    "capabilities": ["repository.read/v1"],
                                    "selection_guidance": None,
                                    "candidates": [],
                                }
                            ],
                        },
                    }
                ]
            }
        raise AssertionError(f"Unexpected request: {method} {path}")

    def wait_for_sse_event(
        self,
        path: str,
        *,
        event_field: str,
        event_names: frozenset[str],
        deadline: float,
    ) -> dict[str, Any]:
        """Interpret と adjust の順に終端 event を返す。"""

        del path, deadline
        assert event_field == "event"
        assert event_names == frozenset({"interpret.completed", "interpret.failed"})
        interpretation_id = self.parent_id if self.wait_count == 0 else self.adjusted_id
        self.wait_count += 1
        return {
            "event": "interpret.completed",
            "data": {"interpretation_id": interpretation_id},
        }


def _acceptance_scenario() -> acceptance.AcceptanceScenario:
    """Repository review の最小 acceptance scenario を返す。"""

    return acceptance.AcceptanceScenario(
        name="repository-review",
        directory="repository-review",
        input_hints={
            "revision": "1111111111111111111111111111111111111111",
            "path": "src/example.py",
            "review_focus": "correctness",
        },
    )


def test_accept_scenario_runs_complete_public_api_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Import から evidence 付き Run まで adjusted version を精確に引き渡す。"""

    source = tmp_path / "repository-review"
    source.mkdir()
    (source / "SKILL.md").write_text("# Repository review\n", encoding="utf-8")
    client = FakeAcceptanceClient()
    captured: list[tuple[UUID, SmokeTask]] = []

    def fake_happy_path(
        _client: SmokeApiClient,
        project_id: UUID,
        task: SmokeTask,
        deadline: float,
    ) -> None:
        """Run smoke へ渡る frozen task binding を記録する。"""

        assert deadline == 100.0
        captured.append((project_id, task))

    monkeypatch.setattr(acceptance, "_run_happy_path", fake_happy_path)
    project_id = uuid4()

    acceptance._accept_scenario(
        cast(SmokeApiClient, client),
        project_id,
        tmp_path,
        _acceptance_scenario(),
        deadline=100.0,
    )

    assert client.wait_count == 2
    assert [path for _, path, _ in client.requests] == [
        "/api/v1/skill-imports",
        f"/api/v1/skill-sources/{client.source_id}/interpret"
        "?force_regenerate=true",
        f"/api/v1/skill-interpretations/{client.parent_id}/execution",
        f"/api/v1/skill-interpretations/{client.parent_id}/adjust",
        f"/api/v1/skill-interpretations/{client.adjusted_id}/execution",
        f"/api/v1/skill-interpretations/{client.adjusted_id}/draft",
        f"/api/v1/skill-versions/{client.version_id}/publish",
        f"/api/v1/projects/{project_id}/skill-versions/{client.version_id}",
        f"/api/v1/projects/{project_id}/tasks",
    ]
    assert len(captured) == 1
    task = captured[0][1]
    assert task.skill_version_id == client.version_id
    assert task.input_json == {
        "revision": "1111111111111111111111111111111111111111",
        "path": "src/example.py",
        "review_focus": "correctness",
    }
    assert task.sources == {"repository-source": "git"}


def test_accept_scenario_rejects_unreviewed_publish_warning(tmp_path: Path) -> None:
    """Acceptance automation が未知 warning を黙って受理しないことを保証する。"""

    source = tmp_path / "repository-review"
    source.mkdir()
    (source / "SKILL.md").write_text("# Repository review\n", encoding="utf-8")
    client = FakeAcceptanceClient(warning=True)

    with pytest.raises(RuntimeError, match="unexpected warnings"):
        acceptance._accept_scenario(
            cast(SmokeApiClient, client),
            uuid4(),
            tmp_path,
            _acceptance_scenario(),
            deadline=100.0,
        )
