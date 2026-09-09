"""実 App の Project 解決・失効リンク・狭幅導航を全面 mock HTTP で検証する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from check_accounts import (
    NEXT_PROJECT,
    OTHER,
    PASSWORD,
    PROJECT,
    AccountsApi,
    ResponseGate,
    privacy,
    project,
    self_revoke,
    settle,
)
from check_run_submission import TASK, VERSION, task_catalog
from playwright.async_api import Browser, Page, Route, async_playwright, expect

ARCHIVED = "00000000-0000-4000-8000-000000000022"
MISSING = "00000000-0000-4000-8000-000000000099"
RUN = "00000000-0000-4000-8000-000000000300"
CATALOG_TASK = f"{VERSION}::analyze"
SECOND_VERSION = "00000000-0000-4000-8000-000000000062"


class ProjectsApi(AccountsApi):
    """既存認証 fixture を共有し、Project 公開読取だけを局所拡張する。"""

    def __init__(self, url: str, language: str = "en") -> None:
        """各 case に独立した可視一覧・精確詳細・拒否・応答 gate を持たせる。"""
        super().__init__(url.replace("projects.html", "accounts.html"), "ADMIN", language)
        self.projects = [project(PROJECT), project(NEXT_PROJECT)]
        self.preference = PROJECT
        archived = {**project(ARCHIVED), "status": "ARCHIVED"}
        self.details = {item["project_id"]: item for item in [*self.projects, archived]}
        self.reject: dict[str, tuple[int, str]] = {}
        self.fragment = ""
        self.with_tasks = False
        self.with_run = False
        self.with_module = False
        self.screenshot: Path | None = None
        self.history_requested = asyncio.Event()
        self.modules_requested = asyncio.Event()

    async def respond(self, route: Route) -> None:
        """無関係 URL は共通の拒否 handler に渡し、Project 認可境界を記録する。"""
        request = route.request
        url = urlsplit(request.url)
        suffix = url.path.removeprefix(self.prefix)
        if f"{url.scheme}://{url.netloc}" != self.origin:
            await super().respond(route)
            return
        if self.with_run and request.method == "GET" and suffix == f"runs/{RUN}":
            self.calls.append(("GET", suffix, {}, None))
            await route.fulfill(json=self.run())
            return
        if self.with_run and request.method == "GET" and suffix == f"runs/{RUN}/events":
            self.calls.append(("GET", suffix, parse_qs(url.query), None))
            event = {
                "run_id": RUN,
                "run_attempt_id": None,
                "agent_session_id": None,
                "sequence": 1,
                "event_type": "RUN_SNAPSHOT",
                "occurred_at": "2026-09-09T00:00:00Z",
                "payload": {"status": "SUCCEEDED", "row_version": 4},
                "trace_id": None,
            }
            await route.fulfill(
                content_type="text/event-stream",
                body="event: run.snapshot\ndata: " + json.dumps(event) + "\n\n",
            )
            return
        if (
            f"{url.scheme}://{url.netloc}" != self.origin
            or not url.path.startswith(self.prefix)
            or not (suffix == "projects" or suffix.startswith("projects/"))
        ):
            await super().respond(route)
            return
        query = parse_qs(url.query)
        body = request.post_data_json if request.post_data else None
        self.calls.append((request.method, suffix, query, body))
        if suffix.endswith("/runs") and "status" not in query:
            self.history_requested.set()
        if suffix.endswith("/modules"):
            self.modules_requested.set()
        if request.method != "GET":
            self.unexpected.append(f"Unrequested Project mutation: {request.method} {suffix}")
            await route.abort()
            return
        gate = self.gates.get(("GET", suffix))
        failure = self.reject.get(suffix)
        parts = suffix.split("/")
        result: object = None
        if suffix == "projects":
            result = {"items": self.projects.copy()}
            if query.get("include_archived") == ["true"]:
                result = {"items": list(self.details.values())}
        elif len(parts) == 2:
            result = self.details.get(unquote(parts[1]))
            if result is None and failure is None:
                failure = (404, "project_not_found")
        elif len(parts) == 3 and parts[1] in self.details:
            if parts[2] == "modules":
                result = {"modules": []}
                if self.with_module:
                    result["modules"] = [
                        {
                            "module_id": f"00000000-0000-4000-8000-{500 + index:012}",
                            "project_id": parts[1],
                            "name": f"Browser module {index}",
                            "description": "Fixture module",
                            "skills": [
                                {
                                    "skill_version_id": version,
                                    "skill_id": version,
                                    "skill_key": f"module-fixture-{index}",
                                    "skill_name": f"Module skill {index}",
                                    "version": "1.0.0",
                                    "sort_order": 0,
                                }
                            ],
                            "created_at": "2026-09-09T00:00:00Z",
                            "updated_at": "2026-09-09T00:00:00Z",
                        }
                        for index, version in enumerate((VERSION, SECOND_VERSION), 1)
                    ]
            elif parts[2] == "tasks":
                result = task_catalog() if self.with_tasks else {"tasks": []}
                if self.with_tasks:
                    result["tasks"][0]["readiness"]["requirements"] = []
                    if self.with_module:
                        first = {**result["tasks"][0], "title": "First module task"}
                        second = {
                            **first,
                            "skill_version_id": SECOND_VERSION,
                            "skill_id": SECOND_VERSION,
                            "task_id": "00000000-0000-4000-8000-000000000031",
                            "title": "Second module task",
                        }
                        result["tasks"] = [first, second]
            elif parts[2] == "schedules":
                result = {"schedules": []}
            elif parts[2] == "runs":
                result = {
                    "items": [],
                    "has_more": False,
                    "limit": int(query["limit"][0]),
                    "offset": int(query["offset"][0]),
                }
        elif self.with_run and suffix == f"projects/{ARCHIVED}/runs/{RUN}/detail":
            result = json.loads(
                (
                    Path(__file__).resolve().parents[3] / "contracts/examples/run-detail.v1.json"
                ).read_text()
            )
            result.update(self.run())
        elif self.with_run and suffix == f"projects/{ARCHIVED}/runs/{RUN}/evaluations":
            result = {"evaluations": []}
        if result is None and failure is None:
            self.unexpected.append(f"Unknown Project read: {suffix}")
            await route.abort()
            return
        if gate:
            gate.received.set()
            await asyncio.wait_for(gate.release.wait(), 30)
            failure = gate.failure or failure
        try:
            if failure:
                status, code = failure
                await route.fulfill(
                    status=status,
                    json={
                        "type": f"https://projectmind.local/problems/{code}",
                        "title": "Fixture refusal",
                        "status": status,
                        "code": code,
                        "detail": (
                            "Fixture server detail must not expose existence or authorization"
                        ),
                    },
                )
            else:
                await route.fulfill(status=200, json=result)
        finally:
            if gate:
                gate.returned.set()

    def project_reads(self, project_id: str | None = None) -> list[tuple]:
        """詳細認可と区別し、実画面/module が送った Project 依存読取を返す。"""
        return [
            call
            for call in self.calls
            if call[1].startswith(f"projects/{project_id}/" if project_id else "projects/")
            and call[1].count("/") >= 2
        ]

    def run(self) -> dict:
        """書込/SSEを要しない完了 Run の公開概要を返す。"""
        return {
            "run_id": RUN,
            "project_id": ARCHIVED,
            "task_id": TASK,
            "status": "SUCCEEDED",
            "row_version": 4,
            "created_at": "2026-09-09T00:00:00Z",
            "idempotent_replay": False,
        }


async def messages(page: Page, language: str) -> dict:
    """実翻訳 catalog を読むので文案を test 側で二重管理しない。"""
    return await page.evaluate(
        "async language => (await import('../../src/lib/i18n/messages.ts')).MESSAGES[language]",
        language,
    )


async def menu(page: Page) -> None:
    """狭幅では実開閉 button を通し、非表示要素の force 操作をしない。"""
    toggle = page.locator(".sidebarMenuToggle")
    if await toggle.is_visible() and await toggle.get_attribute("aria-expanded") == "false":
        await toggle.click()


async def selected(page: Page, project_id: str) -> None:
    """読み込み完了を実 selector の値から確認する。"""
    await expect(page.locator(".sideNavProject select")).to_have_value(project_id)


async def no_other_project(api: ProjectsApi, expected: str | None) -> None:
    """別 Project の読取・保存・mutation を一件も暗黙発行していないと確認する。"""
    for call in api.project_reads():
        assert expected is not None and call[1].split("/")[1] == expected, call
    for method, suffix, _, body in api.calls:
        if method == "PUT" and suffix == "users/me/project-preference":
            assert expected is not None and body["project_id"] == expected, body


async def inaccessible(page: Page, api: ProjectsApi, labels: dict) -> None:
    """明示 target は不正・不存在・無権限のいずれでも保ち、別 Project を読まない。"""
    await expect(page.locator('[data-project-context="unavailable"]')).to_be_visible()
    if labels:
        await expect(
            page.locator('[data-project-context="unavailable"] [role="alert"]')
        ).to_have_text(labels["app"]["projectUnavailable"])
    await settle(page)
    assert urlsplit(page.url).fragment == api.fragment, "Explicit target was silently replaced"
    await expect(page.locator(".historyPage, .workspace")).to_have_count(0)
    await no_other_project(api, None)
    assert "Fixture server detail" not in await page.locator("body").inner_text()


async def default_selection(page: Page, api: ProjectsApi, _: dict) -> None:
    """URL に Project が無い時だけ有効 preference または先頭の活動項目を使う。"""
    expected = api.preference if api.preference in {PROJECT, NEXT_PROJECT} else PROJECT
    await selected(page, expected)
    await expect(page.locator(".historyPage")).to_be_visible()
    assert urlsplit(page.url).fragment == "/history"
    await no_other_project(api, expected)


async def archived_history(page: Page, api: ProjectsApi, _: dict) -> None:
    """活動一覧から漏れる正当な ARCHIVED 詳細を読み、履歴へのアクセスを保持する。"""
    await selected(page, ARCHIVED)
    await expect(page.locator(".historyPage")).to_be_visible()
    assert urlsplit(page.url).fragment == f"/history?project={ARCHIVED}"
    assert any(call[1] == f"projects/{ARCHIVED}" for call in api.calls)
    await no_other_project(api, ARCHIVED)


async def archived_workspace(page: Page, api: ProjectsApi, labels: dict) -> None:
    """活動一覧外の凍結 Run を Workspace から読み、選択変更後に持ち越さない。"""
    await selected(page, ARCHIVED)
    await expect(page.locator(".workspace .runFacts")).to_contain_text(RUN[:8])
    await page.get_by_role("tab", name=labels["workspace"]["tabResult"], exact=True).click()
    await expect(page.get_by_text("Repository review completed", exact=True).first).to_be_visible()
    assert any(call[1] == f"projects/{ARCHIVED}/runs/{RUN}/detail" for call in api.calls)
    await no_other_project(api, ARCHIVED)
    await menu(page)
    await page.locator(".sideNavProject select").select_option(NEXT_PROJECT)
    await selected(page, NEXT_PROJECT)
    query = parse_qs(urlsplit(page.url).fragment.partition("?")[2])
    assert query == {"project": [NEXT_PROJECT]}, query
    await expect(page.locator(".runFacts")).to_have_count(0)


async def workspace_draft(page: Page, api: ProjectsApi, labels: dict) -> None:
    """同じ task ID でも別 Project へ未送信入力を運ばず、URL task も消す。"""
    await selected(page, PROJECT)
    await expect(page.locator(".runForm textarea.jsonInput")).to_be_visible()
    await page.locator(".runForm textarea.jsonInput").fill('{"draft":"old project only"}')
    await page.keyboard.press("Escape")
    await menu(page)
    await page.locator(".sideNavProject select").select_option(NEXT_PROJECT)
    await selected(page, NEXT_PROJECT)
    assert parse_qs(urlsplit(page.url).fragment.partition("?")[2]) == {"project": [NEXT_PROJECT]}
    await expect(page.locator(".runForm")).to_be_hidden()
    await (
        page.locator(".runLauncher")
        .get_by_role("button", name=labels["workspace"]["openNewRun"])
        .click()
    )
    await expect(page.locator(".runForm textarea.jsonInput")).to_have_value("{}")
    assert not any(call[0] != "GET" and call[1].startswith("projects/") for call in api.calls)


async def navigation(page: Page, api: ProjectsApi, labels: dict) -> None:
    """三語/窄屏の keyboard 開閉・選択・平台 route・logout を実導航から辿る。"""
    await selected(page, PROJECT)
    await expect(page.locator(".historyPage")).to_be_visible()
    if api.screenshot:
        await page.screenshot(
            path=str(api.screenshot.with_name(api.screenshot.stem + "-entry.png")),
            full_page=True,
        )
    toggle = page.locator(".sidebarMenuToggle")
    if await toggle.is_visible():
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        await expect(page.locator(".sideNavProject select")).not_to_be_visible()
        await toggle.focus()
        await page.keyboard.press("Tab")
        assert not await page.evaluate(
            "document.querySelector('.navigationPanel').contains(document.activeElement)"
        ), "Closed navigation remained in Tab order"
        await toggle.focus()
        await page.keyboard.press("Enter")
        await expect(toggle).to_have_attribute("aria-expanded", "true")
        await page.locator(".sideNavProject select").focus()
        await page.keyboard.press("Escape")
        await expect(toggle).to_be_focused()
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        await page.keyboard.press("Enter")
        if api.screenshot:
            await page.screenshot(
                path=str(api.screenshot.with_name(api.screenshot.stem + "-expanded.png")),
                full_page=True,
            )
    await page.locator(".sideNavProject select").select_option(NEXT_PROJECT)
    await selected(page, NEXT_PROJECT)
    if await toggle.is_visible():
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        await expect(toggle).to_be_focused()
    await menu(page)
    account_link = page.locator('a[href="#/accounts"]')
    await account_link.focus()
    await page.keyboard.press("Enter")
    await expect(page.locator("[data-account-own]")).to_be_visible()
    assert urlsplit(page.url).fragment == "/accounts"
    await menu(page)
    await page.locator(".sidebarLogout").focus()
    await page.keyboard.press("Enter")
    await expect(page.locator('input[name="email"]')).to_be_visible()
    assert any(call[1] == "auth/logout" for call in api.calls)


async def resize_navigation(page: Page, api: ProjectsApi, _: dict) -> None:
    """狭幅・広幅間の回転でも隠れた control に焦点を残さず module 選択を辿る。"""
    await selected(page, PROJECT)
    await page.locator(".sideNavProject select").focus()
    await page.set_viewport_size({"width": 390, "height": 1000})
    toggle = page.locator(".sidebarMenuToggle")
    await expect(toggle).to_be_focused()
    await expect(toggle).to_have_attribute("aria-expanded", "false")
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await expect(page.locator(".brand")).to_be_focused()
    await page.set_viewport_size({"width": 390, "height": 1000})
    await menu(page)
    await page.locator(".sideNavSubItem").first.focus()
    await page.keyboard.press("Enter")
    await expect(toggle).to_be_focused()
    await expect(toggle).to_have_attribute("aria-expanded", "false")
    await expect(page.locator(".workspace")).to_be_visible()
    assert urlsplit(page.url).fragment == f"/workspace?project={PROJECT}"
    await no_other_project(api, PROJECT)


async def second_module_navigation(page: Page, api: ProjectsApi, labels: dict) -> None:
    """Homeから選んだ第二moduleを再認可後も保ち、その精確版のtaskだけを示す。"""
    await selected(page, PROJECT)
    await expect(page.locator(".homeRuns .emptyState")).to_be_visible()
    await menu(page)
    modules = page.locator(".sideNavSubItem")
    await expect(modules).to_have_count(2)
    await modules.nth(1).focus()
    await page.keyboard.press("Enter")
    await expect(page.locator(".workspace")).to_be_visible()
    await expect(modules.nth(1)).to_have_attribute("aria-current", "true")
    assert await modules.first.get_attribute("aria-current") is None
    await expect(page.locator("main .pageHeader h1")).to_contain_text("Browser module 2")
    await (
        page.locator(".runLauncher")
        .get_by_role("button", name=labels["workspace"]["openNewRun"])
        .click()
    )
    options = page.locator(".runForm select option")
    await expect(options).to_have_count(1)
    await expect(options).to_contain_text("Second module task")
    assert "First module task" not in await page.locator(".runForm").inner_text()
    assert any(call[1] == f"projects/{PROJECT}/tasks" for call in api.calls)
    await no_other_project(api, PROJECT)


async def loading_blocks(page: Page, api: ProjectsApi, _: dict) -> None:
    """認可詳細を待つ間は初期 Run/Task も含め画面の依存 fetch を始めない。"""
    gate = api.gates[("GET", f"projects/{ARCHIVED}")]
    await asyncio.wait_for(gate.received.wait(), 10)
    await expect(page.locator('[data-project-context="loading"]')).to_be_visible()
    await no_other_project(api, None)
    gate.release.set()
    await selected(page, ARCHIVED)
    await expect(page.locator(".historyPage")).to_be_visible()
    await no_other_project(api, ARCHIVED)


async def loading_default(page: Page, api: ProjectsApi, _: dict) -> None:
    """一覧待機中は選択未確定の Project 依存画面や preference 保存を始めない。"""
    gate = api.gates[("GET", "projects")]
    await asyncio.wait_for(gate.received.wait(), 10)
    await expect(page.locator('[data-project-context="loading"]')).to_be_visible()
    await no_other_project(api, None)
    gate.release.set()
    await default_selection(page, api, {})


async def retry_default(page: Page, api: ProjectsApi, _: dict) -> None:
    """無指定 URL の一覧失敗も、明示再読取まで Project 依存要求を保留する。"""
    await expect(page.locator('[data-project-context="error"]')).to_be_visible()
    await no_other_project(api, None)
    api.reject.clear()
    await page.locator('[data-project-context="error"] button').click()
    await default_selection(page, api, {})


async def retry_original(page: Page, api: ProjectsApi, _: dict) -> None:
    """一時読取失敗は元 target の明示再読込で回復し、別の preference に逃がさない。"""
    target = urlsplit(page.url).fragment
    await expect(page.locator('[data-project-context="error"]')).to_be_visible()
    await no_other_project(api, None)
    api.reject.clear()
    await page.locator('[data-project-context="error"] button').click()
    await expect(page.locator(".historyPage")).to_be_visible()
    await selected(page, ARCHIVED)
    assert urlsplit(page.url).fragment == target
    await no_other_project(api, ARCHIVED)


async def explicit_list_failure(page: Page, api: ProjectsApi, labels: dict) -> None:
    """一覧は認可の正本ではなく、一覧500でも精確詳細で許可された履歴を読める。"""
    await selected(page, PROJECT)
    await expect(page.locator(".historyPage")).to_be_visible()
    await expect(page.get_by_role("alert")).to_contain_text(labels["app"]["loadProjectsFailed"])
    assert urlsplit(page.url).fragment == api.fragment
    await no_other_project(api, PROJECT)
    api.reject.clear()
    await page.locator(".sideNavProject button").click()
    await expect(page.locator(".sideNavProject select option")).to_have_count(2)
    await expect(page.locator(".historyPage .emptyState")).to_be_visible()
    await selected(page, PROJECT)
    await expect(page.locator(".sideNavProject [role='alert']")).to_have_count(0)
    assert urlsplit(page.url).fragment == api.fragment
    await no_other_project(api, PROJECT)


async def preference_failure(page: Page, api: ProjectsApi, labels: dict) -> None:
    """preference503は成功した活動一覧を隠さず、無指定の初回だけ先頭を選べる。"""
    await selected(page, PROJECT)
    await expect(page.locator(".historyPage .emptyState")).to_be_visible()
    await expect(page.get_by_role("alert")).to_contain_text(labels["app"]["loadPreferenceFailed"])
    assert urlsplit(page.url).fragment == "/history"
    await no_other_project(api, PROJECT)


async def expired_detail(page: Page, api: ProjectsApi, _: dict) -> None:
    """timerより先に返っても実経過期限を越えた認可応答を採用せず明示再読取する。"""
    gate = api.gates[("GET", f"projects/{ARCHIVED}")]
    await asyncio.wait_for(gate.received.wait(), 10)
    await page.evaluate("""() => {
        const original = performance.now.bind(performance);
        performance.now = () => original() + 30001;
    }""")
    gate.release.set()
    await expect(page.locator('[data-project-context="error"]')).to_be_visible()
    await no_other_project(api, None)
    del api.gates[("GET", f"projects/{ARCHIVED}")]
    await page.locator('[data-project-context="error"] button').click()
    await archived_history(page, api, {})


async def unauthorized_session(page: Page, api: ProjectsApi, _: dict) -> None:
    """詳細認可 401 は匿名画面へ戻し、別 Project や logout の再送をしない。"""
    await expect(page.locator('input[name="email"]')).to_be_visible()
    await no_other_project(api, None)
    assert not any(call[1] == "auth/logout" for call in api.calls)


async def empty_projects(page: Page, api: ProjectsApi, labels: dict) -> None:
    """空集合で Project page を偽 mount せず、アカウントと組織管理の入口を残す。"""
    await expect(page.locator('[data-project-context="empty"]')).to_be_visible()
    await no_other_project(api, None)
    await menu(page)
    await page.locator('a[href="#/accounts"]').click()
    await expect(page.locator("[data-account-own]")).to_be_visible()
    await menu(page)
    await page.locator('a[href="#/projects"]').click()
    await expect(
        page.get_by_role("heading", name=labels["routes"]["projects"]["label"]).first
    ).to_be_visible()
    await no_other_project(api, None)


async def history_navigation(page: Page, api: ProjectsApi, _: dict) -> None:
    """hash・back/forward・reload が常に元の明示 target と認可結果を保つ。"""
    await selected(page, PROJECT)
    await expect(page.locator(".historyPage .emptyState")).to_be_visible()
    api.calls.clear()
    api.fragment = f"/history?project={MISSING}"
    await page.evaluate("target => { location.hash = target }", f"/history?project={MISSING}")
    await inaccessible(page, api, {})
    await page.go_back()
    await selected(page, PROJECT)
    await expect(page.locator(".historyPage .emptyState")).to_be_visible()
    api.calls.clear()
    await page.go_forward()
    await inaccessible(page, api, {})
    api.calls.clear()
    await page.reload()
    await inaccessible(page, api, {})


async def late_target(page: Page, api: ProjectsApi, _: dict) -> None:
    """abort を無視した旧認可詳細の成功/401が新 Project の表示を上書きしない。"""
    gate = api.gates[("GET", f"projects/{ARCHIVED}")]
    await asyncio.wait_for(gate.received.wait(), 10)
    await page.evaluate("target => { location.hash = target }", f"/history?project={NEXT_PROJECT}")
    await selected(page, NEXT_PROJECT)
    await expect(page.locator(".historyPage")).to_be_visible()
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 10)
    await settle(page)
    await selected(page, NEXT_PROJECT)
    assert urlsplit(page.url).fragment == f"/history?project={NEXT_PROJECT}"
    await expect(page.locator('input[name="email"]')).to_have_count(0)
    assert not api.project_reads(ARCHIVED)


async def late_actor(page: Page, api: ProjectsApi, labels: dict) -> None:
    """旧 actor 詳細の遅い拒否が別の実 LoginPage 認証へ侵入しない。"""
    gate = api.gates[("GET", f"projects/{ARCHIVED}")]
    await asyncio.wait_for(gate.received.wait(), 10)
    await page.evaluate("location.hash = '/accounts'")
    await expect(page.locator("[data-account-own]")).to_be_visible()
    await self_revoke(page, api, labels["account"])
    api.actor = OTHER
    api.projects = []
    api.preference = None
    await page.locator('input[name="email"]').fill(api.users[OTHER]["email"])
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator('button[type="submit"]').click()
    await expect(page.locator("[data-account-own]")).to_be_visible()
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 10)
    await settle(page)
    await expect(page.locator(".sidebarUser")).to_contain_text(api.users[OTHER]["email"])
    await expect(page.locator('input[name="email"]')).to_have_count(0)
    assert not api.project_reads(ARCHIVED)


async def late_pending(page: Page, api: ProjectsApi, _: dict) -> None:
    """旧 Project の導航・履歴読取の遅い401で無権限先の現会話を失効させない。"""
    gate = api.gates[("GET", f"projects/{PROJECT}/runs")]
    await asyncio.wait_for(
        asyncio.gather(
            gate.received.wait(),
            api.history_requested.wait(),
            api.modules_requested.wait(),
        ),
        10,
    )
    api.calls.clear()
    api.fragment = f"/history?project={MISSING}"
    await page.evaluate(
        "target => { window.projectFetchTrace = []; location.hash = target }",
        api.fragment,
    )
    await expect(page.locator('[data-project-context="unavailable"]')).to_be_visible()
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 10)
    await settle(page)
    await expect(page.locator('input[name="email"]')).to_have_count(0)
    await expect(page.locator(".navBadge")).to_have_count(0)
    await expect(page.locator('[data-project-context="unavailable"]')).to_be_visible()
    assert urlsplit(page.url).fragment == api.fragment
    trace = await page.evaluate("window.projectFetchTrace")
    assert not any(f"/projects/{PROJECT}/" in call["url"] for call in trace), trace
    assert not any(call["method"] != "GET" for call in trace), trace


async def returning_target_revalidates(page: Page, api: ProjectsApi, _: dict) -> None:
    """A→保留B→Aでも古いAの成功stateを使わず、戻ったAの404を待つ。"""
    await selected(page, PROJECT)
    await expect(page.locator(".historyPage .emptyState")).to_be_visible()
    later = ResponseGate()
    api.gates[("GET", f"projects/{NEXT_PROJECT}")] = later
    await page.evaluate("target => { location.hash = target }", f"/history?project={NEXT_PROJECT}")
    await asyncio.wait_for(later.received.wait(), 10)
    await expect(page.locator('[data-project-context="loading"]')).to_be_visible()
    returning = ResponseGate()
    returning.failure = (404, "project_not_found")
    api.gates[("GET", f"projects/{PROJECT}")] = returning
    api.calls.clear()
    api.fragment = f"/history?project={PROJECT}"
    await page.evaluate("target => { location.hash = target }", api.fragment)
    await asyncio.wait_for(returning.received.wait(), 10)
    await expect(page.locator('[data-project-context="loading"]')).to_be_visible()
    await expect(page.locator(".historyPage")).to_have_count(0)
    await no_other_project(api, None)
    returning.release.set()
    await inaccessible(page, api, {})
    later.release.set()
    await asyncio.wait_for(later.returned.wait(), 10)
    await settle(page)
    await inaccessible(page, api, {})


async def route_revalidates(page: Page, api: ProjectsApi, _: dict) -> None:
    """同一Projectでも別画面へ移る時は再認可し、失効後のRun/Taskを読まない。"""
    await selected(page, PROJECT)
    await expect(page.locator(".historyPage .emptyState")).to_be_visible()
    gate = ResponseGate()
    gate.failure = (404, "project_not_found")
    api.gates[("GET", f"projects/{PROJECT}")] = gate
    api.calls.clear()
    api.fragment = f"/workspace?project={PROJECT}&run={RUN}&task={CATALOG_TASK}"
    await page.evaluate("target => { location.hash = target }", api.fragment)
    await asyncio.wait_for(gate.received.wait(), 10)
    await expect(page.locator('[data-project-context="loading"]')).to_be_visible()
    await expect(page.locator(".historyPage, .workspace")).to_have_count(0)
    await no_other_project(api, None)
    gate.release.set()
    await inaccessible(page, api, {})


async def default_revocation_stays_target(page: Page, api: ProjectsApi, _: dict) -> None:
    """無指定で選んだPが失効しても、再読取時に残りのQへ勝手に乗り換えない。"""
    await default_selection(page, api, {})
    await expect(page.locator(".historyPage .emptyState")).to_be_visible()
    api.projects = [api.details[NEXT_PROJECT]]
    api.reject[f"projects/{PROJECT}"] = (404, "project_not_found")
    api.calls.clear()
    api.fragment = "/workspace"
    await page.evaluate("location.hash = '/workspace'")
    await inaccessible(page, api, {})
    await page.locator('[data-project-context="unavailable"] button').click()
    await inaccessible(page, api, {})
    assert not any(call[1].startswith(f"projects/{NEXT_PROJECT}") for call in api.calls)
    assert len([call for call in api.calls if call[1] == f"projects/{PROJECT}"]) >= 2


async def layout(page: Page) -> None:
    """長い Project 名と失効 ID が page/body に横スクロールを生まないことを測る。"""
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (
        "Page overflow"
    )
    for item in await page.locator(".sidebarMenuToggle, [data-project-context]").all():
        if await item.is_visible():
            assert await item.evaluate("e => e.scrollWidth <= e.clientWidth + 1"), (
                "Control overflow"
            )


async def exercise(
    browser: Browser,
    url: str,
    name: str,
    action: Callable[[Page, ProjectsApi, dict], Awaitable[None]],
    *,
    fragment: str = f"/history?project={PROJECT}",
    setup: Callable[[ProjectsApi], None] | None = None,
    language: str = "en",
    width: int = 1440,
    ignore_abort: bool = False,
    output: Path | None = None,
) -> None:
    """case 毎の通信/JS/秘密/描画を検査し、待機 gate と browser context を閉じる。"""
    api = ProjectsApi(url, language)
    api.fragment = fragment
    api.screenshot = output / f"{name}.png" if output else None
    if setup:
        setup(api)
    context = await browser.new_context(viewport={"width": width, "height": 1000}, locale=language)
    await context.route("**/*", api.route)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.add_init_script("""(() => {
        const original = window.fetch.bind(window);
        window.projectFetchTrace = [];
        window.fetch = (input, init) => {
            window.projectFetchTrace.push({
                url: String(input), hash: location.hash, method: init?.method ?? 'GET'
            });
            return original(input,
                window.projectIgnoreAbort && init ? {...init, signal: undefined} : init);
        };
    })();""")
    if ignore_abort:
        await page.add_init_script("window.projectIgnoreAbort = true")
    try:
        await page.goto(f"{url}#{fragment}")
        labels = await messages(page, language)
        await action(page, api, labels)
        await settle(page)
        await privacy(page)
        await layout(page)
        assert not api.unexpected, api.unexpected
        assert not api.failures, api.failures
        assert not errors, errors
        if output:
            await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
        print(f"PASS {name}", flush=True)
    except Exception:
        if output:
            await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        raise
    finally:
        api.release.set()
        for gate in api.gates.values():
            gate.release.set()
        await context.close()


def hold_archived(api: ProjectsApi) -> None:
    """一覧に存在しない認可 target の結果だけを遅延する。"""
    api.gates[("GET", f"projects/{ARCHIVED}")] = ResponseGate()


async def check(url: str, output: Path | None, only: str | None) -> None:
    """本番サービスに向けられない loopback fixture URL に限定する。"""
    address = urlsplit(url)
    if address.scheme != "http" or address.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("Projects fixture URL must use a loopback Vite server")
    if (
        not address.path.endswith("/tests/browser/projects.html")
        or address.query
        or address.fragment
    ):
        raise ValueError("Only the dedicated Projects browser fixture is supported")
    if output:
        output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        executed = 0

        async def run(name: str, action: Callable, **kwargs) -> None:
            """絞り込み実行でも一致した case だけを明示報告する。"""
            nonlocal executed
            if only is None or only in name:
                await exercise(browser, url, name, action, output=output, **kwargs)
                executed += 1

        try:
            for target in (
                MISSING,
                "",
                "not-a-uuid",
                f"{PROJECT}&project={NEXT_PROJECT}",
                f"{PROJECT}&project={PROJECT}",
            ):
                await run(
                    f"invalid-{target or 'empty'}",
                    inaccessible,
                    fragment=f"/history?project={target}",
                )
            for route in ("", "tasks", "workspace", "documents", "resources"):
                await run(
                    f"invalid-route-{route or 'home'}",
                    inaccessible,
                    fragment=f"/{route}?project={MISSING}&run={RUN}&task={TASK}",
                )
            for preference in (None, PROJECT, NEXT_PROJECT, MISSING):
                await run(
                    f"default-{preference}",
                    default_selection,
                    fragment="/history",
                    setup=lambda api, preference=preference: setattr(api, "preference", preference),
                )
            await run(
                "archived-history",
                archived_history,
                fragment=f"/history?project={ARCHIVED}",
            )
            await run(
                "archived-workspace",
                archived_workspace,
                fragment=f"/workspace?project={ARCHIVED}&run={RUN}",
                setup=lambda api: setattr(api, "with_run", True),
            )
            await run(
                "workspace-draft-switch",
                workspace_draft,
                fragment=f"/workspace?project={PROJECT}&task={CATALOG_TASK}",
                setup=lambda api: setattr(api, "with_tasks", True),
            )
            await run(
                "loading-blocks",
                loading_blocks,
                fragment=f"/history?project={ARCHIVED}",
                setup=hold_archived,
            )
            await run(
                "loading-default",
                loading_default,
                fragment="/history",
                setup=lambda api: api.gates.update({("GET", "projects"): ResponseGate()}),
            )
            await run(
                "retry-default",
                retry_default,
                fragment="/history",
                setup=lambda api: api.reject.update({"projects": (503, "service_unavailable")}),
            )
            await run(
                "active-detail-404",
                inaccessible,
                setup=lambda api: api.reject.update(
                    {f"projects/{PROJECT}": (404, "project_not_found")}
                ),
            )
            await run(
                "retry-detail",
                retry_original,
                fragment=f"/history?project={ARCHIVED}",
                setup=lambda api: api.reject.update(
                    {f"projects/{ARCHIVED}": (503, "service_unavailable")}
                ),
            )
            await run(
                "explicit-list-failure",
                explicit_list_failure,
                setup=lambda api: api.reject.update({"projects": (500, "server_error")}),
            )

            def reject_preference(api: ProjectsApi) -> None:
                """保存済み候補の取得失敗を活動一覧の取得失敗と分離する。"""
                api.preference = NEXT_PROJECT
                gate = ResponseGate()
                gate.failure = (503, "service_unavailable")
                gate.release.set()
                api.gates[("GET", "users/me/project-preference")] = gate

            await run(
                "preference-failure",
                preference_failure,
                fragment="/history",
                setup=reject_preference,
            )
            await run(
                "expired-detail",
                expired_detail,
                fragment=f"/history?project={ARCHIVED}",
                setup=hold_archived,
                ignore_abort=True,
            )
            await run(
                "detail-401",
                unauthorized_session,
                fragment=f"/history?project={ARCHIVED}",
                setup=lambda api: api.reject.update(
                    {f"projects/{ARCHIVED}": (401, "session_expired")}
                ),
            )
            await run(
                "empty-projects",
                empty_projects,
                fragment="/history",
                setup=lambda api: (
                    setattr(api, "projects", []),
                    setattr(api, "preference", None),
                ),
            )
            await run("hash-history-refresh", history_navigation)
            await run(
                "returning-target-revalidates",
                returning_target_revalidates,
                ignore_abort=True,
            )
            await run("route-revalidates", route_revalidates, ignore_abort=True)
            await run(
                "default-revocation-stays-target",
                default_revocation_stays_target,
                fragment="/history",
            )

            def hold_pending(api: ProjectsApi) -> None:
                """現 Project の readonly 応答を保留し、旧401を明示的に返す。"""
                gate = ResponseGate()
                gate.failure = (401, "session_expired")
                api.gates[("GET", f"projects/{PROJECT}/runs")] = gate

            await run(
                "late-pending-project",
                late_pending,
                setup=hold_pending,
                ignore_abort=True,
            )
            for failure in (None, (401, "session_expired")):

                def late_setup(api: ProjectsApi, failure: tuple | None = failure) -> None:
                    """旧詳細の成功と失効を同じ gate で別々に観測する。"""
                    hold_archived(api)
                    api.gates[("GET", f"projects/{ARCHIVED}")].failure = failure

                await run(
                    f"late-target-{failure}",
                    late_target,
                    fragment=f"/history?project={ARCHIVED}",
                    setup=late_setup,
                    ignore_abort=True,
                )
                await run(
                    f"late-actor-{failure}",
                    late_actor,
                    fragment=f"/history?project={ARCHIVED}",
                    setup=late_setup,
                    ignore_abort=True,
                )
            for language in ("zh", "ja", "en"):
                for width in (390, 1440):
                    await run(
                        f"unavailable-{language}-{width}",
                        inaccessible,
                        fragment=f"/history?project={MISSING}",
                        language=language,
                        width=width,
                    )
                    await run(
                        f"navigation-{language}-{width}",
                        navigation,
                        language=language,
                        width=width,
                    )
            await run(
                "navigation-resize-module",
                resize_navigation,
                setup=lambda api: setattr(api, "with_module", True),
            )
            await run(
                "navigation-second-module",
                second_module_navigation,
                fragment=f"/?project={PROJECT}",
                setup=lambda api: (
                    setattr(api, "with_module", True),
                    setattr(api, "with_tasks", True),
                ),
            )
        finally:
            await browser.close()
    if executed == 0:
        raise ValueError("Case filter did not match any browser check")
    print(
        f"Projects browser checks passed: {executed}; "
        "API mock only, not real authorization/DB/HTTPS proof."
    )


def main() -> None:
    """専用 fixture URL・任意の case filter・工作区外 screenshot 出力を受け取る。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--only")
    arguments = parser.parse_args()
    asyncio.run(check(arguments.url, arguments.output, arguments.only))


if __name__ == "__main__":
    main()
