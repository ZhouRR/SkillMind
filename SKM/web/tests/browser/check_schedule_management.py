"""実 App の全 Project 調度一覧/編集を全面 mock HTTP で検証する。"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from check_accounts import (
    ACTOR,
    CSRF,
    NEXT_PROJECT,
    OTHER,
    PASSWORD,
    PROJECT,
    ResponseGate,
    keyboard_scroll,
)
from check_projects import ProjectsApi, layout, messages, privacy, self_revoke, settle
from check_run_submission import VERSION, task_catalog
from check_schedule_times import occurrences
from playwright.async_api import Browser, Error, Page, Route, async_playwright, expect

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


async def selected(page: Page, _: ScheduleManagementApi, __: dict) -> None:
    """実一覧を選び、精確 GET が完了した後の owner を待つ。"""
    await page.locator(f'[data-schedule-select="{SCHEDULE}"]').click()
    await expect(page.locator(f"{DETAIL_FACTS} h3").first).to_be_visible()
    await expect(page.locator("[data-schedule-detail-refresh]")).to_be_enabled()


async def editor(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """原詳細を選択し、名称草稿と時間 preview を別に確認する。"""
    await selected(page, api, labels)
    await page.locator("[data-schedule-edit]").click()
    await expect(page.locator("[data-schedule-editor]")).to_be_visible()
    await page.locator('[data-schedule-editor] input[name="name"]').fill(
        "Edited objective schedule"
    )


async def preview(page: Page) -> None:
    """現在の定義に対する表示と人工チェックの両方を必須にする。"""
    await page.locator("[data-schedule-preview]").click()
    await expect(page.locator("[data-schedule-confirm]")).to_be_visible()
    await expect(page.locator('[data-schedule-editor] button[type="submit"]')).to_be_disabled()
    await page.locator("[data-schedule-confirm]").check()


async def submit(page: Page, count: int = 2) -> None:
    """同 tick の実 submit event で DOM disabled だけに依存しないことを確かめる。"""
    await page.locator("[data-schedule-editor]").evaluate(
        "(form, count) => { for (let n = 0; n < count; n++) "
        "form.dispatchEvent(new Event('submit', {bubbles:true,cancelable:true})); }",
        count,
    )


async def normal(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """三語/二幅で精確編集→状態変更を辿り、各 HTTP は一回だけにする。"""
    await keyboard_scroll(page, page.locator(".scheduleRows"))
    await editor(page, api, labels)
    await preview(page)
    await submit(page)
    await expect(page.locator("[data-schedule-editor]")).to_be_hidden()
    await expect(page.locator(f"{DETAIL_FACTS} h3").first).to_have_text("Edited objective schedule")
    assert len(api.writes) == 1
    assert api.writes[0]["body"]["expected_row_version"] == 7
    assert api.writes[0]["body"]["input"] == {"objective": "Original objective"}
    await expect(page.locator('[data-schedule-status="PAUSED"]')).to_be_enabled()
    await page.locator('[data-schedule-status="PAUSED"]').evaluate("e => {e.click(); e.click()}")
    await expect(page.locator('[data-schedule-status="ACTIVE"]')).to_be_enabled()
    assert len(api.writes) == 2
    assert api.writes[1]["body"] == {"status": "PAUSED", "expected_row_version": 8}
    trigger = page.locator(DETAIL_FACTS).get_by_role(
        "button",
        name=labels["elements"]["technicalDetails"],
        exact=True,
    )
    await trigger.click()
    dialog = page.get_by_role("dialog", name=labels["elements"]["technicalDetails"], exact=True)
    await expect(dialog).to_be_visible()
    await expect(dialog).to_contain_text(SCHEDULE)
    await expect(page.locator(".modalDrawer:not([hidden])")).to_have_count(1)
    await page.keyboard.press("Escape")
    await expect(dialog).to_be_hidden()
    await expect(trigger).to_be_focused()
    assert len(api.writes) == 2


async def pagination(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """100 件外の帰档履歴、literal 検索、状態、件数減少を server 頁で確認する。"""
    await expect(page.locator("[data-schedule-row]")).to_have_count(25)
    for _ in range(4):
        previous = len(api.calls)
        await page.locator("[data-schedule-next]").click()
        await expect(page.locator("[data-schedule-total]")).to_be_visible()
        assert len(api.calls) > previous
    await expect(page.locator("[data-schedule-row]")).to_have_count(1)
    await expect(page.locator("[data-schedule-next]")).to_be_disabled()
    assert any(call[2].get("offset") == ["100"] for call in api.calls if call[1] == LIST)
    api.records.pop()
    await page.locator("[data-schedule-refresh]").click()
    await expect(page.locator("[data-schedule-row]")).to_have_count(25)
    assert any(call[2].get("offset") == ["75"] for call in api.calls if call[1] == LIST)
    api.records.append(schedule(100))
    await page.locator("[data-schedule-search]").fill("Literal_%")
    await page.locator("[data-schedule-filter]").select_option("ARCHIVED")
    await page.get_by_role("button", name=labels["scheduleManager"]["search"], exact=True).click()
    await expect(page.locator("[data-schedule-row]")).to_have_count(1)
    await page.locator("[data-schedule-select]").click()
    await expect(page.locator(f"{DETAIL_FACTS} h3").first).to_have_text(
        "Literal_% archived history"
    )
    await expect(page.locator("[data-schedule-edit]")).to_be_disabled()
    assert any(
        call[2].get("q") == ["Literal_%"]
        and call[2].get("status") == ["ARCHIVED"]
        and call[2].get("offset") == ["0"]
        for call in api.calls
        if call[1] == LIST
    )
    assert "Literal" not in page.url
    await page.locator("[data-schedule-search]").fill("ANALYZE")
    await page.locator("[data-schedule-filter]").select_option("")
    await page.get_by_role("button", name=labels["scheduleManager"]["search"], exact=True).click()
    await expect(page.locator("[data-schedule-row]")).to_have_count(25)
    assert any(
        call[2].get("q") == ["ANALYZE"] and "status" not in call[2]
        for call in api.calls
        if call[1] == LIST
    )


async def read_only(page: Page, api: ScheduleManagementApi, labels: dict, reason: str) -> None:
    """一覧/原版は読めるが、catalog の失敗と精確 task の不適格を区別する。"""
    await selected(page, api, labels)
    await expect(page.locator("[data-schedule-edit]")).to_be_disabled()
    await expect(page.locator('[data-schedule-status="PAUSED"]')).to_be_disabled()
    await expect(page.get_by_text(labels["scheduleManager"][reason], exact=True)).to_be_visible()
    if reason != "taskUnavailable":
        await expect(page.locator("[data-schedule-task-unavailable]")).to_have_count(0)
    assert not api.writes


async def exact_version(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """一覧の旧版ではなく、選択後の精確 GET 版を編集する。"""
    api.exact[SCHEDULE] = {**schedule(), "row_version": 11, "name": "Exact current"}
    await editor(page, api, labels)
    await preview(page)
    await submit(page)
    await expect(page.locator("[data-schedule-editor]")).to_be_hidden()
    assert api.writes[0]["body"]["expected_row_version"] == 11


async def conflict(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """衝突の三方比較と人工採用を、再 preview/再送から分離する。"""
    api.actions = ["conflict", "success"]
    await editor(page, api, labels)
    await preview(page)
    await submit(page)
    await expect(page.locator('[data-schedule-review="conflict"]')).to_be_visible()
    await page.locator("[data-schedule-reconcile]").click()
    await expect(page.locator("[data-schedule-current]")).to_contain_text("Concurrent name")
    await expect(page.locator("[data-schedule-original]")).to_contain_text("Schedule 000")
    await expect(page.locator("[data-schedule-submitted]")).to_contain_text(
        "Edited objective schedule"
    )
    assert len(api.writes) == 1
    await page.locator("[data-schedule-adopt]").click()
    await expect(page.locator('[data-schedule-editor] input[name="name"]')).to_have_value(
        "Edited objective schedule"
    )
    await expect(page.locator('[data-schedule-editor] button[type="submit"]')).to_be_disabled()
    assert len(api.writes) == 1
    await preview(page)
    await submit(page)
    await expect(page.locator("[data-schedule-editor]")).to_be_hidden()
    assert [write["body"]["expected_row_version"] for write in api.writes] == [7, 8]


async def unknown(
    page: Page, api: ScheduleManagementApi, labels: dict, status: bool = False
) -> None:
    """未知を閉じる/一覧 refresh しても解除せず、原詳細を人工核対する。"""
    api.actions = ["drop-save"]
    if status:
        await selected(page, api, labels)
        await page.locator('[data-schedule-status="PAUSED"]').click()
    else:
        await editor(page, api, labels)
        await preview(page)
        await submit(page)
    await expect(page.locator('[data-schedule-review="unknown"]')).to_be_visible()
    if not status:
        await page.get_by_role("button", name=labels["elements"]["close"], exact=True).click()
        await expect(page.locator("[data-schedule-reopen]")).to_be_visible()
    await expect(page.locator("[data-schedule-edit]")).to_be_disabled()
    await page.locator("[data-schedule-refresh]").click()
    await expect(page.locator("[data-schedule-total]")).to_be_visible()
    if not status:
        await page.locator("[data-schedule-reopen]").click()
    await expect(page.locator('[data-schedule-review="unknown"]')).to_be_visible()
    assert len(api.writes) == 1
    await page.locator("[data-schedule-reconcile]").click()
    await expect(page.locator("[data-schedule-current]")).to_be_visible()
    await page.locator("[data-schedule-adopt]").click()
    await expect(page.locator("[data-schedule-previous-unknown]")).to_be_visible()
    assert len(api.writes) == 1


async def writer_order(
    page: Page, api: ScheduleManagementApi, labels: dict, edit_first: bool
) -> None:
    """同 tick の反対 writer は ref 門禁で拒否し、各 owner を並行起動しない。"""
    await selected(page, api, labels)
    api.actions = ["hold"]
    await page.evaluate(
        """editFirst => {
        const edit = document.querySelector('[data-schedule-edit]');
        const pause = document.querySelector('[data-schedule-status="PAUSED"]');
        const actions = editFirst ? [edit,pause] : [pause,edit];
        actions.forEach(button => button.click());
    }""",
        edit_first,
    )
    if edit_first:
        await expect(page.locator("[data-schedule-editor]")).to_be_visible()
        await settle(page)
        assert not api.writes
    else:
        await asyncio.wait_for(api.write_gate.received.wait(), 10)
        await expect(page.locator("[data-schedule-editor]")).to_have_count(0)
        assert len(api.writes) == 1
        api.write_gate.release.set()
        await expect(page.locator('[data-schedule-status="ACTIVE"]')).to_be_enabled()


async def denied(
    page: Page, api: ScheduleManagementApi, labels: dict, code: int, pending: bool = False
) -> None:
    """独立 list 拒否は既存 editor と late detail の両方から資格を撤回する。"""
    await editor(page, api, labels)
    if pending:
        api.actions = ["hold-401"]
        await preview(page)
        await submit(page)
        await asyncio.wait_for(api.write_gate.received.wait(), 10)
    api.read_failures[LIST] = code
    await page.locator("[data-schedule-refresh]").evaluate("e => e.click()")
    if code == 401:
        await expect(page.locator('input[name="email"]')).to_be_visible()
        return
    await expect(page.locator("[data-schedule-read-denied]")).to_be_visible()
    await expect(page.locator('[data-schedule-editor] input[name="name"]')).to_be_disabled()
    if pending:
        await expect(page.locator('[data-schedule-review="unknown"]')).to_be_visible()
        api.write_gate.release.set()
        await asyncio.wait_for(api.write_gate.returned.wait(), 10)
        await settle(page)
        await expect(page.locator('input[name="email"]')).to_have_count(0)
    await page.get_by_role("button", name=labels["elements"]["close"], exact=True).click()
    api.read_failures.pop(LIST)
    await page.locator("[data-schedule-refresh]").click()
    await expect(page.locator("[data-schedule-total]")).to_be_visible()
    await expect(page.locator("[data-schedule-read-denied]")).to_be_visible()
    await page.locator("[data-schedule-detail-refresh]").click()
    await expect(page.locator("[data-schedule-read-denied]")).to_have_count(0)
    if pending:
        await page.locator("[data-schedule-reopen]").click()
        await expect(page.locator('[data-schedule-review="unknown"]')).to_be_visible()
        assert len(api.writes) == 1
    else:
        await expect(page.locator("[data-schedule-edit]")).to_be_enabled()


async def stale_detail(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """拒否前に始まった detail 成功を、新たな人工再認可と見なさない。"""
    gate = ResponseGate()
    api.read_gates[(DETAIL, "")] = gate
    await page.locator(f'[data-schedule-select="{SCHEDULE}"]').click()
    await asyncio.wait_for(gate.received.wait(), 10)
    api.read_failures[LIST] = 403
    await page.locator("[data-schedule-refresh]").click()
    await expect(page.locator("[data-schedule-read-denied]")).to_be_visible()
    gate.release.set()
    await expect(page.locator(f"{DETAIL_FACTS} h3").first).to_be_visible()
    await expect(page.locator("[data-schedule-edit]")).to_be_disabled()
    await expect(page.locator("[data-schedule-read-denied]")).to_be_visible()
    assert not api.writes


async def q_aba(page: Page, api: ScheduleManagementApi, labels: dict, code: int) -> None:
    """A→B→A の古い list 成功/401 は現在頁にも会話にも入れない。"""
    gate = ResponseGate()
    if code != 200:
        gate.failure = (code, "fixture_refused")
    api.read_gates[(LIST, "Schedule")] = gate
    search = page.get_by_role("button", name=labels["scheduleManager"]["search"], exact=True)
    await page.locator("[data-schedule-search]").fill("Schedule")
    await search.click()
    await asyncio.wait_for(gate.received.wait(), 10)
    await page.locator("[data-schedule-search]").fill("Literal")
    await search.click()
    await expect(page.locator("[data-schedule-row]")).to_have_count(1)
    api.read_gates.pop((LIST, "Schedule"))
    await page.locator("[data-schedule-search]").fill("Schedule")
    await search.click()
    await expect(page.locator("[data-schedule-row]")).to_have_count(25)
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 10)
    await settle(page)
    await expect(page.locator("[data-schedule-row]")).to_have_count(25)
    await expect(page.locator("[data-schedule-read-denied]")).to_have_count(0)


async def owner_aba(
    page: Page, api: ScheduleManagementApi, labels: dict, actor: bool, code: int
) -> None:
    """元 owner の未決書込を別 Project/actor の現 draft に持ち込まない。"""
    api.actions = ["hold-401" if code == 401 else "hold"]
    await editor(page, api, labels)
    await preview(page)
    await submit(page)
    await asyncio.wait_for(api.write_gate.received.wait(), 10)
    await page.get_by_role("button", name=labels["elements"]["close"], exact=True).click()
    if actor:
        await page.evaluate("location.hash='/accounts'")
        await expect(page.locator("[data-account-own]")).to_be_visible()
        await self_revoke(page, api, labels["account"])
        api.actor = OTHER
        await page.locator('input[name="email"]').fill(api.users[OTHER]["email"])
        await page.locator('input[name="password"]').fill(PASSWORD)
        await page.locator('button[type="submit"]').click()
        await expect(page.locator("[data-account-own]")).to_be_visible()
    else:
        await page.evaluate("project => location.hash='/schedules?project='+project", NEXT_PROJECT)
        await expect(page.locator("[data-schedule-row]")).to_have_count(25)
    await page.evaluate("project => location.hash='/schedules?project='+project", PROJECT)
    await expect(page.locator("[data-schedule-row]")).to_have_count(25)
    api.write_gate.release.set()
    await asyncio.wait_for(api.write_gate.returned.wait(), 10)
    await settle(page)
    await expect(page.locator("[data-schedule-review]")).to_have_count(0)
    await expect(page.locator("[data-schedule-reopen]")).to_have_count(0)
    await expect(page.locator('input[name="email"]')).to_have_count(0)
    assert len(api.writes) == 1


async def tasks_entry(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """Task card 集約も 100 件で切らず、catalog エラー時も管理へ進める。"""
    await page.evaluate("project => location.hash='/tasks?project='+project", PROJECT)
    await expect(page.locator("[data-schedules-manager-link]")).to_be_visible()
    await expect(page.locator(".taskCard")).to_have_count(1)
    assert any(
        call[2].get("limit") == ["100"] and call[2].get("offset") == ["100"]
        for call in api.calls
        if call[1] == LIST
    )
    api.read_failures[f"projects/{PROJECT}/tasks"] = 500
    await page.evaluate("project => location.hash='/schedules?project='+project", PROJECT)
    await expect(page.locator("[data-schedule-row]")).to_have_count(25)
    await page.evaluate("project => location.hash='/tasks?project='+project", PROJECT)
    await expect(page.locator("[data-schedules-manager-link]")).to_be_visible()
    await page.locator("[data-schedules-manager-link]").click()
    await expect(page.locator("[data-schedule-row]")).to_have_count(25)
    assert not api.writes


async def close_same_tick(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """submit→閉じる→古い edit click でも原 owner を再生成しない。"""
    api.actions = ["hold"]
    await editor(page, api, labels)
    await preview(page)
    await page.evaluate(
        """closeText => {
        const form = document.querySelector('[data-schedule-editor]');
        const close = [...form.closest('[role="dialog"]').querySelectorAll('button')]
            .find(button => button.textContent === closeText);
        const edit = document.querySelector('[data-schedule-edit]');
        form.dispatchEvent(new Event('submit', {bubbles:true,cancelable:true}));
        close.click(); edit.click();
    }""",
        labels["elements"]["close"],
    )
    await asyncio.wait_for(api.write_gate.received.wait(), 10)
    await expect(page.locator("[data-schedule-editor]")).to_be_hidden()
    await expect(page.locator("[data-schedule-reopen]")).to_be_visible()
    await page.locator("[data-schedule-reopen]").click()
    await expect(page.locator('[data-schedule-editor] input[name="name"]')).to_have_value(
        "Edited objective schedule"
    )
    await expect(page.locator('[data-schedule-editor] button[type="submit"]')).to_be_disabled()
    assert len(api.writes) == 1
    api.write_gate.release.set()
    await expect(page.locator("[data-schedule-editor]")).to_be_hidden()
    await expect(page.locator(f"{DETAIL_FACTS} h3").first).to_have_text("Edited objective schedule")


async def terminal_adopt(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """COMPLETED の採用は編集を閉じるが、状態 owner は明示 archive を選べる。"""
    api.actions = ["conflict"]
    await editor(page, api, labels)
    await preview(page)
    await submit(page)
    await expect(page.locator('[data-schedule-review="conflict"]')).to_be_visible()
    api.exact[SCHEDULE]["status"] = "COMPLETED"
    await page.locator("[data-schedule-reconcile]").click()
    await expect(page.locator("[data-schedule-current]")).to_be_visible()
    await page.locator("[data-schedule-adopt]").click()
    await expect(page.locator('[data-schedule-editor] input[name="name"]')).to_be_disabled()
    await page.get_by_role("button", name=labels["elements"]["close"], exact=True).click()
    await page.locator("[data-schedule-detail-refresh]").click()
    await expect(page.locator('[data-schedule-status="ARCHIVED"]')).to_be_enabled()
    await page.locator('[data-schedule-status="ARCHIVED"]').click()
    await expect(page.locator('[data-schedule-status="ARCHIVED"]')).to_have_count(0)
    assert api.writes[-1]["body"] == {"status": "ARCHIVED", "expected_row_version": 8}
    await expect(
        page.get_by_text(labels["scheduleEditor"]["failures"]["conflict"], exact=True)
    ).to_have_count(0)


async def large_comparison(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """狭幅/24px でも三方比較の原版・草稿・現在値を読めることを測る。"""
    await page.add_style_tag(content="html { font-size: 24px !important; }")
    api.actions = ["conflict"]
    await editor(page, api, labels)
    await preview(page)
    await submit(page)
    await expect(page.locator('[data-schedule-review="conflict"]')).to_be_visible()
    await page.locator("[data-schedule-reconcile]").click()
    await expect(page.locator("[data-schedule-current]")).to_be_visible()
    await page.locator("[data-schedule-comparison]").scroll_into_view_if_needed()
    for marker in ("original", "submitted", "current"):
        assert await page.locator(f"[data-schedule-{marker}]").evaluate(
            "e => e.scrollWidth <= e.clientWidth + 1"
        ), marker


async def unicode_search(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """200 code point の合法検索を UTF-16 の native maxLength で半分にしない。"""
    await page.locator("[data-schedule-search]").fill("😀" * 200)
    await page.get_by_role("button", name=labels["scheduleManager"]["search"], exact=True).click()
    await expect(page.locator("[data-schedule-row]")).to_have_count(0)
    assert any(call[2].get("q") == ["😀" * 200] for call in api.calls if call[1] == LIST)
    before = sum(call[1] == LIST for call in api.calls)
    await page.locator("[data-schedule-search]").fill("😀" * 201)
    await expect(
        page.get_by_role("button", name=labels["scheduleManager"]["search"], exact=True)
    ).to_be_disabled()
    assert sum(call[1] == LIST for call in api.calls) == before


async def read_deadline(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """実 30 秒の詳細読取期限後、abort 無視の成功でも編集資格を復元しない。"""
    gate = ResponseGate()
    api.read_gates[(DETAIL, "")] = gate
    await page.locator(f'[data-schedule-select="{SCHEDULE}"]').click()
    await asyncio.wait_for(gate.received.wait(), 10)
    await expect(
        page.locator("[data-schedule-detail]").get_by_text(
            labels["scheduleManager"]["failures"]["loadFailed"], exact=True
        )
    ).to_be_visible(timeout=35000)
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 10)
    await settle(page)
    await expect(page.locator(DETAIL_FACTS)).to_have_count(0)
    assert not api.writes


async def simultaneous_denial(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """原 status 応答と list 拒否を同時解放しても自動 detail が復権しない。"""
    api.actions = ["hold"]
    await selected(page, api, labels)
    await page.locator('[data-schedule-status="PAUSED"]').click()
    await asyncio.wait_for(api.write_gate.received.wait(), 10)
    gate = ResponseGate()
    gate.failure = (403, "fixture_refused")
    api.read_gates[(LIST, "")] = gate
    await page.locator("[data-schedule-refresh]").click()
    await asyncio.wait_for(gate.received.wait(), 10)
    gate.release.set()
    api.write_gate.release.set()
    await asyncio.gather(gate.returned.wait(), api.write_gate.returned.wait())
    await settle(page)
    await expect(page.locator("[data-schedule-read-denied]")).to_be_visible()
    await expect(page.locator("[data-schedule-edit]")).to_be_disabled()
    assert len(api.writes) == 1


async def activity_facts(
    page: Page,
    api: ScheduleManagementApi,
    labels: dict,
    *,
    legacy: bool = False,
    lease: str | None = None,
    attempts: int = 1,
    state: str = "ACTIVE",
) -> None:
    """旧履歴と未結算の有無を分け、到期/耗尽を停止や新発火許可に変換しない。"""
    api.activities[SCHEDULE] = activity(legacy=legacy, lease=lease, attempts=attempts)
    api.exact[SCHEDULE] = {**schedule(), "status": state}
    await selected(page, api, labels)
    panel = page.locator("[data-schedule-activity]")
    await expect(panel).to_be_visible()
    tracking = "LEGACY_UNAVAILABLE" if legacy else "TRACKED"
    await expect(panel.locator(f'[data-schedule-activity-tracking="{tracking}"]')).to_be_visible()
    await expect(panel.locator("[data-schedule-activity-checked-at]")).to_be_visible()
    if lease:
        await expect(panel.locator("[data-schedule-pending]")).to_be_visible()
        await expect(panel.locator(f'[data-schedule-lease="{lease}"]')).to_be_visible()
        limit = "reached" if attempts == 3 else "remaining"
        await expect(panel.locator(f'[data-schedule-attempt-limit="{limit}"]')).to_be_visible()
        await expect(panel).to_contain_text("00000000-0000-4000-8000-000000002001")
    else:
        await expect(panel.locator("[data-schedule-pending]")).to_have_count(0)
    await expect(panel.locator("button")).to_have_count(1)
    assert not api.writes
    await panel.scroll_into_view_if_needed()


async def activity_unknown(page: Page, api: ScheduleManagementApi, labels: dict) -> None:
    """独立活動 GET の成功と「現在 PENDING 無し」は原変更の成功証明ではない。"""
    api.actions = ["drop-save"]
    await editor(page, api, labels)
    await preview(page)
    await submit(page)
    await expect(page.locator('[data-schedule-review="unknown"]')).to_be_visible()
    await page.get_by_role("button", name=labels["elements"]["close"], exact=True).click()
    before = len([call for call in api.calls if call[1] == ACTIVITY])
    await page.locator("[data-schedule-activity-refresh]").click()
    await expect(page.locator('[data-schedule-activity-tracking="TRACKED"]')).to_be_visible()
    assert len([call for call in api.calls if call[1] == ACTIVITY]) == before + 1
    await expect(page.locator("[data-schedule-pending]")).to_have_count(0)
    await expect(page.locator("[data-schedule-edit]")).to_be_disabled()
    await page.locator("[data-schedule-reopen]").click()
    await expect(page.locator('[data-schedule-review="unknown"]')).to_be_visible()
    assert len(api.writes) == 1


async def activity_denied(page: Page, api: ScheduleManagementApi, labels: dict, code: int) -> None:
    """活動の拒否も全 writer を閉じ、活動の成功だけでは旧資格を復活させない。"""
    await selected(page, api, labels)
    await expect(page.locator('[data-schedule-activity-tracking="TRACKED"]')).to_be_visible()
    api.read_failures[ACTIVITY] = code
    await page.locator("[data-schedule-activity-refresh]").click()
    if code == 401:
        await expect(page.locator('input[name="email"]')).to_be_visible()
        return
    await expect(page.locator("[data-schedule-activity-error]")).to_be_visible()
    await expect(page.locator(DETAIL_FACTS)).to_be_visible()
    if code == 500:
        await expect(page.locator("[data-schedule-read-denied]")).to_have_count(0)
        await expect(page.locator("[data-schedule-edit]")).to_be_enabled()
    else:
        await expect(page.locator("[data-schedule-read-denied]")).to_be_visible()
        await expect(page.locator("[data-schedule-edit]")).to_be_disabled()
    api.read_failures.pop(ACTIVITY)
    await page.locator("[data-schedule-activity-refresh]").click()
    await expect(page.locator('[data-schedule-activity-tracking="TRACKED"]')).to_be_visible()
    if code != 500:
        await expect(page.locator("[data-schedule-read-denied]")).to_be_visible()
        await expect(page.locator("[data-schedule-edit]")).to_be_disabled()
    assert not api.writes


async def activity_aba(
    page: Page, api: ScheduleManagementApi, labels: dict, code: int, project: bool = False
) -> None:
    """元活動応答を Schedule/Project A→B→A の新しい読取へ移さない。"""
    gate = ResponseGate()
    if code != 200:
        gate.failure = (code, "fixture_refused")
    api.read_gates[(ACTIVITY, "")] = gate
    api.activities[SCHEDULE] = activity(lease="active")
    await selected(page, api, labels)
    await asyncio.wait_for(gate.received.wait(), 10)
    if project:
        await page.evaluate("id => location.hash='/schedules?project='+id", NEXT_PROJECT)
        await expect(page.locator("[data-schedule-row]")).to_have_count(25)
        await selected(page, api, labels)
    else:
        await page.locator(f'[data-schedule-select="{schedule(1)["schedule_id"]}"]').click()
    await expect(page.locator('[data-schedule-activity-tracking="TRACKED"]')).to_be_visible()
    api.read_gates.pop((ACTIVITY, ""))
    api.activities[SCHEDULE] = activity()
    if project:
        await page.evaluate("id => location.hash='/schedules?project='+id", PROJECT)
        await expect(page.locator("[data-schedule-row]")).to_have_count(25)
    await page.locator(f'[data-schedule-select="{SCHEDULE}"]').click()
    await expect(page.locator('[data-schedule-activity-tracking="TRACKED"]')).to_be_visible()
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 10)
    await settle(page)
    await expect(page.locator("[data-schedule-pending]")).to_have_count(0)
    await expect(page.locator("[data-schedule-read-denied]")).to_have_count(0)
    await expect(page.locator('input[name="email"]')).to_have_count(0)
    assert not api.writes


async def exercise(
    browser: Browser,
    url: str,
    name: str,
    action: Callable[[Page, ScheduleManagementApi, dict], Awaitable[None]],
    output: Path,
    language: str = "en",
    width: int = 1440,
    setup: Callable[[ScheduleManagementApi], None] | None = None,
) -> None:
    """一 case 一 context。HTTP/JS/秘密/横幅を検査し自有 gate を必ず閉じる。"""
    api = ScheduleManagementApi(url, language)
    if setup:
        setup(api)
    context = await browser.new_context(viewport={"width": width, "height": 1000}, locale=language)
    await context.route("**/*", api.route)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            errors.append(message.text)
            if message.type == "error" and not message.text.startswith("Failed to load resource:")
            else None
        ),
    )
    await page.add_init_script("""(() => {
        const fetchOriginal = window.fetch.bind(window);
        window.fetch = (input, init) => fetchOriginal(input,
            init ? {...init,signal:undefined} : init);
    })();""")
    try:
        await page.goto(f"{url}#/schedules?project={PROJECT}")
        await expect(page.locator("[data-schedule-manager]")).to_be_visible()
        await expect(page.locator("[data-schedule-row]")).to_have_count(25)
        labels = await messages(page, language)
        await action(page, api, labels)
        await settle(page)
        await privacy(page)
        await layout(page)
        assert "PRIVATE schedule server detail" not in await page.locator("body").inner_text()
        assert not api.unexpected, api.unexpected
        assert not api.failures, api.failures
        assert not errors, errors
        await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
        await page.screenshot(path=str(output / f"{name}-viewport.png"))
        print(f"PASS {name}", flush=True)
    except Exception:
        await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        print(f"FAIL {name} mock={api.failures} unknown={api.unexpected} js={errors}", flush=True)
        raise
    finally:
        api.release.set()
        api.write_gate.release.set()
        for gate in [*api.gates.values(), *api.read_gates.values()]:
            gate.release.set()
        await context.close()


async def check(url: str, output: Path, only: str | None) -> None:
    """本番を対象にできない loopback の実 App fixture に制限する。"""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or not parsed.path.endswith("/tests/browser/projects.html")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Only the loopback projects.html App fixture is supported")
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        executed = 0

        async def run(name: str, action: Callable, **kwargs) -> None:
            """絞込が空なら成功を報告しない。"""
            nonlocal executed
            if only is None or only in name:
                await exercise(browser, url, name, action, output, **kwargs)
                executed += 1

        try:
            for language in ("zh", "ja", "en"):
                for width in (390, 1440):
                    await run(f"manager-{language}-{width}", normal, language=language, width=width)
            await run("pagination-literal-archived-shrink", pagination)
            await run("exact-get-version", exact_version)
            await run("conflict-adopt-preview", conflict, width=390)
            await run("unknown-edit-reopen", unknown, width=390)
            await run("unknown-status-refresh", lambda p, a, m: unknown(p, a, m, True))
            await run("same-tick-edit-status", lambda p, a, m: writer_order(p, a, m, True))
            await run("same-tick-status-edit", lambda p, a, m: writer_order(p, a, m, False))
            await run("same-tick-save-close-edit", close_same_tick)
            await run("terminal-adopt-then-archive", terminal_adopt)
            await run("comparison-zh-390-24", large_comparison, width=390, language="zh")
            await run("unicode-search-boundary", unicode_search)
            await run("simultaneous-list-denial-status-success", simultaneous_denial)
            await run("late-detail-after-denial", stale_detail)
            for code in (401, 403, 404):
                await run(f"list-denied-{code}", lambda p, a, m, c=code: denied(p, a, m, c))
            await run("list-denied-inflight-late401", lambda p, a, m: denied(p, a, m, 403, True))
            for code in (200, 401):
                await run(f"q-aba-{code}", lambda p, a, m, c=code: q_aba(p, a, m, c))
                for actor in (False, True):
                    await run(
                        f"owner-{'actor' if actor else 'project'}-aba-{code}",
                        lambda p, a, m, c=code, actor=actor: owner_aba(p, a, m, actor, c),
                    )

            def unavailable(api: ScheduleManagementApi, kind: str) -> None:
                """存在しない版・読取障害・未確認 readiness は別 fixture にする。"""
                if kind == "catalogUnavailable":
                    api.read_failures[f"projects/{PROJECT}/tasks"] = 500
                elif kind == "taskUnavailable":
                    api.catalog["tasks"][0]["skill_version_id"] = (
                        "00000000-0000-4000-8000-000000000062"
                    )
                elif kind == "readOnlyProject":
                    api.details[PROJECT]["status"] = "ARCHIVED"
                elif kind == "guidanceOnly":
                    api.catalog["tasks"][0]["readiness"]["level"] = "GUIDANCE_ONLY"
                else:
                    api.catalog["tasks"][0]["readiness"] = None

            for reason in (
                "catalogUnavailable",
                "taskUnavailable",
                "readOnlyProject",
                "guidanceOnly",
                "readinessUnconfirmed",
            ):
                await run(
                    f"readonly-{reason}",
                    lambda p, a, m, r=reason: read_only(p, a, m, r),
                    setup=lambda a, r=reason: unavailable(a, r),
                    width=390,
                )
            await run("tasks-full-list-management-entry", tasks_entry)
            for language in ("zh", "ja", "en"):
                for width in (390, 1440):
                    await run(
                        f"activity-{language}-{width}",
                        lambda p, a, m: activity_facts(p, a, m, lease="expired", attempts=3),
                        language=language,
                        width=width,
                    )
            await run(
                "activity-legacy-unavailable", lambda p, a, m: activity_facts(p, a, m, legacy=True)
            )
            await run("activity-tracked-no-pending", activity_facts)
            await run(
                "activity-lease-active", lambda p, a, m: activity_facts(p, a, m, lease="active")
            )
            await run(
                "activity-lease-expired", lambda p, a, m: activity_facts(p, a, m, lease="expired")
            )
            for state in ("PAUSED", "ARCHIVED"):
                await run(
                    f"activity-{state}-still-pending",
                    lambda p, a, m, s=state: activity_facts(p, a, m, lease="expired", state=s),
                )
            await run("activity-no-write-proof", activity_unknown)
            for code in (401, 403, 404, 500):
                await run(
                    f"activity-read-{code}", lambda p, a, m, c=code: activity_denied(p, a, m, c)
                )
            for code in (200, 401):
                for project in (False, True):
                    await run(
                        f"activity-{'project' if project else 'schedule'}-aba-{code}",
                        lambda p, a, m, c=code, project=project: activity_aba(p, a, m, c, project),
                    )
            await run("read-real-30s-deadline", read_deadline)
            assert executed > 0, "No case matched --only"
            print(f"Schedule management browser: {executed} cases passed", flush=True)
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default="http://127.0.0.1:5205/skillmind/tests/browser/projects.html"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--only")
    args = parser.parse_args()
    asyncio.run(check(args.url, args.output_dir, args.only))
