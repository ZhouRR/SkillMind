"""稼働中 Compose 環境で generic Task の happy path と取消 path を反復検証する。"""

from __future__ import annotations

import json
import os
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from email.message import Message
from getpass import getpass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
ACTIVE_STATUSES = frozenset({"RUNNING"})


@dataclass(frozen=True)
class SmokeTask:
    """Smoke が catalog から選択した published task と入力を保持する。"""

    skill_version_id: str
    task_key: str
    input_json: dict[str, Any]
    sources: dict[str, str]


class SmokeApiClient:
    """Cookie、Origin、CSRF を保持して公開 API だけを呼ぶ smoke client。"""

    def __init__(self, base_url: str) -> None:
        """内部 API URL から同源 Origin を固定する。"""

        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("PROJECTMIND_SMOKE_BASE_URL must be an absolute HTTP(S) URL")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.cookies: dict[str, str] = {}
        self.csrf_token: str | None = None

    def login(self, *, email: str, password: str) -> None:
        """一回限り challenge と password で login して session CSRF を保持する。"""

        context = self.request_json("/api/v1/auth/login-context")
        login_csrf = context.get("csrf_token")
        if not isinstance(login_csrf, str):
            raise RuntimeError("Login context did not return a CSRF token")
        session = self.request_json(
            "/api/v1/auth/login",
            method="POST",
            headers={"X-CSRF-Token": login_csrf},
            body={"email": email, "password": password},
        )
        csrf_token = session.get("csrf_token")
        if not isinstance(csrf_token, str):
            raise RuntimeError("Login did not return a session CSRF token")
        self.csrf_token = csrf_token

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """認証付き JSON request を実行し、秘密を含まない失敗だけを返す。"""

        request_headers = {"Accept": "application/json", **(headers or {})}
        if self.cookies:
            request_headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in self.cookies.items()
            )
        if method not in {"GET", "HEAD", "OPTIONS"}:
            request_headers["Origin"] = self.origin
            if "X-CSRF-Token" not in request_headers and self.csrf_token is not None:
                request_headers["X-CSRF-Token"] = self.csrf_token
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=30) as response:
                self._capture_cookies(response.headers)
                value = json.load(response)
        except HTTPError as error:
            raise RuntimeError(
                f"Smoke API request failed: {method} {path} -> {error.code}"
            ) from error
        if not isinstance(value, dict):
            raise RuntimeError(f"Smoke API returned non-object JSON: {method} {path}")
        return cast(dict[str, Any], value)

    def read_sse(
        self,
        path: str,
        *,
        deadline: float | None = None,
        headers: dict[str, str] | None = None,
        stop_event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """認証 cookie 付き SSE を終端または deadline まで読み込む。"""

        request_headers = {"Accept": "text/event-stream", **(headers or {})}
        if self.cookies:
            request_headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in self.cookies.items()
            )
        request = Request(f"{self.base_url}{path}", headers=request_headers)
        events: list[dict[str, Any]] = []
        with urlopen(request, timeout=30) as response:
            for raw_line in response:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                line = raw_line.decode().strip()
                if not line.startswith("data: "):
                    continue
                value = json.loads(line.removeprefix("data: "))
                if isinstance(value, dict) and value.get("event_type") != "TEXT_DELTA":
                    events.append(cast(dict[str, Any], value))
                    if value.get("event_type") == stop_event_type:
                        break
        return events

    def wait_for_sse_event(
        self,
        path: str,
        *,
        event_field: str,
        event_names: frozenset[str],
        deadline: float,
    ) -> dict[str, Any]:
        """指定した終端 event だけを保持し、model delta を memory へ残さず待機する。"""

        request_headers = {"Accept": "text/event-stream"}
        if self.cookies:
            request_headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in self.cookies.items()
            )
        request = Request(f"{self.base_url}{path}", headers=request_headers)
        with urlopen(request, timeout=30) as response:
            for raw_line in response:
                if time.monotonic() >= deadline:
                    break
                line = raw_line.decode().strip()
                if not line.startswith("data: "):
                    continue
                value = json.loads(line.removeprefix("data: "))
                if isinstance(value, dict) and value.get(event_field) in event_names:
                    return cast(dict[str, Any], value)
        raise RuntimeError(f"SSE did not emit a terminal {event_field} before smoke timeout")

    def _capture_cookies(self, headers: Message) -> None:
        """内部 HTTP smoke で API 発行 cookie の値だけを memory に保存する。"""

        # 同一 container から公開 API contract を検証するため、browser transport の
        # Secure 判定は再実装せず、API 発行 cookie を次 request へ引き継ぐ。
        for raw_cookie in headers.get_all("Set-Cookie", []):
            parsed = SimpleCookie()
            parsed.load(raw_cookie)
            for name, morsel in parsed.items():
                if morsel["max-age"] == "0" or not morsel.value:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value


def _create_run(
    client: SmokeApiClient,
    project_id: UUID,
    task: SmokeTask,
    *,
    suffix: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """一意な idempotency key で選択済み published task の Run を作成する。"""

    return client.request_json(
        f"/api/v1/projects/{project_id}/task-runs",
        method="POST",
        headers={"Idempotency-Key": idempotency_key or f"smoke-{suffix}-{uuid4()}"},
        body={
            "skill_version_id": task.skill_version_id,
            "task_key": task.task_key,
            "input": task.input_json,
            "sources": task.sources,
        },
    )


def _read_json_object(name: str, default: str) -> dict[str, Any]:
    """環境変数の JSON object を読み、smoke request 境界で型を限定する。"""

    try:
        value = json.loads(os.getenv(name, default))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{name} must contain a JSON object") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RuntimeError(f"{name} must contain a JSON object")
    return cast(dict[str, Any], value)


def _resolve_smoke_task(client: SmokeApiClient, project_id: UUID) -> SmokeTask:
    """公開 catalog から明示指定または先頭の published task を選択する。"""

    catalog = client.request_json(f"/api/v1/projects/{project_id}/tasks")
    raw_tasks = catalog.get("tasks")
    if not isinstance(raw_tasks, list):
        raise RuntimeError("Task catalog did not contain a tasks array")
    requested_version = os.getenv("PROJECTMIND_SMOKE_SKILL_VERSION_ID", "").strip()
    requested_task_key = os.getenv("PROJECTMIND_SMOKE_TASK_KEY", "").strip()
    selected: dict[str, Any] | None = None
    for raw_task in raw_tasks:
        if not isinstance(raw_task, dict):
            continue
        version_matches = (
            not requested_version
            or raw_task.get("skill_version_id") == requested_version
        )
        key_matches = not requested_task_key or raw_task.get("task_key") == requested_task_key
        if version_matches and key_matches:
            selected = cast(dict[str, Any], raw_task)
            break
    if selected is None:
        raise RuntimeError("No published task matched the smoke task selection")
    skill_version_id = selected.get("skill_version_id")
    task_key = selected.get("task_key")
    if not isinstance(skill_version_id, str) or not isinstance(task_key, str):
        raise RuntimeError("Selected task did not contain a valid version binding")
    raw_sources = _read_json_object("PROJECTMIND_SMOKE_TASK_SOURCES_JSON", "{}")
    if not all(isinstance(value, str) for value in raw_sources.values()):
        raise RuntimeError("PROJECTMIND_SMOKE_TASK_SOURCES_JSON values must be strings")
    return SmokeTask(
        skill_version_id=skill_version_id,
        task_key=task_key,
        input_json=_read_json_object("PROJECTMIND_SMOKE_TASK_INPUT_JSON", "{}"),
        sources=cast(dict[str, str], raw_sources),
    )


def _wait_for_status(
    client: SmokeApiClient,
    run_id: str,
    *,
    accepted: frozenset[str],
    deadline: float,
) -> dict[str, Any]:
    """PostgreSQL projection が指定状態へ到達するまで bounded polling する。"""

    while time.monotonic() < deadline:
        run = client.request_json(f"/api/v1/runs/{run_id}")
        status = run.get("status")
        if isinstance(status, str) and status in accepted:
            return run
        time.sleep(0.2)
    raise RuntimeError(f"Run did not reach {sorted(accepted)} before smoke timeout: {run_id}")


def _read_events(
    client: SmokeApiClient,
    run_id: str,
    *,
    last_event_id: int | None = None,
) -> list[dict[str, Any]]:
    """Terminal Run の有限 SSE replay から永続 event data を抽出する。"""

    headers = {"Last-Event-ID": str(last_event_id)} if last_event_id is not None else None
    return client.read_sse(
        f"/api/v1/runs/{run_id}/events?after=0",
        headers=headers,
    )


def _wait_for_event_type(
    client: SmokeApiClient,
    run_id: str,
    event_type: str,
    deadline: float,
) -> None:
    """Active SSE stream を指定監査 event まで読み、確認後に接続を閉じる。"""

    events = client.read_sse(
        f"/api/v1/runs/{run_id}/events?after=0",
        deadline=deadline,
        stop_event_type=event_type,
    )
    if any(event.get("event_type") == event_type for event in events):
        return
    raise RuntimeError(f"Run did not emit {event_type} before smoke timeout: {run_id}")


def _assert_terminal_audit(events: list[dict[str, Any]], expected_status: str) -> None:
    """Sequence 単調性と terminal RUN_SNAPSHOT が最後である不変条件を検証する。"""

    raw_sequences = [event.get("sequence") for event in events]
    if not raw_sequences or not all(isinstance(sequence, int) for sequence in raw_sequences):
        raise RuntimeError("SSE replay did not contain integer event sequences")
    sequences = cast(list[int], raw_sequences)
    if sequences != sorted(set(sequences)):
        raise RuntimeError("SSE replay event sequences are not strictly increasing")
    last = events[-1]
    payload = last.get("payload")
    if (
        last.get("event_type") != "RUN_SNAPSHOT"
        or not isinstance(payload, dict)
        or payload.get("status") != expected_status
    ):
        raise RuntimeError("Terminal RUN_SNAPSHOT was not the final persisted event")


def _run_happy_path(
    client: SmokeApiClient,
    project_id: UUID,
    task: SmokeTask,
    deadline: float,
) -> None:
    """Run 成功、Result、Evidence、監査 sequence を検証する。"""

    idempotency_key = f"smoke-happy-{uuid4()}"
    created = _create_run(
        client,
        project_id,
        task,
        suffix="happy",
        idempotency_key=idempotency_key,
    )
    replayed = _create_run(
        client,
        project_id,
        task,
        suffix="happy-replay",
        idempotency_key=idempotency_key,
    )
    if (
        replayed.get("run_id") != created.get("run_id")
        or replayed.get("idempotent_replay") is not True
    ):
        raise RuntimeError("Same idempotency key did not replay the original Run")
    run_id = str(created["run_id"])
    terminal = _wait_for_status(
        client,
        run_id,
        accepted=TERMINAL_STATUSES,
        deadline=deadline,
    )
    if terminal.get("status") != "SUCCEEDED":
        raise RuntimeError(f"Happy-path Run did not succeed: {terminal.get('status')}")
    detail = client.request_json(f"/api/v1/projects/{project_id}/runs/{run_id}/detail")
    if not isinstance(detail.get("result"), dict):
        raise RuntimeError("Happy-path Run has no validated Result")
    evidence = detail.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise RuntimeError("Happy-path Run has no Evidence")
    skill_snapshots = detail.get("skill_snapshots")
    if not isinstance(skill_snapshots, list) or len(skill_snapshots) != 1:
        raise RuntimeError("Happy-path Run does not have one frozen SkillVersion binding")
    skill_snapshot = skill_snapshots[0]
    if (
        not isinstance(skill_snapshot, dict)
        or skill_snapshot.get("skill_version_id") != task.skill_version_id
        or skill_snapshot.get("sort_order") != 0
        or not isinstance(skill_snapshot.get("manifest_checksum"), str)
        or not cast(str, skill_snapshot["manifest_checksum"]).startswith("sha256:")
    ):
        raise RuntimeError("Happy-path Run is not bound to the selected SkillVersion")
    original_result = deepcopy(detail["result"])
    evaluation = client.request_json(
        f"/api/v1/projects/{project_id}/runs/{run_id}/evaluations",
        method="POST",
        body={
            "rating": 5,
            "verdict": "accurate",
            "comment": "M0 smoke evaluation",
            "revisions": [],
        },
    )
    history = client.request_json(
        f"/api/v1/projects/{project_id}/runs/{run_id}/evaluations",
    )
    if evaluation.get("evaluation_id") is None or history.get("items") != [evaluation]:
        raise RuntimeError("Evaluation was not appended and replayed unchanged")
    detail_after_evaluation = client.request_json(
        f"/api/v1/projects/{project_id}/runs/{run_id}/detail",
    )
    if detail_after_evaluation.get("result") != original_result:
        raise RuntimeError("Evaluation changed the immutable Result")
    events = _read_events(client, run_id)
    _assert_terminal_audit(events, "SUCCEEDED")
    reconnect_cursor = cast(int, events[len(events) // 2]["sequence"])
    replayed_events = _read_events(client, run_id, last_event_id=reconnect_cursor)
    expected_sequences = [
        event["sequence"] for event in events if cast(int, event["sequence"]) > reconnect_cursor
    ]
    if [event.get("sequence") for event in replayed_events] != expected_sequences:
        raise RuntimeError("SSE replay did not resume strictly after Last-Event-ID")
    print(f"happy-path: SUCCEEDED ({run_id})")


def _run_cancel_path(
    client: SmokeApiClient,
    project_id: UUID,
    task: SmokeTask,
    deadline: float,
) -> None:
    """Active Run を取消し、interrupt 排空後の CANCELLED audit を検証する。"""

    created = _create_run(client, project_id, task, suffix="cancel")
    run_id = str(created["run_id"])
    _wait_for_status(client, run_id, accepted=ACTIVE_STATUSES, deadline=deadline)
    _wait_for_event_type(client, run_id, "SESSION_STARTED", deadline)
    client.request_json(f"/api/v1/runs/{run_id}/cancel", method="POST")
    terminal = _wait_for_status(
        client,
        run_id,
        accepted=TERMINAL_STATUSES,
        deadline=deadline,
    )
    if terminal.get("status") != "CANCELLED":
        raise RuntimeError(f"Cancel-path Run did not reach CANCELLED: {terminal.get('status')}")
    events = _read_events(client, run_id)
    if not any(event.get("event_type") == "RUN_CANCEL_REQUESTED" for event in events):
        raise RuntimeError("Cancel-path audit has no RUN_CANCEL_REQUESTED event")
    if not any(event.get("event_type") == "SESSION_INTERRUPTED" for event in events):
        raise RuntimeError("Cancel-path audit has no SESSION_INTERRUPTED event")
    _assert_terminal_audit(events, "CANCELLED")
    print(f"cancel-path: CANCELLED ({run_id})")


def resolve_smoke_project_id() -> UUID:
    """Smoke/acceptance の対象 Project を環境変数から解決する唯一の実装。

    migration 0024 で seed Project を廃止したため既定値は持たない。既定値を残すと存在しない
    Project へ実行し「task catalog が空」という原因の判りにくい失敗になるため、未設定は
    明示的に拒否する。
    """

    configured = os.getenv("PROJECTMIND_SMOKE_PROJECT_ID")
    if not configured:
        raise SystemExit(
            "PROJECTMIND_SMOKE_PROJECT_ID is required: set it to the project that owns "
            "the published task to exercise"
        )
    return UUID(configured)


def _read_smoke_email() -> str:
    """環境変数または対話入力から smoke 用 login email を取得する。"""

    email = os.getenv("PROJECTMIND_SMOKE_EMAIL", "").strip()
    if not email:
        if not sys.stdin.isatty():
            raise RuntimeError("PROJECTMIND_SMOKE_EMAIL is required without an interactive TTY")
        email = input("Smoke ADMIN email: ").strip()
    if not email:
        raise RuntimeError("Smoke login email must not be empty")
    return email


def _read_smoke_password() -> str:
    """TTY または制限 file から password を読み、argv/environment を避ける。"""

    password_file = os.getenv("PROJECTMIND_SMOKE_PASSWORD_FILE")
    if password_file:
        path = Path(password_file)
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise RuntimeError("Smoke password file must not be readable by group or others")
        password = path.read_text(encoding="utf-8").rstrip("\r\n")
    else:
        if not sys.stdin.isatty():
            raise RuntimeError(
                "Interactive TTY or PROJECTMIND_SMOKE_PASSWORD_FILE is required for smoke login"
            )
        password = getpass("Smoke ADMIN password: ")
    if not password:
        raise RuntimeError("Smoke login password must not be empty")
    return password


def main() -> None:
    """環境変数を読み、一つの deadline 内で二つの generic smoke path を実行する。"""

    base_url = os.getenv("PROJECTMIND_SMOKE_BASE_URL", "http://127.0.0.1:8000")
    timeout_seconds = int(os.getenv("PROJECTMIND_SMOKE_TIMEOUT_SECONDS", "1200"))
    project_id = resolve_smoke_project_id()
    client = SmokeApiClient(base_url)
    client.login(email=_read_smoke_email(), password=_read_smoke_password())
    task = _resolve_smoke_task(client, project_id)
    deadline = time.monotonic() + timeout_seconds
    _run_happy_path(client, project_id, task, deadline)
    _run_cancel_path(client, project_id, task, deadline)
    print("ProjectMind generic task smoke passed")


if __name__ == "__main__":
    main()
