"""実 App の Project membership 管理を loopback・全面 mock HTTP で検証する。"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from check_accounts import ACTOR, CSRF, OTHER, PASSWORD, ResponseGate, self_revoke
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

ACTIVE = "00000000-0000-4000-8000-000000000100"
REMOVED = "00000000-0000-4000-8000-000000000101"
ADMIN = "00000000-0000-4000-8000-000000000103"
DISABLED = "00000000-0000-4000-8000-000000000104"
CANDIDATE = "00000000-0000-4000-8000-000000000105"
LETTER_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
PRIVATE_DETAIL = "Fixture internal membership refusal must not reach users"


class MembersApi(ProjectsApi):
    """既存の認証/Project mock を共有し、membership 公開操作だけ追加する。"""

    def __init__(self, url: str, language: str, role: str) -> None:
        """関係状態と account 状態を分離し、case ごとに原要求と応答 gate を保持する。"""
        super().__init__(url, language)
        self.role = role
        self.users[ACTOR]["system_role"] = role
        self.users[ADMIN]["system_role"] = "ADMIN"
        self.users[DISABLED]["status"] = "DISABLED"
        self.members = {
            project_id: {
                ACTIVE: self.member(ACTIVE, "ACTIVE"),
                REMOVED: self.member(REMOVED, "REMOVED"),
            }
            for project_id in (PROJECT, NEXT_PROJECT, ARCHIVED)
        }
        self.write_failure: tuple[int, str] | None = None
        self.write_gate: ResponseGate | None = None
        self.write_mode = "success"
        self.member_read_failure: tuple[int, str] | None = None
        self.member_read_gate: ResponseGate | None = None

    def member(self, user_id: str, status: str) -> dict:
        """一覧には現行公開 contract の五項目だけを返し、account 有効性を捏造しない。"""
        user = self.users[user_id]
        return {
            "user_id": user_id,
            "email": user["email"],
            "display_name": user["display_name"],
            "status": status,
            "joined_at": "2026-09-09T00:00:00Z",
        }

    def member_mutations(self) -> list[tuple]:
        """preference/認証の保存を membership 書込から分離する。"""
        return [call for call in self.calls if "/members/" in call[1] and call[0] != "GET"]

    async def problem(self, route: Route, failure: tuple[int, str]) -> None:
        """既知拒否の status/code だけを UI 分類へ渡し、内部 detail の非表示も試す。"""
        status, code = failure
        await route.fulfill(
            status=status,
            json={
                "type": f"https://projectmind.local/problems/{code}",
                "title": "Fixture refusal",
                "status": status,
                "code": code,
                "detail": PRIVATE_DETAIL,
            },
        )

    async def respond(self, route: Route) -> None:
        """全面 mock 境界と原 Project/user/CSRF を守り、未定義操作は親へ拒否させる。"""
        request = route.request
        url = urlsplit(request.url)
        suffix = url.path.removeprefix(self.prefix)
        parts = suffix.split("/")
        if (
            f"{url.scheme}://{url.netloc}" != self.origin
            or not url.path.startswith(self.prefix)
            or len(parts) not in {3, 4}
            or parts[0] != "projects"
            or parts[2] != "members"
        ):
            await super().respond(route)
            return
        assert self.role == "ADMIN", "USER requested membership administration"
        assert parts[1] in self.members, "Unknown membership Project"
        self.calls.append((request.method, suffix, parse_qs(url.query), request.post_data))
        if request.method == "GET" and len(parts) == 3:
            rows = [value.copy() for value in self.members[parts[1]].values()]
            gate, failure = self.member_read_gate, self.member_read_failure
            if gate:
                gate.received.set()
                await asyncio.wait_for(gate.release.wait(), 35)
            try:
                if gate and gate.failure:
                    failure = gate.failure
                if failure:
                    await self.problem(route, failure)
                else:
                    await route.fulfill(json={"items": rows})
            finally:
                if gate:
                    gate.returned.set()
            return
        assert len(parts) == 4 and request.method in {"PUT", "DELETE"}, "Unknown member operation"
        assert request.headers.get("x-csrf-token") == CSRF
        assert request.headers.get("origin") == self.origin
        assert request.post_data is None, "Membership request unexpectedly contains a body"
        target = parts[3].lower()
        assert target in self.users, "Unknown candidate"
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
            if request.method == "PUT":
                assert self.details[parts[1]]["status"] == "ACTIVE", "Added to archived project"
                assert self.users[target]["status"] == "ACTIVE", "Added a disabled account"
                self.members[parts[1]][target] = self.member(target, "ACTIVE")
            else:
                assert self.members[parts[1]][target]["status"] == "ACTIVE"
                self.members[parts[1]][target] = self.member(target, "REMOVED")
            if mode == "drop":
                await route.abort("failed")
            elif mode == "invalid":
                await route.fulfill(json={"user_id": target})
            elif request.method == "PUT":
                await route.fulfill(json=self.members[parts[1]][target])
            else:
                await route.fulfill(status=204)
        finally:
            if gate:
                gate.returned.set()


async def open_tab(page: Page) -> None:
    """実Project tabをkeyboardで開き、応答前の中間描画には依存しない。"""
    tab = page.locator('[data-project-tab="members"]')
    await expect(tab).to_be_visible()
    await tab.focus()
    await page.keyboard.press("Enter")


async def panel(page: Page, *, loaded: bool = True) -> Locator:
    """実 Project tab を keyboard で開き、公開 membership 取得の完了を待つ。"""
    await open_tab(page)
    result = page.locator("[data-project-members]")
    await expect(result).to_be_visible()
    if loaded:
        await expect(result.locator(f'[data-member-id="{ACTIVE}"]')).to_be_visible()
    return result


async def select_action(page: Page, user_id: str, action: str) -> None:
    """固定 identity の操作を選択し、選択だけでは書込を起こさない。"""
    row = (
        page.locator(f'[data-member-id="{user_id}"]')
        if action == "remove"
        else page.locator(f'[data-member-candidate="{user_id}"]')
    )
    await row.locator(f'[data-member-action="{action}"]').click()
    await expect(page.locator("[data-member-intent]")).to_be_visible()


async def double_confirm(page: Page) -> None:
    """同一 tick に確定を二回配送し、render 後 disabled だけに頼らない境界を試す。"""
    await page.locator("[data-member-confirm]").evaluate("""button => {
        for (let index = 0; index < 2; index++)
            button.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
    }""")


async def basic_flow(page: Page, api: MembersApi, labels: dict) -> None:
    """一覧の二状態・候補条件・再追加/除外・未知ではない成功を三語で辿る。"""
    area = await panel(page)
    await expect(area.locator(f'[data-member-id="{REMOVED}"]')).to_be_visible()
    await expect(
        area.locator(f'[data-member-candidate="{DISABLED}"] [data-member-action="add"]')
    ).to_be_disabled()
    await expect(area.locator(f'[data-member-candidate="{ADMIN}"]')).to_contain_text(
        labels["account"]["roles"]["ADMIN"]
    )
    await expect(area).to_contain_text(labels["projectMembers"]["adminBypass"])
    await expect(area).to_contain_text(labels["projectMembers"]["relationshipHint"])
    for user_id, status in ((ACTIVE, "ACTIVE"), (REMOVED, "REMOVED")):
        await expect(area.locator(f'[data-member-id="{user_id}"]')).to_contain_text(
            labels["projectMembers"]["states"][status]
        )
    assert any(call[1] == "users" and call[2].get("limit") == ["25"] for call in api.calls)
    await select_action(page, CANDIDATE, "add")
    await expect(page.locator("[data-member-intent] h3")).to_be_focused()
    assert not api.member_mutations()
    await page.locator("[data-member-cancel]").click()
    assert not api.member_mutations()
    await select_action(page, CANDIDATE, "add")
    api.write_gate = ResponseGate()
    await double_confirm(page)
    await asyncio.wait_for(api.write_gate.received.wait(), 10)
    assert len(api.member_mutations()) == 1
    api.write_gate.release.set()
    await expect(area.locator(f'[data-member-id="{CANDIDATE}"]')).to_be_visible()
    await select_action(page, CANDIDATE, "remove")
    await double_confirm(page)
    await expect(
        area.locator(f'[data-member-id="{CANDIDATE}"] [data-member-action="remove"]')
    ).to_have_count(0)
    assert len(api.member_mutations()) == 2
    await select_action(page, REMOVED, "add")
    await double_confirm(page)
    await expect(
        area.locator(f'[data-member-id="{REMOVED}"] [data-member-action="remove"]')
    ).to_be_enabled()
    assert len(api.member_mutations()) == 3


async def search_paging(page: Page, api: MembersApi, labels: dict) -> None:
    """候補後半を server offset で取り、検索を URL/storage に移さない。"""
    area = await panel(page)
    await area.get_by_role("button", name=labels["account"]["next"], exact=True).click()
    await expect(area.locator("[data-member-candidate]")).to_have_count(5)
    assert any(call[1] == "users" and call[2].get("offset") == ["25"] for call in api.calls)
    await area.locator("[data-member-search] input[name=q]").fill("person27")
    await area.locator("[data-member-search]").evaluate("form => form.requestSubmit()")
    await expect(area.locator("[data-member-candidate]")).to_have_count(1)
    assert any(
        call[1] == "users" and call[2].get("q") == ["person27"] and call[2].get("offset") == ["0"]
        for call in api.calls
    )
    assert "person27" not in page.url
    assert "person27" not in await page.evaluate(
        "JSON.stringify({...localStorage, ...sessionStorage})"
    )


async def ordinary_user(page: Page, api: MembersApi, _: dict) -> None:
    """USERに管理入口・候補・membership読取を一切発行しない。"""
    await selected(page, PROJECT)
    await expect(page.locator("main .pageHeader")).to_be_visible()
    await settle(page)
    await expect(page.locator('[data-project-tab="members"]')).to_have_count(0)
    assert not any("/members" in call[1] or call[1] == "users" for call in api.calls)


async def archived_members(page: Page, api: MembersApi, _: dict) -> None:
    """アーカイブ後も関係の読取/除外は許すが、新規/再追加の候補は読まない。"""
    await panel(page)
    assert not any(call[1] == "users" for call in api.calls)
    for button in await page.locator('[data-member-action="add"]').all():
        await expect(button).to_be_disabled()
    await select_action(page, ACTIVE, "remove")
    await double_confirm(page)
    await expect(
        page.locator(f'[data-member-id="{ACTIVE}"] [data-member-action="remove"]')
    ).to_have_count(0)
    assert len(api.member_mutations()) == 1
    assert api.member_mutations()[0][1] == f"projects/{ARCHIVED}/members/{ACTIVE}"


async def refusal(page: Page, api: MembersApi, _: dict) -> None:
    """既知拒否は内部detailやunknownへ変換せず、401だけ現会話を閉じる。"""
    await panel(page)
    await select_action(page, CANDIDATE, "add")
    await double_confirm(page)
    if api.write_failure and api.write_failure[0] == 401:
        await expect(page.locator('input[name="email"]')).to_be_visible()
    else:
        await expect(page.locator("[data-project-members] [role=alert]")).to_be_visible()
        await expect(page.locator("[data-member-unknown]")).to_have_count(0)
    assert len(api.member_mutations()) == 1


async def read_refusal(page: Page, api: MembersApi, labels: dict) -> None:
    """一覧拒否は古い関係を現在と扱わず、元Projectへの明示再読取だけで復帰する。"""
    if api.member_read_failure and api.member_read_failure[0] == 401:
        await open_tab(page)
        await expect(page.locator('input[name="email"]')).to_be_visible()
    else:
        await panel(page, loaded=False)
        area = page.locator("[data-project-members]")
        await expect(area.get_by_role("alert")).to_be_visible()
        await expect(area.locator("[data-member-id]")).to_have_count(0)
        await expect(area.locator(f'[data-member-candidate="{CANDIDATE}"]')).to_be_visible()
        await expect(
            area.locator(f'[data-member-candidate="{CANDIDATE}"] [data-member-action="add"]')
        ).to_be_disabled()
        await expect(area.get_by_role("alert")).to_be_focused()
        api.member_read_failure = None
        await area.locator("[data-member-refresh]").click()
        await expect(area.locator(f'[data-member-id="{ACTIVE}"]')).to_be_visible()
        await expect(
            area.locator(f'[data-member-candidate="{CANDIDATE}"] [data-member-action="add"]')
        ).to_be_enabled()
    assert not api.member_mutations()


async def pending_read(page: Page, api: MembersApi, _: dict) -> None:
    """候補だけ先に届いても、関係一覧を検証できるまで選択・送信を許可しない。"""
    area = await panel(page, loaded=False)
    gate = api.member_read_gate
    assert gate is not None
    await asyncio.wait_for(gate.received.wait(), 10)
    await expect(area.locator(f'[data-member-candidate="{CANDIDATE}"]')).to_be_visible()
    await expect(
        area.locator(f'[data-member-candidate="{CANDIDATE}"] [data-member-action="add"]')
    ).to_be_disabled()
    await expect(area.locator("[data-member-id]")).to_have_count(0)
    assert not api.member_mutations()
    gate.release.set()
    await expect(area.locator(f'[data-member-id="{ACTIVE}"]')).to_be_visible()
    await expect(
        area.locator(f'[data-member-candidate="{CANDIDATE}"] [data-member-action="add"]')
    ).to_be_enabled()


async def uppercase_relationship(page: Page, api: MembersApi, labels: dict) -> None:
    """UUIDの大小文字差を別会員と見なさず、候補の重複追加と未知照合の誤判定を防ぐ。"""
    account = {**api.users[CANDIDATE], "user_id": LETTER_ID, "email": "uppercase@example.com"}
    api.users[LETTER_ID] = account
    api.users[LETTER_ID.upper()] = account
    api.directory.insert(0, LETTER_ID)
    api.members[PROJECT][LETTER_ID] = {
        **api.member(LETTER_ID, "ACTIVE"),
        "user_id": LETTER_ID.upper(),
    }
    await panel(page)
    await expect(
        page.locator(f'[data-member-candidate="{LETTER_ID}"] [data-member-action="add"]')
    ).to_be_disabled()
    await select_action(page, LETTER_ID.upper(), "remove")
    await double_confirm(page)
    unknown = page.locator("[data-member-unknown]")
    await expect(unknown).to_be_visible()
    await unknown.locator("[data-member-reconcile]").click()
    await expect(unknown.locator("[data-member-acknowledged]")).to_be_enabled()
    await expect(unknown.locator("[data-member-reviewed]")).to_contain_text(
        labels["projectMembers"]["states"]["REMOVED"]
    )
    assert len(api.member_mutations()) == 1


async def unknown_submission(page: Page, api: MembersApi, labels: dict) -> None:
    """喪失/500/不正成功/期限は自動再送せず、原2事実の読取後に人が新操作を許可する。"""
    await panel(page)
    await select_action(page, CANDIDATE, "add")
    if api.write_mode == "timeout":
        api.write_gate = ResponseGate()
    await double_confirm(page)
    if api.write_mode == "timeout":
        await asyncio.wait_for(api.write_gate.received.wait(), 10)
        await page.evaluate("""() => {
            const original = performance.now.bind(performance);
            performance.now = () => original() + 30001;
        }""")
        api.write_gate.release.set()
    await expect(page.locator("[data-member-unknown]")).to_be_visible()
    assert len(api.member_mutations()) == 1
    for button in await page.locator("[data-member-confirm]").all():
        await expect(button).to_be_disabled()
    before = len(api.calls)
    await page.locator("[data-member-reconcile]").click()
    await expect(page.locator("[data-member-acknowledged]")).to_be_enabled()
    reads = api.calls[before:]
    assert any(call[:2] == ("GET", f"projects/{PROJECT}/members") for call in reads)
    assert any(call[:2] == ("GET", f"users/{CANDIDATE}") for call in reads)
    await expect(page.locator("[data-member-acknowledge]")).to_be_disabled()
    await page.locator("[data-member-acknowledged]").check()
    await page.locator("[data-member-acknowledge]").click()
    await expect(page.locator("[data-member-unknown]")).to_have_count(0)
    assert len(api.member_mutations()) == 1
    api.write_mode = "success"
    api.write_failure = None
    await select_action(page, CANDIDATE, "remove" if CANDIDATE in api.members[PROJECT] else "add")
    assert len(api.member_mutations()) == 1
    await double_confirm(page)
    await expect(page.locator("[data-member-intent]")).to_have_count(0)
    assert len(api.member_mutations()) == 2


async def failed_review(page: Page, api: MembersApi, labels: dict, fact: str) -> None:
    """片方だけ読めた状態や前回の facts では人の確認を受け付けない。"""
    await panel(page)
    await select_action(page, CANDIDATE, "add")
    await double_confirm(page)
    unknown = page.locator("[data-member-unknown]")
    await expect(unknown).to_be_visible()
    failure = (500, "server_error")
    gate = ResponseGate()
    gate.failure = failure
    gate.release.set()
    if fact == "members":
        api.member_read_gate = gate
    else:
        api.gates[("GET", f"users/{CANDIDATE}")] = gate
    await unknown.locator("[data-member-reconcile]").click()
    await expect(unknown.get_by_role("alert")).to_have_text(
        labels["projectMembers"]["failures"]["loadFailed"]
    )
    await expect(unknown.locator("[data-member-acknowledged]")).to_have_count(0)
    assert len(api.member_mutations()) == 1
    gate.failure = None
    before = len(api.calls)
    await unknown.locator("[data-member-reconcile]").click()
    await expect(unknown.locator("[data-member-acknowledged]")).to_be_enabled()
    reads = api.calls[before:]
    assert any(call[:2] == ("GET", f"projects/{PROJECT}/members") for call in reads)
    assert any(call[:2] == ("GET", f"users/{CANDIDATE}") for call in reads)
    await unknown.locator("[data-member-acknowledged]").check()
    await unknown.locator("[data-member-acknowledge]").click()
    await expect(unknown).to_have_count(0)
    assert len(api.member_mutations()) == 1


async def remove_unknown(page: Page, api: MembersApi, labels: dict) -> None:
    """除外の応答喪失でも元ACTIVE関係を残し、現在REMOVEDを元操作の成功と呼ばない。"""
    await panel(page)
    await select_action(page, ACTIVE, "remove")
    await double_confirm(page)
    unknown = page.locator("[data-member-unknown]")
    await expect(unknown).to_be_visible()
    await expect(unknown).to_contain_text(labels["projectMembers"]["states"]["ACTIVE"])
    await unknown.locator("[data-member-reconcile]").click()
    await expect(unknown.locator("[data-member-acknowledged]")).to_be_enabled()
    await expect(unknown.locator("[data-member-reviewed]")).to_contain_text(
        labels["projectMembers"]["states"]["REMOVED"]
    )
    await expect(unknown.locator("[data-member-acknowledge]")).to_be_disabled()
    assert len(api.member_mutations()) == 1


async def pending_timeout(page: Page, api: MembersApi, _: dict) -> None:
    """応答を全く返さないwriteは実timerの30秒で未知となり、遅い成功も採用しない。"""
    await panel(page)
    await page.clock.install()
    await select_action(page, CANDIDATE, "add")
    gate = ResponseGate()
    api.write_gate = gate
    await double_confirm(page)
    await asyncio.wait_for(gate.received.wait(), 10)
    await page.clock.fast_forward(30_001)
    await expect(page.locator("[data-member-unknown]")).to_be_visible()
    assert len(api.member_mutations()) == 1
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 10)
    await settle(page)
    await expect(page.locator("[data-member-unknown]")).to_be_visible()
    await expect(page.locator(f'[data-member-id="{CANDIDATE}"]')).to_have_count(0)
    assert len(api.member_mutations()) == 1


async def tabs_preserve_unknown(page: Page, api: MembersApi, labels: dict) -> None:
    """同じProjectのtab移動をunknown解除や新しい意図の作成と取り違えない。"""
    await panel(page)
    await select_action(page, CANDIDATE, "add")
    await double_confirm(page)
    await expect(page.locator("[data-member-unknown]")).to_be_visible()
    await page.locator("[data-member-refresh]").click()
    await expect(page.locator(f'[data-member-id="{CANDIDATE}"]')).to_be_visible()
    await expect(page.locator("[data-member-unknown]")).to_be_visible()
    await expect(
        page.locator(f'[data-member-id="{CANDIDATE}"] [data-member-action="remove"]')
    ).to_be_disabled()
    await page.locator('[data-project-tab="projects"]').click()
    await expect(page.locator("[data-project-members]")).to_be_hidden()
    await page.locator('[data-project-tab="members"]').click()
    await expect(page.locator("[data-member-unknown]")).to_be_visible()
    assert len(api.member_mutations()) == 1


async def late_read(page: Page, api: MembersApi, _: dict, failure: bool) -> None:
    """Aの古い一覧/401はBやA→B→Aの新しい一覧・現在sessionに作用しない。"""
    api.members[PROJECT][CANDIDATE] = api.member(CANDIDATE, "ACTIVE")
    gate = ResponseGate()
    if failure:
        gate.failure = (401, "session_expired")
    api.member_read_gate = gate
    await panel(page, loaded=False)
    await asyncio.wait_for(gate.received.wait(), 10)
    api.member_read_gate = None
    del api.members[PROJECT][CANDIDATE]
    for project_id in (NEXT_PROJECT, PROJECT):
        await menu(page)
        await page.locator(".sideNavProject select").select_option(project_id)
        await selected(page, project_id)
        await panel(page)
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 10)
    await settle(page)
    await expect(page.locator(f'[data-member-id="{CANDIDATE}"]')).to_have_count(0)
    await expect(page.locator('input[name="email"]')).to_have_count(0)
    await expect(page.locator("[data-project-members] [role=alert]")).to_have_count(0)
    assert not api.member_mutations()


async def late_project(page: Page, api: MembersApi, _: dict, returning: bool) -> None:
    """遅い元書込は次Project、またはA→B→Aの新しいAのstateを更新しない。"""
    await panel(page)
    await select_action(page, CANDIDATE, "add")
    gate = ResponseGate()
    api.write_gate = gate
    await double_confirm(page)
    await asyncio.wait_for(gate.received.wait(), 10)
    await menu(page)
    await page.locator(".sideNavProject select").select_option(NEXT_PROJECT)
    await selected(page, NEXT_PROJECT)
    await panel(page)
    if returning:
        await menu(page)
        await page.locator(".sideNavProject select").select_option(PROJECT)
        await selected(page, PROJECT)
        await panel(page)
    await expect(page.locator(f'[data-member-id="{CANDIDATE}"]')).to_have_count(0)
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 10)
    await settle(page)
    await expect(page.locator(f'[data-member-id="{CANDIDATE}"]')).to_have_count(0)
    await expect(page.locator("[data-member-intent], [data-member-unknown]")).to_have_count(0)
    assert len(api.member_mutations()) == 1


async def late_actor(page: Page, api: MembersApi, labels: dict, status: int) -> None:
    """別actorで再ログインした後に旧401/成功が届いても、新会話を変更しない。"""
    await panel(page)
    await select_action(page, CANDIDATE, "add")
    gate = ResponseGate()
    if status != 200:
        gate.failure = (status, "session_expired" if status == 401 else "server_error")
    api.write_gate = gate
    await double_confirm(page)
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


async def exercise(
    browser: Browser,
    url: str,
    name: str,
    action: Callable[[Page, MembersApi, dict], Awaitable[None]],
    *,
    role: str = "ADMIN",
    language: str = "en",
    width: int = 1440,
    project_id: str = PROJECT,
    setup: Callable[[MembersApi], None] | None = None,
    output: Path | None = None,
) -> None:
    """各caseに実Appを一度だけmountし、未知通信/JS/秘密/横溢れを必ず検査する。"""
    api = MembersApi(url, language, role)
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
        await page.goto(f"{url}#/projects?project={project_id}")
        await action(page, api, await messages(page, language))
        await settle(page)
        await privacy(page)
        assert PRIVATE_DETAIL not in await page.locator("body").inner_text()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (
            "Page overflow"
        )
        assert not api.unexpected, api.unexpected
        assert not api.failures, api.failures
        assert not errors, errors
        if output:
            await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
            if name.startswith("members-"):
                await page.evaluate("window.scrollTo(0, 0)")
                await page.screenshot(path=str(output / f"{name}-viewport.png"))
        print(f"PASS {name}", flush=True)
    except Exception:
        if output:
            await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        print("Fixture failures:", api.unexpected, api.failures, errors, flush=True)
        raise
    finally:
        for gate in [api.write_gate, api.member_read_gate, *api.gates.values()]:
            if gate:
                gate.release.set()
        api.release.set()
        await context.close()


async def check(url: str, output: Path | None, only: str | None) -> None:
    """専用loopback fixtureだけを許し、絞り込み空集合を成功として報告しない。"""
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

        async def run(name: str, action: Callable, **kwargs) -> None:
            """case名で選択し、実完了した検査だけを数える。"""
            nonlocal count
            if only is None or only in name:
                await exercise(browser, url, name, action, output=output, **kwargs)
                count += 1

        try:
            for language in ("zh", "ja", "en"):
                for width in (390, 1440):
                    await run(
                        f"members-{language}-{width}", basic_flow, language=language, width=width
                    )
            await run("search-paging", search_paging)
            await run("ordinary-user", ordinary_user, role="USER", width=390)
            await run("archived-members", archived_members, project_id=ARCHIVED)
            await run(
                "uppercase-relationship",
                uppercase_relationship,
                setup=lambda api: setattr(api, "write_mode", "drop"),
            )
            await run(
                "pending-member-read",
                pending_read,
                setup=lambda api: setattr(api, "member_read_gate", ResponseGate()),
            )
            for status, code in (
                (401, "session_expired"),
                (403, "administrator_required"),
                (404, "project_not_found"),
                (500, "server_error"),
            ):
                await run(
                    f"read-refusal-{status}",
                    read_refusal,
                    setup=lambda api, status=status, code=code: setattr(
                        api, "member_read_failure", (status, code)
                    ),
                )
            for status, code in (
                (401, "session_expired"),
                (403, "administrator_required"),
                (404, "project_not_found"),
                (422, "validation_error"),
            ):
                await run(
                    f"refusal-{status}",
                    refusal,
                    setup=lambda api, status=status, code=code: setattr(
                        api, "write_failure", (status, code)
                    ),
                )
            for mode in ("drop", "invalid", "timeout", "500"):

                def unknown(api: MembersApi, mode: str = mode) -> None:
                    """喪失と既知拒否のない500を、異なる公開結果として固定する。"""
                    if mode == "500":
                        api.write_failure = (500, "server_error")
                    else:
                        api.write_mode = mode

                await run(f"unknown-{mode}", unknown_submission, setup=unknown)
            await run(
                "tabs-preserve-unknown",
                tabs_preserve_unknown,
                setup=lambda api: setattr(api, "write_mode", "drop"),
            )
            for fact in ("members", "account"):

                async def review(
                    page: Page, api: MembersApi, labels: dict, fact: str = fact
                ) -> None:
                    """照合時の独立した失敗側をcaseごとに固定する。"""
                    await failed_review(page, api, labels, fact)

                await run(
                    f"review-failure-{fact}",
                    review,
                    setup=lambda api: setattr(api, "write_mode", "drop"),
                )
            await run(
                "remove-unknown",
                remove_unknown,
                setup=lambda api: setattr(api, "write_mode", "drop"),
            )
            await run("pending-timeout", pending_timeout)
            for failure in (False, True):

                async def stale_read(
                    page: Page, api: MembersApi, labels: dict, failure: bool = failure
                ) -> None:
                    """過去一覧の成功と失効を別々に配送する。"""
                    await late_read(page, api, labels, failure)

                await run(f"late-read-{401 if failure else 200}", stale_read)
            for returning in (False, True):

                async def late(
                    page: Page, api: MembersApi, labels: dict, returning: bool = returning
                ) -> None:
                    """往復と片道の文脈変更を独立caseに束縛する。"""
                    await late_project(page, api, labels, returning)

                await run(f"late-project-{returning}", late)
                await run(
                    f"late-project-500-{returning}",
                    late,
                    setup=lambda api: setattr(api, "write_failure", (500, "server_error")),
                )
            for status in (200, 401, 500):

                async def actor(
                    page: Page, api: MembersApi, labels: dict, status: int = status
                ) -> None:
                    """旧actorの既知失効・未知拒否・成功を現在actorから隔離する。"""
                    await late_actor(page, api, labels, status)

                await run(f"late-actor-{status}", actor)
        finally:
            await browser.close()
    if not count:
        raise ValueError("Case filter did not match any check")
    print(f"Project members browser checks passed: {count}; mock API only, no real DB/auth proof.")


def main() -> None:
    """既存projects入口、workspace外screenshot出力と任意の単caseを受け取る。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--only")
    arguments = parser.parse_args()
    asyncio.run(check(arguments.url, arguments.output, arguments.only))


if __name__ == "__main__":
    main()
