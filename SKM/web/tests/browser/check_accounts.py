"""実 AccountsPage と App の境界を loopback Vite・全面 mock API で検証する。"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.async_api import Browser, Error, Locator, Page, Route, async_playwright, expect

ACTOR = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000003"
TARGET = "00000000-0000-4000-8000-000000000100"
CSRF = "c" * 32
PASSWORD = "browser-only new passphrase"
CURRENT_PASSWORD = "browser-only current passphrase"
NOW = "2026-09-09T00:00:00Z"
PROJECT = "00000000-0000-4000-8000-000000000020"
NEXT_PROJECT = "00000000-0000-4000-8000-000000000021"


class ResponseGate:
    """HTTP 応答を既知の handle で止め、次の会話が成立してから解放する。"""

    def __init__(self) -> None:
        """case 内だけで共有する到達/解放/完了と拒否を保持する。"""
        self.received = asyncio.Event()
        self.release = asyncio.Event()
        self.returned = asyncio.Event()
        self.failure: tuple[int, str] | None = None


def project(project_id: str) -> dict:
    """実 Project を操作せず sidebar の選択と module lifecycle を再現する。"""
    return {
        "project_id": project_id,
        "key": f"fixture-{project_id[-12:]}",
        "name": f"Browser project {project_id[-2:]}",
        "description": "Browser fixture",
        "status": "ACTIVE",
        "settings": {},
        "retention_days": 30,
        "row_version": 7,
        "created_at": NOW,
        "updated_at": NOW,
    }


def account(user_id: str, index: int = 0, role: str = "USER") -> dict:
    """許可済み公開 field だけを持つ架空アカウントを返す。"""

    return {
        "user_id": user_id,
        "email": "reader@example.com" if user_id == ACTOR else f"person{index:02}@example.com",
        "display_name": "Browser reader" if user_id == ACTOR else f"Browser person {index:02}",
        "system_role": role,
        "status": "ACTIVE",
        "row_version": 7,
        "created_at": NOW,
        "updated_at": NOW,
    }


def session(user_id: str = ACTOR, role: str = "USER") -> dict:
    """実 token を使わず、App と component に同じ会話 identity を渡す。"""

    user = account(user_id, index=99 if user_id == OTHER else 0, role=role)
    return {
        "user": {
            **{key: user[key] for key in ("user_id", "email", "display_name", "system_role")},
            "organization_id": "00000000-0000-4000-8000-000000000002",
        },
        "csrf_token": CSRF,
        "absolute_expires_at": "2099-01-01T00:00:00Z",
    }


class AccountsApi:
    """UI の観測可能な応答だけを模擬し、実 transaction の証明には用いない。"""

    def __init__(self, url: str, role: str = "USER", language: str = "en") -> None:
        """case ごとに account、応答喪失、待機 gate を独立させる。"""

        address = urlsplit(url)
        self.origin = f"{address.scheme}://{address.netloc}"
        self.prefix = address.path.removesuffix("tests/browser/accounts.html") + "api/v1/"
        self.role = role
        self.language = language
        self.actor = ACTOR
        self.users = {ACTOR: account(ACTOR, role=role), OTHER: account(OTHER, 99)}
        self.directory = [f"00000000-0000-4000-8000-{100 + index:012}" for index in range(30)]
        self.users.update(
            {user_id: account(user_id, index) for index, user_id in enumerate(self.directory)}
        )
        self.calls: list[tuple[str, str, dict, object]] = []
        self.unexpected: list[str] = []
        self.failures: list[str] = []
        self.failure: tuple[int, str] | None = None
        self.hold = False
        self.retry_seconds = 1
        self.drop = False
        self.received = asyncio.Event()
        self.release = asyncio.Event()
        self.returned = asyncio.Event()
        self.projects: list[dict] = []
        self.preference: str | None = None
        self.gates: dict[tuple[str, str], ResponseGate] = {}

    def mutations(self, suffix: str | None = None) -> list[tuple[str, str, dict, object]]:
        """preference 保存と区別し、対象 account mutation の回数を数える。"""

        return [
            call
            for call in self.calls
            if call[0] != "GET"
            and call[1].startswith("users")
            and not call[1].endswith(("preference", "ui-language"))
            and (suffix is None or call[1] == suffix)
        ]

    async def route(self, route: Route) -> None:
        """不明な API/外部 HTTP を遮断し、fixture handler の失敗も最後に報告する。"""

        try:
            await self.respond(route)
        except Exception as error:
            # Route task の例外を握り潰して UI の unknown 成功扱いにしない。
            self.failures.append(f"{type(error).__name__}: {error}")
            await route.abort()

    async def respond(self, route: Route) -> None:
        """local source 資産だけ通過させ、公開 API の形と CSRF を照合する。"""

        request = route.request
        url = urlsplit(request.url)
        if f"{url.scheme}://{url.netloc}" != self.origin:
            self.unexpected.append(request.url)
            await route.abort()
            return
        if not url.path.startswith((self.prefix, "/api/", self.prefix.removesuffix("v1/"))):
            await route.continue_()
            return
        suffix = url.path.removeprefix(self.prefix)
        query = parse_qs(url.query)
        body = request.post_data_json if request.post_data else None
        method = request.method
        self.calls.append((method, suffix, query, body))
        if method != "GET":
            assert request.headers.get("x-csrf-token") == CSRF, "Missing current CSRF"
            assert request.headers.get("origin") == self.origin, "Wrong Origin"
        result: object
        if suffix == "auth/session" and method == "GET":
            result = session(self.actor, self.role)
        elif suffix == "auth/login-context" and method == "GET":
            result = {"csrf_token": CSRF, "expires_in_seconds": 300}
        elif suffix == "auth/login" and method == "POST":
            assert body == {"email": self.users[self.actor]["email"], "password": PASSWORD}
            result = session(self.actor, self.role)
        elif suffix == "auth/logout" and method == "POST":
            result = {}
        elif suffix == "meta" and method == "GET":
            result = {
                key: "browser fixture" for key in ("name", "version", "phase", "task", "ingress")
            }
        elif suffix == "projects" and method == "GET":
            result = {"items": self.projects.copy()}
        elif suffix.startswith("projects/") and suffix.count("/") == 1 and method == "GET":
            target = suffix.split("/")[1]
            matches = [item for item in self.projects if item["project_id"] == target]
            assert len(matches) == 1, "Requested project is not an authorized fixture"
            result = matches[0].copy()
        elif suffix.endswith("/modules") and method == "GET":
            assert suffix in {f"projects/{PROJECT}/modules", f"projects/{NEXT_PROJECT}/modules"}
            result = {"modules": []}
        elif (
            suffix in {f"projects/{PROJECT}/runs", f"projects/{NEXT_PROJECT}/runs"}
            and method == "GET"
        ):
            result = {
                "items": [],
                "total": 0,
                "limit": int(query["limit"][0]),
                "offset": int(query["offset"][0]),
            }
        elif suffix == "users/me/project-preference" and method in {"GET", "PUT"}:
            result = {"project_id": self.preference if method == "GET" else body["project_id"]}
        elif suffix == "users/me/ui-language" and method in {"GET", "PUT"}:
            result = {"ui_language": self.language if method == "GET" else body["ui_language"]}
        elif suffix == "users/me/account" and method == "GET":
            result = self.users[self.actor].copy()
        elif suffix == "users" and method == "GET":
            assert self.role == "ADMIN", "USER requested an ADMIN list"
            limit, offset = int(query["limit"][0]), int(query["offset"][0])
            q = query.get("q", [""])[0].lower()
            items = [self.users[user_id].copy() for user_id in self.directory]
            items = [
                item for item in items if q in f"{item['email']} {item['display_name']}".lower()
            ]
            result = {
                "items": items[offset : offset + limit],
                "total": len(items),
                "limit": limit,
                "offset": offset,
            }
        elif suffix.endswith("/security-events") and method == "GET":
            target = self.actor if suffix == "users/me/security-events" else suffix.split("/")[1]
            assert target == self.actor or self.role == "ADMIN"
            limit, offset = int(query["limit"][0]), int(query["offset"][0])
            items = [
                {
                    "event_id": f"00000000-0000-4000-8000-{index + 1000:012}",
                    "user_id": target,
                    "actor_id": ACTOR,
                    "action": "UPDATED",
                    "row_version": index + 1,
                    "previous_role": "USER",
                    "previous_status": "ACTIVE",
                    "system_role": "USER",
                    "status": "ACTIVE",
                    "revoked_sessions": 0,
                    "request_id": f"00000000-0000-4000-8000-{index + 2000:012}",
                    "created_at": NOW,
                }
                for index in range(30)
            ]
            result = {
                "items": items[offset : offset + limit],
                "total": 30,
                "limit": limit,
                "offset": offset,
            }
        elif suffix.startswith("users/") and method == "GET" and suffix.count("/") == 1:
            assert self.role == "ADMIN"
            result = self.users[suffix.split("/")[1]].copy()
        elif (suffix == "users" and method == "POST") or (
            suffix.startswith("users/") and method in {"POST", "PUT"}
        ):
            await self.mutate(route, suffix, body)
            return
        else:
            self.unexpected.append(f"{method} {request.url}")
            await route.abort()
            return
        gate = self.gates.get((method, suffix))
        if gate:
            gate.received.set()
            await asyncio.wait_for(gate.release.wait(), 30)
        try:
            if gate and gate.failure:
                status, code = gate.failure
                await route.fulfill(
                    status=status,
                    json={
                        "type": f"https://skillmind.local/problems/{code}",
                        "title": "Late fixture rejection",
                        "status": status,
                        "code": code,
                        "detail": PASSWORD,
                    },
                )
            else:
                await route.fulfill(status=200, headers={"Cache-Control": "no-store"}, json=result)
        finally:
            if gate:
                gate.returned.set()

    async def mutate(self, route: Route, suffix: str, body: dict) -> None:
        """原版を記録し、既知拒否・応答喪失・遅延を case が明示的に選ぶ。"""

        original_actor = self.actor
        hold, drop, failure = self.hold, self.drop, self.failure
        self.received.set()
        if hold:
            await asyncio.wait_for(self.release.wait(), 20)
        try:
            if drop:
                await route.abort("failed")
                return
            if failure:
                status, code = failure
                await route.fulfill(
                    status=status,
                    headers={
                        "Content-Type": "application/problem+json",
                        "Retry-After": str(self.retry_seconds),
                        "Cache-Control": "no-store",
                    },
                    json={
                        "type": f"https://skillmind.local/problems/{code}",
                        "title": "Fixture rejection",
                        "status": status,
                        "code": code,
                        "detail": PASSWORD,
                        "request_id": "00000000-0000-4000-8000-000000009999",
                    },
                )
                return
            if suffix == "users":
                assert self.role == "ADMIN"
                assert set(body) == {"email", "display_name", "system_role", "password"}
                assert body["password"] == PASSWORD
                target = "00000000-0000-4000-8000-000000000900"
                user = account(target)
                user.update({key: body[key] for key in ("email", "display_name", "system_role")})
                user["row_version"] = 1
                self.directory.append(target)
                security_change = False
            else:
                target = original_actor if suffix.startswith("users/me/") else suffix.split("/")[1]
                assert target == original_actor or self.role == "ADMIN"
                user = self.users[target].copy()
                security_change = route.request.method != "PUT" or (
                    body.get("system_role") != user["system_role"]
                    or body.get("status") != user["status"]
                )
                assert body["expected_row_version"] == user["row_version"], "Wrong original version"
                if suffix.endswith("/password"):
                    assert set(body) == {"current_password", "new_password", "expected_row_version"}
                    assert (
                        body["current_password"] == CURRENT_PASSWORD
                        and body["new_password"] == PASSWORD
                    )
                elif route.request.method == "PUT":
                    assert set(body) == {
                        "display_name",
                        "system_role",
                        "status",
                        "expected_row_version",
                    }
                    user.update(
                        {key: body[key] for key in ("display_name", "system_role", "status")}
                    )
                else:
                    assert set(body) == {"expected_row_version"}
                user["row_version"] += 1
            self.users[target] = user
            await route.fulfill(
                status=201 if suffix == "users" else 200,
                headers={"Cache-Control": "no-store"},
                json={
                    "user": user,
                    "revoked_sessions": 2 if security_change else 0,
                    "session_revoked": target == original_actor and security_change,
                },
            )
        except Error:
            # Native abort 済みの場合も遅い callback が発生しないことを別途観測する。
            if not hold:
                raise
        finally:
            self.returned.set()


async def settle(page: Page) -> None:
    """React の描画と microtask を待ち、固定秒数の sleep を使わない。"""

    await page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )


async def catalog(page: Page, language: str = "en") -> dict:
    """実 catalog の現在値を読み、翻訳を fixture 内へ複製しない。"""

    return await page.evaluate(
        "async language => (await import('../../src/lib/i18n/messages.ts'))"
        ".MESSAGES[language].account",
        language,
    )


async def password_form(page: Page, messages: dict) -> Locator:
    """実 label から password を入力し、送信回数と消去を後続 case で検証する。"""

    form = page.locator('[data-account-form="password"]')
    await expect(form.get_by_label(messages["currentPassword"], exact=True)).to_be_enabled()
    for field, value in (
        ("currentPassword", CURRENT_PASSWORD),
        ("newPassword", PASSWORD),
        ("confirmPassword", PASSWORD),
    ):
        await form.get_by_label(messages[field], exact=True).fill(value)
    return form


async def double_submit(form: Locator) -> None:
    """同じ event loop tick の二重 submit を送り、disabled 描画前の ref guard を試す。"""

    await form.evaluate(
        "form => { for (let i=0; i<2; i++) "
        "form.dispatchEvent(new Event('submit', {bubbles:true, cancelable:true})); }"
    )


async def privacy(page: Page) -> None:
    """password/token が URL・storage・可視文へ出ないことを各操作後に検査する。"""

    state = await page.evaluate(
        "() => JSON.stringify({url: location.href, local: {...localStorage}, "
        "session: {...sessionStorage}, text: document.body.innerText})"
    )
    for value in (PASSWORD, CURRENT_PASSWORD, CSRF):
        assert value not in state, "Sensitive fixture input escaped transient form memory"


async def layout(page: Page) -> None:
    """長い説明と UUID を含むページ全体・操作欄の横溢れを検査する。"""

    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (
        "Page overflow"
    )
    for element in await page.locator(
        ".accountNotice, .accountForm, .accountFacts, .accountPager"
    ).all():
        if await element.is_visible():
            assert await element.evaluate("e => e.scrollWidth <= e.clientWidth + 1"), (
                "Account element overflow"
            )


async def keyboard_scroll(page: Page, listing: Locator) -> None:
    """有限の一覧でもkeyboardから後半へ到達でき、内部scrollをmouse専用にしない。"""
    await expect(listing).to_be_visible()
    assert await listing.evaluate("e => e.scrollHeight > e.clientHeight + 1"), (
        "Missing bounded list"
    )
    await listing.focus()
    await expect(listing).to_be_focused()
    await page.keyboard.press("PageDown")
    await page.wait_for_function("e => e.scrollTop > 0", arg=await listing.element_handle())


async def choose_target(page: Page, messages: dict) -> Locator:
    """一覧行の実操作から精確 ID の editor を開き、一覧 object の流用を避ける。"""

    directory = page.locator("[data-account-directory]")
    await directory.get_by_role(
        "button",
        name=f"{messages['edit']}: person00@example.com",
        exact=True,
    ).click()
    editor = page.locator(f'[data-account-editor="{TARGET}"]')
    await expect(editor.locator('[data-account-form="edit"] input')).to_be_enabled()
    return editor


async def create_form(page: Page, messages: dict) -> Locator:
    """初期 password と明示的役割を入力し、Project membership を要求しない。"""

    form = page.locator('[data-account-form="create"]')
    await form.get_by_label(messages["fields"]["email"], exact=True).fill("new-account@example.com")
    await form.get_by_label(messages["fields"]["name"], exact=True).fill("Created browser account")
    await form.get_by_label(messages["fields"]["role"], exact=True).select_option("USER")
    await form.get_by_label(messages["initialPassword"], exact=True).fill(PASSWORD)
    await form.get_by_label(messages["confirmPassword"], exact=True).fill(PASSWORD)
    return form


async def exercise_case(
    browser: Browser,
    url: str,
    name: str,
    action: Callable[[Page, AccountsApi, dict], Awaitable[None]],
    *,
    role: str = "USER",
    language: str = "en",
    width: int = 1440,
    app: bool = False,
    ignore_abort: bool = False,
    output: Path | None = None,
    setup: Callable[[AccountsApi], None] | None = None,
) -> None:
    """独立 context の通信/JS/秘密検査を必ず実行し、case 終了時に route 待機も閉じる。"""

    api = AccountsApi(url, role, language)
    if setup is not None:
        setup(api)
    context = await browser.new_context(viewport={"width": width, "height": 1000}, locale=language)
    await context.route("**/*", api.route)
    errors: list[str] = []
    page = await context.new_page()
    page.on("pageerror", lambda error: errors.append(error.stack or str(error)))
    if ignore_abort:
        await page.add_init_script("""(() => {
          const original = window.fetch.bind(window);
          window.fetch = (input, init) => original(input,
            init ? {...init, signal: undefined} : init);
        })();""")
    parameters = {"role": role, "language": language}
    if app:
        parameters["app"] = "1"
    try:
        await page.goto(f"{url}?{urlencode(parameters)}#/accounts")
        messages = await catalog(page, language)
        await expect(
            page.get_by_role("heading", name=messages["myAccount"], exact=True)
        ).to_be_visible()
        if app and output is not None:
            await expect(page.locator('[data-account-form="password"] input').first).to_be_enabled()
            await expect(page.locator(".accountEvents > details")).to_be_attached()
            await settle(page)
            await page.screenshot(path=str(output / f"{name}-entry.png"), full_page=True)
        await action(page, api, messages)
        await settle(page)
        await privacy(page)
        await layout(page)
        assert not api.unexpected, api.unexpected
        assert not api.failures, api.failures
        assert not errors, errors
        if output is not None:
            await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
        print(f"PASS {name}", flush=True)
    except Exception:
        # 失敗時の実描画も残し、後から別版の成功 screenshot と取り違えない。
        if output is not None:
            await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        raise
    finally:
        api.release.set()
        for gate in api.gates.values():
            gate.release.set()
        await context.close()


async def own_refusal(page: Page, api: AccountsApi, messages: dict) -> None:
    """本人参照・誤 password・三語・keyboard・同期防重・監査の server page を検証する。"""

    assert (
        await page.locator(
            '[data-account-directory], [data-account-form="create"], [data-account-form="edit"]'
        ).count()
        == 0
    )
    api.failure = (400, "current_password_rejected")
    api.hold = True
    form = await password_form(page, messages)
    await form.get_by_label(messages["confirmPassword"], exact=True).focus()
    await page.keyboard.press("Tab")
    await expect(
        form.get_by_role("button", name=messages["changePassword"], exact=True)
    ).to_be_focused()
    await double_submit(form)
    await asyncio.wait_for(api.received.wait(), 10)
    assert len(api.mutations()) == 1
    await expect(form.locator('input[type="password"]').first).to_have_value("")
    api.release.set()
    alert = page.get_by_role("alert")
    await expect(alert).to_contain_text(messages["failures"]["passwordRejected"])
    await expect(alert).to_be_focused()
    await expect(page.locator('[data-testid="session-ended-count"]')).to_have_text("0")
    events = page.locator(".accountEvents")
    await events.locator(":scope > details > summary").click()
    # 日付のない時刻だけでは、別日の安全操作を人が区別できない。
    await expect(events.locator("time").first).to_contain_text("2026")
    await keyboard_scroll(page, events.locator(".accountEventList"))
    await events.get_by_role("button", name=messages["next"], exact=True).click()
    await expect(events.locator("li")).to_have_count(5)
    assert any(
        call[1] == "users/me/security-events" and call[2]["offset"] == ["25"] for call in api.calls
    )


async def self_revoke(page: Page, api: AccountsApi, messages: dict) -> None:
    """自己失効は LoginPage に戻るだけで、追加 logout や password の再送をしない。"""

    logout_count = len([call for call in api.calls if call[1] == "auth/logout"])
    await page.get_by_label(messages["confirmRevoke"], exact=True).check()
    await page.get_by_role("button", name=messages["revoke"], exact=True).click()
    await expect(page.locator('input[name="email"]')).to_be_visible()
    assert len(api.mutations("users/me/sessions/revoke")) == 1
    assert len([call for call in api.calls if call[1] == "auth/logout"]) == logout_count


async def admin_directory(page: Page, api: AccountsApi, messages: dict) -> None:
    """検索/一覧 page/作成/精確編集/役割状態変更を一つの実 UI 導線で辿る。"""

    directory = page.locator("[data-account-directory]")
    await keyboard_scroll(page, directory.locator(".accountUserList"))
    await directory.get_by_role("button", name=messages["next"], exact=True).click()
    await expect(directory.get_by_text("person29@example.com", exact=True)).to_be_visible()
    assert any(call[1] == "users" and call[2]["offset"] == ["25"] for call in api.calls)
    search = page.locator('[data-account-form="search"]')
    await search.locator("input").fill("person29@example.com")
    await search.get_by_role("button", name=messages["search"], exact=True).click()
    await expect(directory.get_by_text("person25@example.com", exact=True)).to_have_count(0)
    assert any(
        call[1] == "users"
        and call[2].get("q") == ["person29@example.com"]
        and call[2]["offset"] == ["0"]
        for call in api.calls
    )
    assert "person29" not in page.url
    await search.locator("input").fill("")
    await search.get_by_role("button", name=messages["search"], exact=True).click()
    editor = await choose_target(page, messages)
    form = editor.locator('[data-account-form="edit"]')
    await form.get_by_label(messages["fields"]["name"], exact=True).fill("Revised display name")
    await form.get_by_label(messages["fields"]["role"], exact=True).select_option("ADMIN")
    await form.get_by_label(messages["fields"]["status"], exact=True).select_option("DISABLED")
    await form.get_by_label(messages["confirmChange"], exact=True).check()
    await double_submit(form)
    await expect(editor.get_by_text(messages["mutationSuccess"], exact=False)).to_be_visible()
    assert len(api.mutations(f"users/{TARGET}")) == 1
    assert api.users[TARGET]["system_role"] == "ADMIN" and api.users[TARGET]["status"] == "DISABLED"
    creation = await create_form(page, messages)
    await double_submit(creation)
    await expect(page.get_by_text(messages["createdSuccess"], exact=True)).to_be_visible()
    assert len(api.mutations("users")) == 1
    await expect(creation.locator('input[type="password"]').first).to_have_value("")


async def version_conflict(page: Page, api: AccountsApi, messages: dict) -> None:
    """原版の草稿は保持し、精確読取だけでは再送せず明示採用後に新しい版を使う。"""

    editor = await choose_target(page, messages)
    form = editor.locator('[data-account-form="edit"]')
    await form.get_by_label(messages["fields"]["name"], exact=True).fill("Unsaved original draft")
    api.users[TARGET]["row_version"] = 8
    api.users[TARGET]["display_name"] = "New server facts"
    api.failure = (409, "user_version_conflict")
    await double_submit(form)
    await expect(editor.get_by_role("alert")).to_contain_text(
        messages["failures"]["versionConflict"]
    )
    await expect(editor.locator(".accountVersion")).to_contain_text("7")
    await expect(form.get_by_label(messages["fields"]["name"], exact=True)).to_have_value(
        "Unsaved original draft"
    )
    await expect(
        editor.get_by_role("button", name=messages["adoptLatest"], exact=True)
    ).to_be_enabled()
    await expect(form.get_by_role("button", name=messages["save"], exact=True)).to_be_disabled()
    assert len(api.mutations()) == 1
    assert api.mutations()[0][3]["expected_row_version"] == 7
    assert (
        len([call for call in api.calls if call[0] == "GET" and call[1] == f"users/{TARGET}"]) >= 2
    )
    await editor.get_by_role("button", name=messages["adoptLatest"], exact=True).click()
    await expect(editor.locator(".accountVersion")).to_contain_text("8")
    assert len(api.mutations()) == 1
    api.failure = None
    await form.get_by_role("button", name=messages["save"], exact=True).click()
    await expect(editor.get_by_text(messages["mutationSuccess"], exact=False)).to_be_visible()
    assert api.mutations()[-1][3]["expected_row_version"] == 8


async def unknown_submission(page: Page, api: AccountsApi, messages: dict) -> None:
    """応答喪失は rollback とみなさず、password は消去して read-only 確認だけを行う。"""

    api.drop = True
    form = await password_form(page, messages)
    await double_submit(form)
    await expect(page.get_by_role("alert")).to_contain_text(messages["failures"]["unknown"])
    await expect(
        page.get_by_role("button", name=messages["adoptLatest"], exact=True)
    ).to_be_enabled()
    await double_submit(form)
    await settle(page)
    assert len(api.mutations()) == 1
    await expect(
        form.get_by_role("button", name=messages["changePassword"], exact=True)
    ).to_be_disabled()
    await expect(form.locator('input[type="password"]').first).to_have_value("")
    await expect(page.locator('[data-testid="session-ended-count"]')).to_have_text("0")
    assert any(call[0] == "GET" and call[1] == "users/me/account" for call in api.calls)


async def creation_unknown(page: Page, api: AccountsApi, messages: dict) -> None:
    """同 email の読取は成功証明にせず、新規作成の明示判断まで POST を封鎖する。"""

    api.drop = True
    form = await create_form(page, messages)
    await double_submit(form)
    await expect(page.get_by_role("alert")).to_contain_text(messages["failures"]["unknown"])
    await double_submit(form)
    await page.get_by_role("button", name=messages["checkCreation"], exact=True).click()
    await expect(
        page.get_by_role("button", name=messages["beginNewCreate"], exact=True)
    ).to_be_enabled()
    assert len(api.mutations("users")) == 1
    assert any(
        call[1] == "users" and call[2].get("q") == ["new-account@example.com"] for call in api.calls
    )
    assert "new-account" not in page.url
    await page.get_by_role("button", name=messages["beginNewCreate"], exact=True).click()
    assert len(api.mutations("users")) == 1


async def timed_out_submission(page: Page, api: AccountsApi, messages: dict) -> None:
    """実 timer の期限後も遅い成功で失効せず、未知結果の一回として保持する。"""

    await page.clock.install()
    api.hold = True
    form = await password_form(page, messages)
    await double_submit(form)
    await asyncio.wait_for(api.received.wait(), 10)
    await page.clock.fast_forward(30_001)
    await expect(page.get_by_role("alert")).to_contain_text(messages["failures"]["unknown"])
    api.release.set()
    await asyncio.wait_for(api.returned.wait(), 10)
    await settle(page)
    await expect(page.locator('[data-testid="session-ended-count"]')).to_have_text("0")
    await expect(
        page.get_by_role("button", name=messages["adoptLatest"], exact=True)
    ).to_be_enabled()
    await double_submit(form)
    assert len(api.mutations()) == 1


async def late_response(
    page: Page,
    api: AccountsApi,
    messages: dict,
    boundary: str,
    success: bool = False,
) -> None:
    """abort を無視する transport の遅い 401 も、旧会話の callback として破棄する。"""

    api.failure = None if success else (401, "session_expired")
    api.hold = True
    form = await password_form(page, messages)
    await double_submit(form)
    await asyncio.wait_for(api.received.wait(), 10)
    update = {"mounted": False} if boundary == "unmount" else {"projectId": "another-project"}
    if boundary == "actor":
        api.actor = OTHER
        update = {"session": session(OTHER)}
    await page.evaluate("value => window.updateAccountsTestContext(value)", update)
    await settle(page)
    api.release.set()
    await asyncio.wait_for(api.returned.wait(), 10)
    await settle(page)
    await expect(page.locator('[data-testid="session-ended-count"]')).to_have_text("0")
    await expect(page.locator('[data-testid="account-changed-count"]')).to_have_text("0")
    assert len(api.mutations()) == 1
    if boundary != "unmount":
        await expect(
            page.get_by_role("heading", name=messages["myAccount"], exact=True)
        ).to_be_visible()
        await expect(page.locator('input[type="password"]').first).to_have_value("")


async def known_refusal(
    page: Page, api: AccountsApi, messages: dict, status: int, code: str, key: str
) -> None:
    """拒否別文案を検査し、内部 Problem detail に混ぜた password を描画しない。"""

    api.failure = (status, code)
    form = await password_form(page, messages)
    await double_submit(form)
    await expect(page.get_by_role("alert")).to_contain_text(messages["failures"][key])
    await expect(page.locator('[data-testid="session-ended-count"]')).to_have_text("0")
    assert len(api.mutations()) == 1
    if key == "unknown":
        await expect(
            form.get_by_role("button", name=messages["changePassword"], exact=True)
        ).to_be_disabled()
        await double_submit(form)
        assert len(api.mutations()) == 1


async def admin_refusal(page: Page, api: AccountsApi, messages: dict, creation: bool) -> None:
    """末位 ADMIN と重複 email は版更新だけで解決/成功した扱いにしない。"""

    api.failure = (409, "user_email_conflict" if creation else "last_active_admin")
    if creation:
        form = await create_form(page, messages)
    else:
        editor = await choose_target(page, messages)
        form = editor.locator('[data-account-form="edit"]')
        await form.get_by_label(messages["fields"]["status"], exact=True).select_option("DISABLED")
        await form.get_by_label(messages["confirmChange"], exact=True).check()
    await double_submit(form)
    await expect(page.get_by_role("alert")).to_contain_text(
        messages["failures"]["emailConflict" if creation else "lastAdmin"]
    )
    assert len(api.mutations()) == 1
    assert api.users[TARGET]["status"] == "ACTIVE"
    await expect(
        page.get_by_role("button", name=messages["adoptLatest"], exact=True)
    ).to_have_count(0)


async def app_entry(page: Page, api: AccountsApi, messages: dict) -> None:
    """本番 App の Project 非依存 route と navigation から本人失効までを辿る。"""

    assert urlsplit(page.url).fragment == "/accounts"
    toggle = page.locator(".sidebarMenuToggle")
    if await toggle.is_visible():
        await toggle.click()
    links = page.locator('a[href="#/accounts"]')
    await expect(links).to_have_count(1)
    await expect(links).to_be_visible()
    await links.focus()
    await page.keyboard.press("Enter")
    await expect(
        page.get_by_role("heading", name=messages["myAccount"], exact=True)
    ).to_be_visible()
    assert not any("/modules" in call[1] for call in api.calls)
    await self_revoke(page, api, messages)


def delay_initial_project(api: AccountsApi) -> None:
    """最初の Project 読取だけを遅らせ、アカウントの先行操作を可能にする。"""
    api.projects = [project(PROJECT), project(NEXT_PROJECT)]
    api.preference = PROJECT
    api.gates[("GET", "projects")] = ResponseGate()


async def app_initial_project(page: Page, api: AccountsApi, messages: dict, unknown: bool) -> None:
    """初期選択の遅い解決は草稿/unknownを保ち、明示的なProject変更だけが破棄する。"""
    gate = api.gates[("GET", "projects")]
    await asyncio.wait_for(gate.received.wait(), 10)
    form = await password_form(page, messages)
    if unknown:
        api.drop = True
        await double_submit(form)
        await expect(page.get_by_role("alert")).to_contain_text(messages["failures"]["unknown"])
    gate.release.set()
    await expect(page.locator(".sideNavProject select")).to_have_value(PROJECT)
    if unknown:
        await expect(page.get_by_role("alert")).to_contain_text(messages["failures"]["unknown"])
        await expect(
            form.get_by_role("button", name=messages["changePassword"], exact=True)
        ).to_be_disabled()
        assert len(api.mutations()) == 1
    else:
        await expect(form.get_by_label(messages["currentPassword"], exact=True)).to_have_value(
            CURRENT_PASSWORD
        )
    await page.locator(".sideNavProject select").select_option(NEXT_PROJECT)
    await expect(form.get_by_label(messages["currentPassword"], exact=True)).to_have_value("")
    await expect(page.get_by_role("alert")).to_have_count(0)
    assert urlsplit(page.url).fragment == "/accounts"


async def creation_does_not_replace_editor(page: Page, api: AccountsApi, messages: dict) -> None:
    """作成の遅い成功で現在の別アカウントのunknown/原版/草稿を破棄しない。"""
    api.hold = True
    creation = await create_form(page, messages)
    await double_submit(creation)
    await asyncio.wait_for(api.received.wait(), 10)
    editor = await choose_target(page, messages)
    form = editor.locator('[data-account-form="edit"]')
    await form.get_by_label(messages["fields"]["name"], exact=True).fill("Protected unknown draft")
    api.hold = False
    api.drop = True
    await double_submit(form)
    await expect(editor.get_by_role("alert")).to_contain_text(messages["failures"]["unknown"])
    api.release.set()
    await expect(page.get_by_text(messages["createdSuccess"], exact=True)).to_be_visible()
    await expect(editor).to_be_visible()
    await expect(form.get_by_label(messages["fields"]["name"], exact=True)).to_have_value(
        "Protected unknown draft"
    )
    await expect(editor.locator(".accountVersion")).to_contain_text("7")
    await expect(editor.get_by_role("alert")).to_contain_text(messages["failures"]["unknown"])
    await (
        page.locator("[data-account-create]")
        .get_by_role(
            "button",
            name=f"{messages['edit']}: new-account@example.com",
            exact=True,
        )
        .click()
    )
    await expect(
        page.locator('[data-account-editor="00000000-0000-4000-8000-000000000900"]')
    ).to_be_visible()
    assert len(api.mutations()) == 2


async def deadline_before_timer(page: Page, api: AccountsApi, messages: dict) -> None:
    """timerが実行されなくても実経過期限を越えた成功はunknownにする。"""
    await page.evaluate("""() => {
        const original = performance.now.bind(performance);
        window.accountExtraElapsed = 0;
        performance.now = () => original() + window.accountExtraElapsed;
    }""")
    api.hold = True
    form = await password_form(page, messages)
    await double_submit(form)
    await asyncio.wait_for(api.received.wait(), 10)
    await page.evaluate("window.accountExtraElapsed = 30001")
    api.release.set()
    await expect(page.get_by_role("alert")).to_contain_text(messages["failures"]["unknown"])
    await expect(page.locator('[data-testid="session-ended-count"]')).to_have_text("0")
    await expect(
        page.get_by_role("button", name=messages["adoptLatest"], exact=True)
    ).to_be_enabled()
    assert len(api.mutations()) == 1


async def app_sign_in_other(page: Page, api: AccountsApi) -> None:
    """旧会話の失効後に実LoginPageから別actorを認証し、fixture callbackで代用しない。"""
    api.actor = OTHER
    api.projects = []
    api.preference = None
    api.language = "en"
    await page.locator('input[name="email"]').fill(api.users[OTHER]["email"])
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator('button[type="submit"]').click()
    await expect(page.locator(".sidebarUser")).to_contain_text(api.users[OTHER]["email"])
    await expect(page.locator(".sidebarLanguage select")).to_have_value("en")


async def app_late_session(
    page: Page, api: AccountsApi, messages: dict, method: str, suffix: str
) -> None:
    """旧HTTPの成功/401/言語を次のログインへ適用せず、actor境界を実Appで検証する。"""
    gate = api.gates[(method, suffix)]
    if suffix == "auth/logout":
        await page.locator(".sidebarLogout").click()
    elif method == "PUT" and suffix.endswith("ui-language"):
        await page.locator(".sidebarLanguage select").select_option("ja")
        messages = await catalog(page, "ja")
    await asyncio.wait_for(gate.received.wait(), 10)
    del api.gates[(method, suffix)]
    await self_revoke(page, api, messages)
    await app_sign_in_other(page, api)
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 10)
    await settle(page)
    await expect(page.locator(".sidebarUser")).to_contain_text(api.users[OTHER]["email"])
    await expect(page.locator(".sidebarLanguage select")).to_have_value("en")
    await expect(page.locator(".sideNavProject select option")).to_have_count(1)
    await expect(page.locator(".sidebarError")).to_have_count(0)
    await expect(page.locator('input[name="email"]')).to_have_count(0)


def include_admin_self(api: AccountsApi) -> None:
    """本人を管理一覧にも含め、本人資料と管理編集の別の原版を同時に表示する。"""
    api.directory.insert(0, ACTOR)


async def app_self_name_preserves_unknown(page: Page, api: AccountsApi, messages: dict) -> None:
    """本人表示名だけをsidebarへ同期しても本人安全のunknown草稿を再mountしない。"""
    api.drop = True
    password = await password_form(page, messages)
    await double_submit(password)
    await expect(page.locator("[data-account-own]").get_by_role("alert")).to_contain_text(
        messages["failures"]["unknown"]
    )
    await (
        page.locator("[data-account-directory]")
        .get_by_role(
            "button",
            name=f"{messages['edit']}: reader@example.com",
            exact=True,
        )
        .click()
    )
    editor = page.locator(f'[data-account-editor="{ACTOR}"]')
    form = editor.locator('[data-account-form="edit"]')
    await form.get_by_label(messages["fields"]["name"], exact=True).fill("New visible self name")
    api.drop = False
    await form.get_by_role("button", name=messages["save"], exact=True).click()
    await expect(page.locator(".sidebarUser")).to_contain_text("New visible self name")
    own = page.locator("[data-account-own]")
    await expect(own.get_by_role("alert")).to_contain_text(messages["failures"]["unknown"])
    await expect(own.locator(".accountVersion")).to_contain_text("7")
    await expect(
        password.get_by_role("button", name=messages["changePassword"], exact=True)
    ).to_be_disabled()
    assert len(api.mutations()) == 2


async def check_app_races(browser: Browser, url: str) -> None:
    """Appの実入口に限定して初期解決と別会話の遅い応答を検証する。"""
    for unknown in (False, True):

        async def initial_project(
            page: Page, api: AccountsApi, messages: dict, unknown: bool = unknown
        ) -> None:
            """初期Project解決を通常草稿とunknownの双方で遅延させる。"""
            await app_initial_project(page, api, messages, unknown)

        await exercise_case(
            browser,
            url,
            f"app-initial-project-{unknown}",
            initial_project,
            app=True,
            setup=delay_initial_project,
            ignore_abort=True,
        )
    await exercise_case(
        browser, url, "creation-preserves-editor", creation_does_not_replace_editor, role="ADMIN"
    )
    await exercise_case(
        browser, url, "deadline-before-timer", deadline_before_timer, ignore_abort=True
    )
    await exercise_case(
        browser,
        url,
        "app-self-name-preserves-unknown",
        app_self_name_preserves_unknown,
        role="ADMIN",
        app=True,
        setup=include_admin_self,
    )
    for method, suffix in (
        ("GET", "projects"),
        ("GET", "users/me/project-preference"),
        ("GET", f"projects/{PROJECT}/modules"),
        ("GET", "users/me/ui-language"),
        ("PUT", "users/me/ui-language"),
        ("POST", "auth/logout"),
    ):

        def setup_late(api: AccountsApi, method: str = method, suffix: str = suffix) -> None:
            """先行HTTPを一件のgateに固定し、新会話の応答と区別する。"""
            gate = ResponseGate()
            api.gates[(method, suffix)] = gate
            if suffix.endswith("/modules"):
                api.projects = [project(PROJECT)]
                api.preference = PROJECT
            if suffix == "users/me/ui-language" and method == "GET":
                api.language = "ja"
            if suffix not in {"auth/logout", "users/me/ui-language"} or method == "PUT":
                gate.failure = (401, "session_expired")

        async def late_session(
            page: Page, api: AccountsApi, messages: dict, method: str = method, suffix: str = suffix
        ) -> None:
            """各公開HTTP入口の旧会話callbackを別の実ログインへ持ち込まない。"""
            await app_late_session(page, api, messages, method, suffix)

        await exercise_case(
            browser,
            url,
            f"app-late-{method}-{suffix.replace('/', '-')}",
            late_session,
            app=True,
            setup=setup_late,
            ignore_abort=True,
        )


async def admin_self_revocation(
    page: Page, api: AccountsApi, messages: dict, disable: bool
) -> None:
    """ADMINの本人管理PUTも成功した失効なら再logoutせず本人画面を閉じる。"""
    await (
        page.locator("[data-account-directory]")
        .get_by_role(
            "button",
            name=f"{messages['edit']}: reader@example.com",
            exact=True,
        )
        .click()
    )
    form = page.locator(f'[data-account-editor="{ACTOR}"] [data-account-form="edit"]')
    field = messages["fields"]["status" if disable else "role"]
    await form.get_by_label(field, exact=True).select_option("DISABLED" if disable else "USER")
    await form.get_by_label(messages["confirmChange"], exact=True).check()
    await double_submit(form)
    await expect(page.locator('input[name="email"]')).to_be_visible()
    assert len(api.mutations(f"users/{ACTOR}")) == 1
    assert not any(call[1] == "auth/logout" for call in api.calls)


async def cooldown_survives_adoption(page: Page, api: AccountsApi, messages: dict) -> None:
    """版の明示採用は有効な配額待機を解除せず、拒否理由も消さない。"""
    api.retry_seconds = 300
    api.failure = (429, "login_rate_limited")
    form = await password_form(page, messages)
    await double_submit(form)
    await expect(page.get_by_role("alert")).to_contain_text(messages["failures"]["rateLimited"])
    api.users[ACTOR]["row_version"] = 8
    own = page.locator("[data-account-own]")
    await own.get_by_role("button", name=messages["refresh"], exact=True).first.click()
    await own.get_by_role("button", name=messages["adoptLatest"], exact=True).click()
    await expect(own.locator(".accountVersion")).to_contain_text("8")
    await expect(own.get_by_role("alert")).to_contain_text(messages["failures"]["rateLimited"])
    await expect(
        form.get_by_role("button", name=messages["changePassword"], exact=True)
    ).to_be_disabled()
    assert len(api.mutations()) == 1


async def check(url: str, output: Path | None) -> None:
    """実サービスへ向けられない専用 URL で matrix と危険な競争を実行する。"""

    address = urlsplit(url)
    if address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Accounts fixture URL must use a loopback Vite server")
    if (
        not address.path.endswith("/tests/browser/accounts.html")
        or address.query
        or address.fragment
    ):
        raise ValueError("Only the dedicated Accounts browser fixture is supported")
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ("zh", "ja", "en"):
                for width in (390, 1440):
                    await exercise_case(
                        browser,
                        url,
                        f"own-{language}-{width}",
                        own_refusal,
                        language=language,
                        width=width,
                        output=output,
                    )
                    await exercise_case(
                        browser,
                        url,
                        f"admin-{language}-{width}",
                        admin_directory,
                        role="ADMIN",
                        language=language,
                        width=width,
                        output=output,
                    )
                    await exercise_case(
                        browser,
                        url,
                        f"app-{language}-{width}",
                        app_entry,
                        app=True,
                        language=language,
                        width=width,
                        output=output,
                    )
            for name, action, role in (
                ("self-revoke", self_revoke, "USER"),
                ("version-conflict", version_conflict, "ADMIN"),
                ("unknown", unknown_submission, "USER"),
                ("creation-unknown", creation_unknown, "ADMIN"),
            ):
                await exercise_case(browser, url, name, action, role=role)
            for boundary in ("actor", "project", "unmount"):

                async def late(
                    page: Page, api: AccountsApi, messages: dict, boundary: str = boundary
                ) -> None:
                    """loop 変数を固定して各 lifecycle 境界を単独で試す。"""
                    await late_response(page, api, messages, boundary)

                await exercise_case(browser, url, f"late-{boundary}", late, ignore_abort=True)

                async def late_success(
                    page: Page,
                    api: AccountsApi,
                    messages: dict,
                    boundary: str = boundary,
                ) -> None:
                    """離頁後に元操作が成功しても、新会話の状態を変更しない。"""
                    await late_response(page, api, messages, boundary, success=True)

                await exercise_case(
                    browser,
                    url,
                    f"late-success-{boundary}",
                    late_success,
                    ignore_abort=True,
                )
            await exercise_case(
                browser,
                url,
                "timeout-unknown",
                timed_out_submission,
                ignore_abort=True,
            )
            for status, code, key in (
                (403, "csrf_rejected", "csrfRejected"),
                (403, "administrator_required", "adminRequired"),
                (404, "user_not_found", "notFound"),
                (422, "validation_error", "invalidRequest"),
                (429, "login_rate_limited", "rateLimited"),
                (429, "upstream_rate_limit", "unknown"),
                (503, "login_protection_unavailable", "unavailable"),
            ):

                async def refusal(
                    page: Page,
                    api: AccountsApi,
                    messages: dict,
                    status: int = status,
                    code: str = code,
                    key: str = key,
                ) -> None:
                    """公開 Problem 分類を個別 case に束縛する。"""
                    await known_refusal(page, api, messages, status, code, key)

                await exercise_case(browser, url, code, refusal)
            for creation in (False, True):

                async def refusal(
                    page: Page, api: AccountsApi, messages: dict, creation: bool = creation
                ) -> None:
                    """作成と既存 account の意味の異なる 409 を分ける。"""
                    await admin_refusal(page, api, messages, creation)

                await exercise_case(
                    browser, url, f"admin-refusal-{creation}", refusal, role="ADMIN"
                )
            await check_app_races(browser, url)
            await exercise_case(
                browser, url, "cooldown-survives-adoption", cooldown_survives_adoption
            )
            for disable in (False, True):

                async def self_change(
                    page: Page, api: AccountsApi, messages: dict, disable: bool = disable
                ) -> None:
                    """本人降格と本人無効化の同一公開PUTを個別に検査する。"""
                    await admin_self_revocation(page, api, messages, disable)

                await exercise_case(
                    browser,
                    url,
                    f"admin-self-revocation-{disable}",
                    self_change,
                    role="ADMIN",
                    app=True,
                    setup=include_admin_self,
                )
        finally:
            await browser.close()
    print("Accounts browser checks passed; API mock only, no real transaction/HTTPS proof.")


def main() -> None:
    """loopback の明示 fixture と工作区外 screenshot directory を受け付ける。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    asyncio.run(check(arguments.url, arguments.output))


if __name__ == "__main__":
    main()
