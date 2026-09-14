"""Task の予定管理ブラウザ回帰が共有する合成 HTTP fixture。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from check_accounts import (
    ACTOR,
    CSRF,
    PROJECT,
    ResponseGate,
    session,
)
from check_projects import ProjectsApi
from check_run_submission import VERSION, task_catalog
from check_schedule_times import occurrences
from playwright.async_api import Error, Route

SCHEDULE = "00000000-0000-4000-8000-000000001000"
LIST = f"projects/{PROJECT}/schedules"
DETAIL = f"{LIST}/{SCHEDULE}"
ACTIVITY = f"{DETAIL}/activity"
DETAIL_FACTS = ".scheduleFacts:not([data-schedule-activity])"


def schedule(index: int = 0, project_id: str = PROJECT) -> dict:
    """公開 DTO の全 field を持つ架空履歴。稼働 scheduler の証拠ではない。"""
    return {
        "schedule_id": f"00000000-0000-4000-8000-{1000 + index:012}",
        "project_id": project_id,
        "name": f"Schedule {index:03}" if index < 100 else "Literal_% archived history",
        "kind": "CRON",
        "status": "ACTIVE" if index < 100 else "ARCHIVED",
        "timezone": "Asia/Tokyo",
        "cron_expression": "0 3 * * *",
        "run_at": None,
        "end_at": None,
        "max_runs": 10,
        "skill_version_id": VERSION,
        "task_key": "analyze",
        "input": {"objective": "Original objective"},
        "sources": {},
        "next_run_at": "2027-01-01T18:00:00Z",
        "last_run_at": "2026-09-09T00:00:00Z",
        "last_run_id": None,
        "last_outcome": "SKIPPED_OVERLAP",
        "last_error": None,
        "run_count": 2,
        "missed_count": 3,
        "created_by": ACTOR,
        "row_version": 7,
        "created_at": "2026-09-08T00:00:00Z",
        "updated_at": "2026-09-09T00:00:00Z",
    }


def activity(*, legacy: bool = False, lease: str | None = None, attempts: int = 1) -> dict:
    """原 input/token を含まない読取投影。server checked_at だけで期限表示を決める。"""
    pending = (
        None
        if lease is None
        else {
            "occurrence_id": "00000000-0000-4000-8000-000000002001",
            "occurrence_at": "2026-09-09T00:00:00Z",
            "configuration_version": 3,
            "created_at": "2026-09-09T00:00:05Z",
            "updated_at": "2026-09-09T00:00:05Z",
            "attempt_count": attempts,
            "lease_expires_at": "2026-09-09T00:01:05Z"
            if lease == "active"
            else "2026-09-09T00:00:20Z",
        }
    )
    return {
        "schedule_id": SCHEDULE,
        "project_id": PROJECT,
        "row_version": 7,
        "configuration_version": 1 if legacy else 4,
        "tracking": "LEGACY_UNAVAILABLE" if legacy else "TRACKED",
        "checked_at": "2026-09-09T00:00:30Z",
        "automatic_attempt_limit": 3,
        "pending": pending,
    }


class ScheduleManagementApi(ProjectsApi):
    """認証/Project fixture を再利用し、調度の既知 read/write だけ追加する。"""

    def __init__(self, url: str, language: str) -> None:
        """各 case の原版・一覧・late 応答を独立させる。"""
        super().__init__(url, language)
        self.records = [schedule(index) for index in range(101)]
        self.catalog = task_catalog()
        self.catalog["tasks"][0]["readiness"]["requirements"] = []
        self.exact: dict[str, dict] = {}
        self.activities: dict[str, dict] = {}
        self.actions: list[str] = []
        self.writes: list[dict] = []
        self.write_gate = ResponseGate()
        self.read_gates: dict[tuple[str, str], ResponseGate] = {}
        self.read_failures: dict[str, int] = {}

    async def respond(self, route: Route) -> None:
        """外部通信は親が拒絶。既知 mock でも元版と CSRF を照合する。"""
        request = route.request
        url = urlsplit(request.url)
        suffix = url.path.removeprefix(self.prefix)
        if suffix == "auth/session":
            await route.fulfill(
                json={
                    **session(self.actor, self.role),
                    "deferred_features_enabled": False,
                    "scheduling_enabled": True,
                }
            )
            return
        parts = suffix.split("/")
        if (
            f"{url.scheme}://{url.netloc}" != self.origin
            or len(parts) < 3
            or parts[0] != "projects"
            or parts[2] not in {"schedules", "tasks"}
        ):
            await super().respond(route)
            return
        query = parse_qs(url.query)
        method = request.method
        body = request.post_data_json if request.post_data else None
        self.calls.append((method, suffix, query, body))
        result: object
        if method == "GET":
            failure = self.read_failures.get(suffix)
            gate = self.read_gates.get((suffix, query.get("q", [""])[0]))
            if parts[2] == "tasks":
                result = deepcopy(self.catalog)
            elif len(parts) == 5 and parts[-1] == "activity":
                result = deepcopy(self.activities.get(parts[3], activity()))
                result.update({"schedule_id": parts[3], "project_id": parts[1]})
            elif len(parts) == 3:
                limit, offset = int(query["limit"][0]), int(query["offset"][0])
                q = query.get("q", [""])[0].lower()
                state = query.get("status", [None])[0]
                records = [
                    deepcopy(record)
                    for record in self.records
                    if (q in record["name"].lower() or q in record["task_key"].lower())
                    and (not state or record["status"] == state)
                ]
                for record in records:
                    record["project_id"] = parts[1]
                result = {
                    "schedules": records[offset : offset + limit],
                    "total": len(records),
                    "limit": limit,
                    "offset": offset,
                }
            else:
                matches = [record for record in self.records if record["schedule_id"] == parts[3]]
                result = deepcopy(self.exact.get(parts[3], matches[0] if matches else {}))
                if not result:
                    failure = 404
                else:
                    result["project_id"] = parts[1]
            if gate:
                gate.received.set()
                await asyncio.wait_for(gate.release.wait(), 40)
                if gate.failure:
                    failure = gate.failure[0]
            try:
                if failure:
                    await self.refuse(route, failure)
                else:
                    await route.fulfill(json=result)
            finally:
                if gate:
                    gate.returned.set()
            return
        assert request.headers.get("x-csrf-token") == CSRF
        assert request.headers.get("origin") == self.origin
        if method == "POST" and len(parts) == 3:
            self.writes.append({"method": method, "suffix": suffix, "body": deepcopy(body)})
            saved = schedule(200)
            saved.update(
                {
                    key: body[key]
                    for key in (
                        "name",
                        "skill_version_id",
                        "task_key",
                        "input",
                        "sources",
                    )
                }
            )
            saved.update(body["definition"])
            saved.update(status="ACTIVE", row_version=1, run_count=0, missed_count=0)
            self.records.insert(0, saved)
            await route.fulfill(status=201, json=saved)
            return
        if method == "POST" and parts[-1] == "preview":
            await route.fulfill(json={"occurrences": occurrences(body["definition"])})
            return
        if (method == "PUT" and len(parts) == 4) or (
            method == "POST" and len(parts) == 5 and parts[-1] == "status"
        ):
            self.writes.append({"method": method, "suffix": suffix, "body": deepcopy(body)})
            action = self.actions.pop(0) if self.actions else "success"
            target = next(record for record in self.records if record["schedule_id"] == parts[3])
            base = self.exact.get(parts[3], target)
            if action == "conflict":
                base = {**base, "row_version": base["row_version"] + 1, "name": "Concurrent name"}
                self.exact[parts[3]] = base
                await self.refuse(route, 409)
                return
            if action.isdigit():
                await self.refuse(route, int(action))
                return
            assert body["expected_row_version"] == base["row_version"], (body, base)
            saved = {**deepcopy(base), "row_version": base["row_version"] + 1}
            if method == "PUT":
                saved.update({key: body[key] for key in ("name", "input", "sources")})
                saved.update(body["definition"])
            else:
                saved["status"] = body["status"]
            if action != "drop":
                self.exact[parts[3]] = saved
                target.update(saved)
            if action.startswith("hold"):
                self.write_gate.received.set()
                await asyncio.wait_for(self.write_gate.release.wait(), 40)
            try:
                if action in {"drop", "drop-save"}:
                    await route.abort("connectionreset")
                elif action == "hold-401":
                    await self.refuse(route, 401)
                else:
                    await route.fulfill(json=saved)
            except Error:
                # Abort/owner 破棄は架空 commit の取消しを示さない。
                pass
            finally:
                self.write_gate.returned.set()
            return
        self.unexpected.append(f"Unknown schedule request: {method} {suffix}")
        await route.abort()

    async def refuse(self, route: Route, status: int) -> None:
        """画面にそのまま表示してはいけない Problem detail も検査する。"""
        await route.fulfill(
            status=status,
            json={
                "status": status,
                "code": "fixture_refused",
                "detail": "PRIVATE schedule server detail",
            },
        )
