"""実AppのProject原版確認・結果未知を全面mock HTTPで検証する。"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from check_accounts import CSRF, OTHER, PASSWORD, ResponseGate, project, self_revoke
from check_projects import (
    ARCHIVED,
    NEXT_PROJECT,
    PROJECT,
    ProjectsApi,
    menu,
    messages,
    privacy,
    selected,
    settle,
)
from playwright.async_api import Browser, Locator, Page, Route, async_playwright, expect

CREATED = "00000000-0000-4000-8000-000000000090"
DRAFT_NAME = "Browser reviewed project"
DRAFT_DESCRIPTION = "Browser transient project description"
PRIVATE_DETAIL = "Fixture project refusal detail must not be shown"


class ManagementApi(ProjectsApi):
    """認証/読取fixtureを共有し、原版付きProject管理だけを局所追加する。"""

    def __init__(self, url: str, language: str, role: str) -> None:
        """疑似保存と応答喪失を分け、各caseのgateや競合を独立させる。"""
        super().__init__(url, language)
        self.role = role
        self.details = {
            identity: {**record, "key": f"fixture-{identity[-2:]}"}
            for identity, record in self.details.items()
        }
        self.sync_list()
        self.write_gate: ResponseGate | None = None
        self.initial_gate: ResponseGate | None = None
        self.write_failure: tuple[int, str] | None = None
        self.write_mode = "success"

    def sync_list(self) -> None:
        """同じ正本から活動一覧を投影し、status変更後の読取を整合させる。"""
        self.projects = [record for record in self.details.values() if record["status"] == "ACTIVE"]

    def project_mutations(self) -> list[tuple]:
        """preferenceや会話操作を除外し、実Project書込だけを数える。"""
        return [
            call
            for call in self.calls
            if call[0] != "GET" and (call[1] == "projects" or call[1].startswith("projects/"))
        ]

    async def problem(self, route: Route, failure: tuple[int, str]) -> None:
        """固定code分類と任意内部detailを分け、内部理由の非表示を試す。"""
        status, code = failure
        await route.fulfill(
            status=status,
            json={
                "type": f"https://skillmind.local/problems/{code}",
                "title": "Fixture refusal",
                "status": status,
                "code": code,
                "detail": PRIVATE_DETAIL,
            },
        )

    async def respond(self, route: Route) -> None:
        """未知HTTPは親で拒否し、全書込のOrigin/CSRF/原identity/原版を照合する。"""
        request = route.request
        address = urlsplit(request.url)
        suffix = address.path.removeprefix(self.prefix)
        method = request.method
        if (
            self.initial_gate
            and method == "GET"
            and (
                suffix == "users/me/project-preference"
                or (suffix == "projects" and not parse_qs(address.query).get("include_archived"))
            )
        ):
            self.initial_gate.received.set()
            await asyncio.wait_for(self.initial_gate.release.wait(), 35)
        if (
            f"{address.scheme}://{address.netloc}" != self.origin
            or not address.path.startswith(self.prefix)
            or not (suffix == "projects" or suffix.startswith("projects/"))
            or method == "GET"
        ):
            await super().respond(route)
            return
        assert self.role == "ADMIN", "USER attempted project administration"
        assert request.headers.get("x-csrf-token") == CSRF
        assert request.headers.get("origin") == self.origin
        query = parse_qs(address.query)
        body = request.post_data_json if request.post_data else None
        self.calls.append((method, suffix, query, body))
        parts = suffix.split("/")
        creating = suffix == "projects" and method == "POST"
        if creating:
            assert isinstance(body, dict)
            assert set(body) == {"key", "name", "description", "settings", "retention_days"}
        else:
            assert len(parts) in {2, 3} and parts[1] in self.details, "Unknown project write"
            if method == "DELETE":
                assert len(parts) == 2 and body is None
                assert set(query) == {"expected_row_version"}
                expected = int(query["expected_row_version"][0])
            else:
                assert isinstance(body, dict) and "expected_row_version" in body
                expected = body["expected_row_version"]
                assert isinstance(expected, int) and not isinstance(expected, bool)
                if method == "PATCH":
                    assert len(parts) == 2
                    assert set(body) <= {
                        "expected_row_version",
                        "name",
                        "description",
                        "settings",
                        "retention_days",
                    }
                    assert len(body) >= 2 and all(value is not None for value in body.values())
                else:
                    assert method == "POST" and len(parts) == 3
                    assert parts[2] in {"archive", "unarchive"}
                    assert set(body) == {"expected_row_version"}
        gate, failure, mode = self.write_gate, self.write_failure, self.write_mode
        if gate:
            gate.received.set()
            await asyncio.wait_for(gate.release.wait(), 35)
        try:
            if gate and gate.failure:
                failure = gate.failure
            if failure:
                await self.problem(route, failure)
                return
            if creating:
                if any(item["key"] == body["key"] for item in self.details.values()):
                    await self.problem(route, (409, "project_key_conflict"))
                    return
                result = {**project(CREATED), **body, "row_version": 1}
                self.details[CREATED] = result
            else:
                original = self.details[parts[1]]
                if expected != original["row_version"]:
                    await self.problem(route, (409, "project_version_conflict"))
                    return
                if method == "DELETE":
                    if original["status"] != "ARCHIVED":
                        await self.problem(route, (409, "project_delete_requires_archive"))
                        return
                    del self.details[parts[1]]
                    result = None
                else:
                    changes = (
                        {key: value for key, value in body.items() if key != "expected_row_version"}
                        if method == "PATCH"
                        else {"status": "ARCHIVED" if parts[2] == "archive" else "ACTIVE"}
                    )
                    changed = any(original[key] != value for key, value in changes.items())
                    result = {**original, **changes, "row_version": expected + int(changed)}
                    self.details[parts[1]] = result
            self.sync_list()
            if mode == "drop":
                await route.abort("failed")
            elif mode == "invalid":
                await route.fulfill(
                    status=201 if creating else 200,
                    json={"project_id": parts[1] if not creating else CREATED},
                )
            elif mode == "unexpected-status":
                await route.fulfill(status=200 if creating else 202, json=result)
            elif mode == "unexpected-204" or method == "DELETE":
                await route.fulfill(status=204)
            else:
                await route.fulfill(status=201 if creating else 200, json=result)
        finally:
            if gate:
                gate.returned.set()


async def tab(page: Page, name: str) -> None:
    """実tabをkeyboardで選び、hidden側に操作を配送しない。"""
    button = page.locator(f'[data-project-tab="{name}"]')
    await expect(button).to_be_visible()
    await button.focus()
    await page.keyboard.press("Enter")


async def action(page: Page, project_id: str, operation: str) -> None:
    """行の原Projectから操作を選び、選択だけではmutationしない。"""
    await page.locator(
        f'[data-project-row="{project_id}"] [data-project-action="{operation}"]'
    ).click()


async def form(page: Page, *, creating: bool = False) -> Locator:
    """非敏感草稿を入力し、送信時の原値と別の比較材料にする。"""
    result = page.locator("[data-project-form]")
    await expect(result).to_be_visible()
    if creating:
        await result.locator('input[name="key"]').fill("browser-created")
    await result.locator('input[name="name"]').fill(DRAFT_NAME)
    await result.locator('textarea[name="description"]').fill(DRAFT_DESCRIPTION)
    await result.locator('input[name="retention_days"]').fill("45")
    return result


async def freeze(page: Page, target: str = PROJECT, operation: str = "edit") -> None:
    """作成/編集も明示的確認を作り、確認前HTTPが無いことをcaseで検査する。"""
    if operation in {"restore", "delete"}:
        await tab(page, "archived")
    if operation != "create":
        await action(page, target, operation)
    if operation in {"create", "edit"}:
        draft = await form(page, creating=operation == "create")
        await draft.evaluate("form => form.requestSubmit()")
    await expect(page.locator("[data-project-intent]")).to_be_visible()
    await expect(page.locator("[data-project-intent] h2")).to_be_focused()


async def confirm(page: Page) -> None:
    """同tick二回配送し、描画disabled以外の同期guardを検証する。"""
    await page.locator("[data-project-confirm]").evaluate("""button => {
        for (let i=0; i<2; i++) button.dispatchEvent(
            new MouseEvent('click', {bubbles: true, cancelable: true}));
    }""")


async def success(page: Page) -> None:
    """成功確認後に原intentが消えるのを待ち、自動再送をしないことを後続で照合する。"""
    await expect(page.locator("[data-project-intent]")).to_have_count(0)
    await expect(page.locator("[data-project-unknown], [data-project-conflict]")).to_have_count(0)
    await settle(page)


async def basic_flow(page: Page, api: ManagementApi, _: dict) -> None:
    """作成・編集・アーカイブ・復元・削除を原版で辿り、同tick確認を毎回一通にする。"""
    await freeze(page, operation="create")
    await page.locator("[data-project-cancel]").click()
    await expect(page.locator(".projectSidePanel > h2")).to_be_focused()
    assert not api.project_mutations()
    await freeze(page, operation="create")
    if api.screenshot:
        await page.screenshot(
            path=str(api.screenshot.with_stem(api.screenshot.stem + "-confirm")), full_page=True
        )
    assert not api.project_mutations()
    await confirm(page)
    await success(page)
    await expect(page.locator(f'[data-project-row="{CREATED}"]')).to_be_visible()
    await expect(page.locator(".projectSidePanel > h2")).to_be_focused()
    assert api.details[CREATED]["row_version"] == 1
    for operation, expected in (
        ("edit", 1),
        ("archive", 2),
        ("restore", 3),
        ("archive", 4),
        ("delete", 5),
    ):
        await tab(page, "archived" if operation in {"restore", "delete"} else "projects")
        await freeze(page, CREATED, operation)
        if operation == "edit":
            # 作成時の値と異なる草稿にし、実変更の一回だけ版を進める。
            await page.locator("[data-project-cancel]").click()
            await page.locator('[data-project-form] input[name="name"]').fill(
                DRAFT_NAME + " edited"
            )
            await page.locator("[data-project-form]").evaluate("form => form.requestSubmit()")
        await confirm(page)
        await success(page)
        await expect(
            page.locator(
                ".archivedProjects .panelHeader h2"
                if operation in {"restore", "delete"}
                else ".projectSidePanel > h2"
            )
        ).to_be_focused()
        call = api.project_mutations()[-1]
        assert (
            int(call[2]["expected_row_version"][0])
            if operation == "delete"
            else call[3]["expected_row_version"]
        ) == expected
    assert CREATED not in api.details
    assert len(api.project_mutations()) == 6


async def conflict(
    page: Page, api: ManagementApi, labels: dict, second_unknown: bool = False
) -> None:
    """旧版の拒否後も草稿を保持し、精確な再読取と人による版の採用は再送しない。"""
    await freeze(page)
    api.details[PROJECT] = {**api.details[PROJECT], "name": "Concurrent project", "row_version": 8}
    api.sync_list()
    await confirm(page)
    await expect(page.locator("[data-project-conflict]")).to_be_visible()
    assert api.project_mutations()[0][3]["expected_row_version"] == 7
    before = len(api.calls)
    await page.locator("[data-project-reconcile]").click()
    comparison = page.locator("[data-project-comparison]")
    await expect(comparison).to_be_visible()
    await expect(comparison).to_contain_text("Concurrent project")
    await expect(comparison).to_contain_text(DRAFT_NAME)
    assert any(call[:2] == ("GET", f"projects/{PROJECT}") for call in api.calls[before:])
    gate = ResponseGate()
    api.gates[("GET", f"projects/{PROJECT}")] = gate
    await page.evaluate("""() => {
        const refresh = document.querySelector('[data-project-reconcile]');
        const adopt = document.querySelector('[data-project-adopt]');
        refresh.click(); adopt.click();
    }""")
    await asyncio.wait_for(gate.received.wait(), 10)
    await expect(page.locator("[data-project-conflict]")).to_be_visible()
    await expect(page.locator("[data-project-intent]")).to_have_count(0)
    assert len(api.project_mutations()) == 1
    gate.release.set()
    await expect(comparison).to_be_visible()
    if api.screenshot:
        await page.screenshot(
            path=str(api.screenshot.with_stem(api.screenshot.stem + "-comparison")), full_page=True
        )
    await page.locator("[data-project-adopt]").click()
    assert len(api.project_mutations()) == 1
    await expect(page.locator("[data-project-confirm]")).to_be_enabled()
    if second_unknown:
        api.write_mode = "drop"
    await confirm(page)
    if second_unknown:
        area = page.locator("[data-project-unknown]")
        await expect(area).to_be_visible()
        original = area.locator(".projectFacts").first
        await expect(original).to_contain_text("Concurrent project")
        await expect(page.locator('[data-project-form] input[name="name"]')).to_have_value(
            DRAFT_NAME
        )
        await expect(
            original.locator("dl div")
            .filter(has=page.locator("dt", has_text=labels["projectManagement"]["version"]))
            .locator("dd")
        ).to_have_text("8")
        assert len(api.project_mutations()) == 2
        assert api.project_mutations()[1][3]["expected_row_version"] == 8
        return
    await success(page)
    assert len(api.project_mutations()) == 2
    assert api.project_mutations()[1][3]["expected_row_version"] == 8
    assert api.details[PROJECT]["name"] == DRAFT_NAME


async def unknown(page: Page, api: ManagementApi, _: dict, operation: str) -> None:
    """現在値の一致/不在を元操作の成功/rollbackと呼ばず、照合と人の解除を分ける。"""
    target = ARCHIVED if operation in {"restore", "delete"} else PROJECT
    await freeze(page, target, operation)
    await confirm(page)
    area = page.locator("[data-project-unknown]")
    await expect(area).to_be_visible()
    assert len(api.project_mutations()) == 1
    before = len(api.calls)
    await area.locator("[data-project-reconcile]").click()
    await expect(area.locator("[data-project-reviewed]")).to_be_visible()
    await expect(area.locator("[data-project-acknowledged]")).to_be_enabled()
    reads = api.calls[before:]
    if operation == "create":
        assert any(
            call[1] == "projects" and call[2].get("include_archived") == ["true"] for call in reads
        )
    else:
        assert any(call[:2] == ("GET", f"projects/{target}") for call in reads)
    await expect(area.locator("[data-project-acknowledge]")).to_be_disabled()
    if api.screenshot:
        await page.screenshot(
            path=str(api.screenshot.with_stem(api.screenshot.stem + "-reviewed")), full_page=True
        )
    await area.locator("[data-project-acknowledged]").check()
    await area.locator("[data-project-acknowledge]").click()
    await expect(area).to_have_count(0)
    await expect(
        page.locator(
            ".archivedProjects .panelHeader h2"
            if operation in {"restore", "delete"}
            else ".projectSidePanel > h2"
        )
    ).to_be_focused()
    assert len(api.project_mutations()) == 1


async def refusal(page: Page, api: ManagementApi, _: dict) -> None:
    """既知拒否は元草稿を保持し、401以外を会話失効やunknownへ読み替えない。"""
    deleting = bool(api.write_failure and "delete_blocked" in api.write_failure[1])
    await freeze(page, ARCHIVED if deleting else PROJECT, "delete" if deleting else "edit")
    await confirm(page)
    if api.write_failure and api.write_failure[0] == 401:
        await expect(page.locator('input[name="email"]')).to_be_visible()
    else:
        await expect(page.locator("[data-project-management] [role=alert]")).to_be_visible()
        await expect(page.locator("[data-project-unknown]")).to_have_count(0)
        await expect(page.locator("[data-project-intent]")).to_contain_text(
            api.details[ARCHIVED]["key"] if deleting else DRAFT_NAME
        )
    assert len(api.project_mutations()) == 1


async def failed_review(
    page: Page,
    api: ManagementApi,
    _: dict,
    creating: bool,
    failure: tuple[int, str] = (500, "server_error"),
) -> None:
    """照合用の読取が失敗した状態で旧事実を使い新writeを解禁しない。"""
    await freeze(page, operation="create" if creating else "edit")
    await confirm(page)
    area = page.locator("[data-project-unknown]")
    await expect(area).to_be_visible()
    suffix = "projects" if creating else f"projects/{PROJECT}"
    api.reject[suffix] = failure
    await area.locator("[data-project-reconcile]").click()
    await expect(area.get_by_role("alert")).to_be_visible()
    await expect(area.locator("[data-project-acknowledged]")).to_have_count(0)
    assert len(api.project_mutations()) == 1
    del api.reject[suffix]
    await area.locator("[data-project-reconcile]").click()
    await expect(area.locator("[data-project-acknowledged]")).to_be_enabled()
    assert len(api.project_mutations()) == 1


async def unknown_tabs(page: Page, api: ManagementApi, _: dict) -> None:
    """tabを離れて戻っても未知要求の門禁は解除されず、別の原操作へ移さない。"""
    await freeze(page)
    await confirm(page)
    await expect(page.locator("[data-project-unknown]")).to_be_visible()
    await page.locator('[data-project-list-refresh="active"]').click()
    await expect(page.locator(f'[data-project-row="{PROJECT}"]')).to_contain_text(DRAFT_NAME)
    await expect(page.locator("[data-project-unknown]")).to_contain_text(project(PROJECT)["name"])
    await expect(
        page.locator(f'[data-project-row="{PROJECT}"] [data-project-action="edit"]')
    ).to_be_disabled()
    await tab(page, "modules")
    await tab(page, "projects")
    await expect(page.locator("[data-project-unknown]")).to_be_visible()
    assert len(api.project_mutations()) == 1


async def late_project(page: Page, api: ManagementApi, _: dict, returning: bool) -> None:
    """元Aの遅い書込がBや新しいAへ成功/未知表示を届けないことを実Appで観測する。"""
    await freeze(page)
    gate = ResponseGate()
    api.write_gate = gate
    await confirm(page)
    await asyncio.wait_for(gate.received.wait(), 10)
    for identity in (NEXT_PROJECT, PROJECT) if returning else (NEXT_PROJECT,):
        await menu(page)
        await page.locator(".sideNavProject select").select_option(identity)
        await selected(page, identity)
        await expect(page.locator("[data-project-form]")).to_be_visible()
    await expect(page.locator("[data-project-intent], [data-project-unknown]")).to_have_count(0)
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 10)
    await settle(page)
    await expect(page.locator("[data-project-intent], [data-project-unknown]")).to_have_count(0)
    await expect(page.locator('[data-project-form] input[name="name"]')).not_to_have_value(
        DRAFT_NAME
    )
    assert len(api.project_mutations()) == 1


async def late_actor(page: Page, api: ManagementApi, labels: dict, status: int) -> None:
    """本人失効後に実Loginを経由し、旧actorの応答は新会話を変更しない。"""
    await freeze(page)
    gate = ResponseGate()
    if status != 200:
        gate.failure = (status, "session_expired" if status == 401 else "server_error")
    api.write_gate = gate
    await confirm(page)
    await asyncio.wait_for(gate.received.wait(), 10)
    await page.evaluate("location.hash = '/accounts'")
    await expect(page.locator("[data-account-own]")).to_be_visible()
    await self_revoke(page, api, labels["account"])
    api.actor = OTHER
    await page.locator('input[name="email"]').fill(api.users[OTHER]["email"])
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator('button[type="submit"]').click()
    await expect(page.locator("[data-account-own]")).to_be_visible()
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 10)
    await settle(page)
    await expect(page.locator('input[name="email"]')).to_have_count(0)
    await expect(page.locator(".sidebarUser")).to_contain_text(api.users[OTHER]["email"])


async def deadline(page: Page, api: ManagementApi, _: dict) -> None:
    """30秒timerで結果未知を確定し、その後の200も成功へ昇格しない。"""
    await freeze(page)
    await page.clock.install()
    gate = ResponseGate()
    api.write_gate = gate
    await confirm(page)
    await asyncio.wait_for(gate.received.wait(), 10)
    await page.clock.fast_forward(30_001)
    await expect(page.locator("[data-project-unknown]")).to_be_visible()
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 10)
    await settle(page)
    await expect(page.locator("[data-project-unknown]")).to_be_visible()
    assert len(api.project_mutations()) == 1


async def ordinary_user(page: Page, api: ManagementApi, _: dict) -> None:
    """USERは活動Projectを選べても管理フォームと全CRUD操作を持たない。"""
    await selected(page, PROJECT)
    await expect(page.locator("main .pageHeader")).to_be_visible()
    await settle(page)
    await expect(
        page.locator(
            '[data-project-form], [data-project-action]:not([data-project-action="select"])'
        )
    ).to_have_count(0)
    assert not any(
        call[1] == "projects" and call[2].get("include_archived") == ["true"] for call in api.calls
    )
    assert not api.project_mutations()


async def no_op(page: Page, api: ManagementApi, _: dict) -> None:
    """同版同値の成功を受け入れ、版が必ず増えると捏造しない。"""
    await action(page, PROJECT, "edit")
    await page.locator("[data-project-form]").evaluate("form => form.requestSubmit()")
    await expect(page.locator("[data-project-intent]")).to_be_visible()
    await confirm(page)
    await success(page)
    assert api.details[PROJECT]["row_version"] == 7
    assert len(api.project_mutations()) == 1


async def legacy_lifecycle(page: Page, api: ManagementApi, _: dict) -> None:
    """合法な歴史空白nameをUI都合で改名せず、長い原keyも狭幅で保って保守する。"""
    for operation, expected in (("restore", 7), ("archive", 8), ("delete", 9)):
        await tab(page, "projects" if operation == "archive" else "archived")
        await freeze(page, ARCHIVED, operation)
        await confirm(page)
        await success(page)
        call = api.project_mutations()[-1]
        version = (
            int(call[2]["expected_row_version"][0])
            if operation == "delete"
            else call[3]["expected_row_version"]
        )
        assert version == expected
        if operation != "delete":
            assert api.details[ARCHIVED]["name"] == "   "
            assert api.details[ARCHIVED]["row_version"] == expected + 1
    assert ARCHIVED not in api.details
    assert len(api.project_mutations()) == 3


async def create_without_list(page: Page, api: ManagementApi, _: dict) -> None:
    """一覧失敗でもplatform作成は使え、未読のProject版を流用しない。"""
    await freeze(page, operation="create")
    await confirm(page)
    await success(page)
    assert api.details[CREATED]["row_version"] == 1
    assert api.project_mutations()[0][0:2] == ("POST", "projects")
    assert len(api.project_mutations()) == 1


async def initial_selection(page: Page, api: ManagementApi, _: dict, phase: str) -> None:
    """初回の既定Project解決は手動切替でなく、platform作成草稿/確認/未知を消さない。"""
    assert api.initial_gate is not None
    await asyncio.wait_for(api.initial_gate.received.wait(), 10)
    await expect(page.locator(f'[data-project-row="{PROJECT}"]')).to_be_visible()
    await form(page, creating=True)
    if phase != "draft":
        await page.locator("[data-project-form]").evaluate("form => form.requestSubmit()")
        await expect(page.locator("[data-project-intent]")).to_be_visible()
    if phase == "unknown":
        api.write_mode = "drop"
        await confirm(page)
        await expect(page.locator("[data-project-unknown]")).to_be_visible()
    api.initial_gate.release.set()
    await selected(page, PROJECT)
    await expect(page.locator(".sideNavProject select")).to_have_value(PROJECT)
    await settle(page)
    if phase == "unknown":
        await expect(page.locator("[data-project-unknown]")).to_be_visible()
        assert len(api.project_mutations()) == 1
    elif phase == "confirm":
        await expect(page.locator("[data-project-intent]")).to_contain_text(DRAFT_NAME)
        assert not api.project_mutations()
    else:
        await expect(page.locator('[data-project-form] input[name="name"]')).to_have_value(
            DRAFT_NAME
        )
        assert not api.project_mutations()


async def exercise(
    browser: Browser,
    url: str,
    name: str,
    check: Callable[[Page, ManagementApi, dict], Awaitable[None]],
    *,
    language: str = "en",
    width: int = 1440,
    role: str = "ADMIN",
    fragment: str = f"#/projects?project={PROJECT}",
    setup: Callable[[ManagementApi], None] | None = None,
    output: Path | None = None,
) -> None:
    """本番Appを一度だけmountし、未知HTTP・JS error・機密・横溢れを全caseで検査する。"""
    api = ManagementApi(url, language, role)
    api.screenshot = output / f"{name}.png" if output else None
    if setup:
        setup(api)
    context = await browser.new_context(viewport={"width": width, "height": 1000}, locale=language)
    await context.route("**/*", api.route)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(error.stack))
    await page.add_init_script("""(() => {
        const original = window.fetch.bind(window);
        window.fetch = (input, init) => original(input, init ? {...init, signal: undefined} : init);
    })();""")
    try:
        await page.goto(f"{url}{fragment}")
        await check(page, api, await messages(page, language))
        await settle(page)
        await privacy(page)
        assert PRIVATE_DETAIL not in await page.locator("body").inner_text()
        assert DRAFT_DESCRIPTION not in page.url
        assert DRAFT_DESCRIPTION not in await page.evaluate(
            "JSON.stringify({...localStorage,...sessionStorage})"
        )
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (
            "Page overflow"
        )
        assert not api.unexpected, api.unexpected
        assert not api.failures, api.failures
        assert not errors, errors
        if output:
            await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
            if name.startswith("management-"):
                await page.evaluate("window.scrollTo(0, 0)")
                await page.screenshot(path=str(output / f"{name}-viewport.png"))
        print(f"PASS {name}", flush=True)
    except Exception:
        if output:
            await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        print("Fixture failures:", api.unexpected, api.failures, errors, flush=True)
        raise
    finally:
        for gate in [api.write_gate, api.initial_gate, *api.gates.values()]:
            if gate:
                gate.release.set()
        api.release.set()
        await context.close()


async def check(url: str, output: Path | None, only: str | None) -> None:
    """専用loopback入口以外を拒否し、実完了caseだけを成功数に含める。"""
    address = urlsplit(url)
    if (
        address.scheme != "http"
        or address.hostname not in {"127.0.0.1", "localhost", "::1"}
        or not address.path.endswith("/tests/browser/projects.html")
        or address.query
        or address.fragment
    ):
        raise ValueError("Use the loopback projects.html fixture, never a real service")
    if output:
        output.mkdir(parents=True, exist_ok=True)
    count = 0
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()

        async def run(name: str, check: Callable, **kwargs) -> None:
            """任意caseの絞り込みを行い、空集合を成功と報告しない。"""
            nonlocal count
            if only is None or only in name:
                await exercise(browser, url, name, check, output=output, **kwargs)
                count += 1

        try:
            for language in ("zh", "ja", "en"):
                for width in (390, 1440):
                    await run(
                        f"management-{language}-{width}", basic_flow, language=language, width=width
                    )
            await run("version-conflict", conflict)

            async def adopted_unknown(page: Page, api: ManagementApi, labels: dict) -> None:
                """再送信のunknownが最初の競合版ではなく採用した版を原値として保持する。"""
                await conflict(page, api, labels, second_unknown=True)

            await run("conflict-adopted-unknown", adopted_unknown)
            await run("ordinary-user", ordinary_user, role="USER", width=390)
            await run("same-version-no-op", no_op)

            def legacy(api: ManagementApi) -> None:
                """契約上合法な歴史値だけを用い、新UIの入力制約とは切り分ける。"""
                api.details[ARCHIVED] = {
                    **api.details[ARCHIVED],
                    "name": "   ",
                    "key": "old-" + "x" * 96,
                }

            await run("legacy-whitespace-lifecycle", legacy_lifecycle, setup=legacy, width=390)
            await run(
                "create-list-failure",
                create_without_list,
                setup=lambda api: api.reject.update({"projects": (500, "server_error")}),
            )
            for phase in ("draft", "confirm", "unknown"):

                async def initial(
                    page: Page, api: ManagementApi, labels: dict, phase: str = phase
                ) -> None:
                    """遅い既定Project選択時の三つの作成段階を固定する。"""
                    await initial_selection(page, api, labels, phase)

                await run(
                    f"initial-selection-{phase}",
                    initial,
                    fragment="#/projects",
                    setup=lambda api: setattr(api, "initial_gate", ResponseGate()),
                )
            for status, code in (
                (401, "session_expired"),
                (403, "administrator_required"),
                (404, "project_not_found"),
                (422, "validation_error"),
                (409, "project_version_exhausted"),
                (409, "project_delete_blocked_by_runs"),
                (409, "project_delete_blocked_by_schedules"),
                (409, "project_delete_blocked_by_member_audit"),
            ):
                await run(
                    f"refusal-{status}-{code}",
                    refusal,
                    setup=lambda api, status=status, code=code: setattr(
                        api, "write_failure", (status, code)
                    ),
                )
            for operation in ("create", "edit", "archive", "restore", "delete"):
                for mode in ("drop", "invalid", "unexpected-status", "unexpected-204", "500"):
                    if operation == "delete" and mode == "unexpected-204":
                        continue

                    def configure(api: ManagementApi, mode: str = mode) -> None:
                        """500と異常成功を区別し、元の操作が適用されたかは別に保持する。"""
                        if mode == "500":
                            api.write_failure = (500, "server_error")
                        else:
                            api.write_mode = mode

                    async def uncertain(
                        page: Page, api: ManagementApi, labels: dict, operation: str = operation
                    ) -> None:
                        """各原操作を閉じ込め、case間のloop変数を持ち越さない。"""
                        await unknown(page, api, labels, operation)

                    await run(f"unknown-{operation}-{mode}", uncertain, setup=configure)
            await run(
                "unknown-tabs", unknown_tabs, setup=lambda api: setattr(api, "write_mode", "drop")
            )
            for creating in (False, True):

                async def failed_read(
                    page: Page, api: ManagementApi, labels: dict, creating: bool = creating
                ) -> None:
                    """原IDと原keyの読取失敗を独立したcaseとして固定する。"""
                    await failed_review(page, api, labels, creating)

                await run(
                    f"unknown-read-failure-{creating}",
                    failed_read,
                    setup=lambda api: setattr(api, "write_mode", "drop"),
                )
            await run("pending-timeout", deadline)

            async def unclassified_404(page: Page, api: ManagementApi, labels: dict) -> None:
                """proxy等の不明404をProject不可読という安全な確定事実にしない。"""
                await failed_review(page, api, labels, False, (404, "proxy_not_found"))

            await run(
                "unknown-unclassified-404",
                unclassified_404,
                setup=lambda api: setattr(api, "write_mode", "drop"),
            )
            for returning in (False, True):
                for status in (200, 500):

                    async def late(
                        page: Page, api: ManagementApi, labels: dict, returning: bool = returning
                    ) -> None:
                        """片道と往復を別caseとして元responseを配送する。"""
                        await late_project(page, api, labels, returning)

                    await run(
                        f"late-project-{returning}-{status}",
                        late,
                        setup=lambda api, status=status: setattr(
                            api, "write_failure", (500, "server_error") if status == 500 else None
                        ),
                    )
            for status in (200, 401, 500):

                async def actor(
                    page: Page, api: ManagementApi, labels: dict, status: int = status
                ) -> None:
                    """旧actorの成功・失効・未知を独立させる。"""
                    await late_actor(page, api, labels, status)

                await run(f"late-actor-{status}", actor)
        finally:
            await browser.close()
    if not count:
        raise ValueError("Case filter did not match any check")
    print(
        f"Project management browser checks passed: {count}; "
        "mock HTTP only, no real DB/authorization proof."
    )


def main() -> None:
    """明示されたloopback fixtureとworkspace外の証拠directoryを受け付ける。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--only")
    arguments = parser.parse_args()
    asyncio.run(check(arguments.url, arguments.output, arguments.only))


if __name__ == "__main__":
    main()
