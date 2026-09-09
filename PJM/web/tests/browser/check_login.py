"""実 LoginPage の拒否表示と離頁を、loopback Vite と mock API で検証する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error, Page, Route, async_playwright, expect

MESSAGES = {
    "zh": {
        # Catalog の全角句読点も user-facing 契約として比較する。
        "wait": "登录请求过于频繁，请至少等待 60 秒后手动重试。",  # noqa: RUF001
        "rate": "登录请求过于频繁，请稍后手动重试。",  # noqa: RUF001
        "unavailable": "登录服务暂时不可用，请稍后手动重试。",  # noqa: RUF001
        "credentials": "邮箱或密码不正确。",
        "csrf": "登录校验未通过，请重新提交。若持续失败，请联系管理员。",  # noqa: RUF001
        "failed": "登录失败。",
    },
    "ja": {
        "wait": "ログイン要求が多すぎます。少なくとも 60 秒待ってから手動で再試行してください。",
        "rate": "ログイン要求が多すぎます。しばらく待ってから手動で再試行してください。",
        "unavailable": (
            "ログインサービスを一時的に利用できません。しばらく待ってから手動で再試行してください。"
        ),
        "credentials": "メールアドレスまたはパスワードが正しくありません。",
        "csrf": (
            "ログインの検証に失敗しました。再送信してください。続く場合は管理者に連絡してください。"
        ),
        "failed": "ログインに失敗しました。",
    },
    "en": {
        "wait": "Too many sign-in requests. Wait at least 60 seconds before trying again manually.",
        "rate": "Too many sign-in requests. Wait before trying again manually.",
        "unavailable": "Sign-in is temporarily unavailable. Please try again manually later.",
        "credentials": "The email or password is incorrect.",
        "csrf": (
            "The sign-in check failed. Submit again. If this continues, contact an administrator."
        ),
        "failed": "Sign-in failed.",
    },
}


class LoginApi:
    """実 credential を使わず、challenge と password の到達順だけを記録する。"""

    def __init__(self, origin: str, api_prefix: str) -> None:
        """応答と待機は case ごとに隔離し、既存 API へは到達させない。"""

        self.origin = origin
        self.api_prefix = api_prefix
        self.stage = "login"
        self.status = 429
        self.retry: str | None = "60"
        self.non_json = False
        self.hold: str | None = None
        self.allow_cancelled = False
        self.calls: list[str] = []
        self.tokens: list[str] = []
        self.unexpected: list[str] = []
        self.received = asyncio.Event()
        self.release = asyncio.Event()
        self.returned = asyncio.Event()

    async def route(self, route: Route) -> None:
        """同一 loopback の資産だけ通し、すべての API と外部 origin を遮断する。"""

        request = route.request
        url = urlsplit(request.url)
        if f"{url.scheme}://{url.netloc}" != self.origin:
            self.unexpected.append(request.url)
            await route.abort()
            return
        # Vite の src/api/*.ts はコード資産であり、公開 API prefix とは区別する。
        if not url.path.startswith((self.api_prefix, "/api/")):
            await route.continue_()
            return
        if request.method == "GET" and url.path == f"{self.api_prefix}v1/auth/login-context":
            stage = "context"
        elif request.method == "POST" and url.path == f"{self.api_prefix}v1/auth/login":
            stage = "login"
            assert request.post_data_json == {
                "email": "reader@example.com",
                "password": "test-only long passphrase",
            }
            assert request.headers["x-csrf-token"] == self.tokens[-1]
            assert request.headers["origin"] == self.origin
            assert f"projectmind_login_csrf={self.tokens[-1]}" in request.headers.get("cookie", "")
        else:
            self.unexpected.append(request.url)
            await route.abort()
            return
        self.calls.append(stage)
        if self.hold == stage:
            self.received.set()
            await asyncio.wait_for(self.release.wait(), timeout=15)
        status = self.status if stage == self.stage else 200
        headers = {"Cache-Control": "no-store"}
        if status == 200 and stage == "context":
            token = f"context-{len(self.tokens)}-" + "c" * 32
            self.tokens.append(token)
            body = {"csrf_token": token, "expires_in_seconds": 300}
            headers["Set-Cookie"] = (
                f"projectmind_login_csrf={token}; Max-Age=300; Path=/; HttpOnly; SameSite=Strict"
            )
        elif status == 200:
            body = {
                "user": {
                    "user_id": "00000000-0000-4000-8000-000000000001",
                    "organization_id": "00000000-0000-4000-8000-000000000002",
                    "email": "reader@example.com",
                    "display_name": "Test reader",
                    "system_role": "USER",
                },
                "csrf_token": "s" * 32,
                "absolute_expires_at": "2099-01-01T00:00:00Z",
            }
            headers["Set-Cookie"] = (
                "projectmind_session=test-only; Path=/; HttpOnly; SameSite=Strict"
            )
        else:
            code = {
                429: "login_rate_limited",
                503: "login_protection_unavailable",
                401: "invalid_credentials",
                403: "csrf_rejected",
            }.get(status, "http_error")
            body = {
                "type": f"https://projectmind.local/problems/{code}",
                "title": "Test refusal",
                "status": status,
                "detail": "Do not show this internal fixture detail",
                "code": code,
                "instance": url.path,
                "request_id": "test-request",
            }
            headers["Content-Type"] = "application/problem+json"
            if self.retry is not None:
                headers["Retry-After"] = self.retry
        try:
            if self.non_json and status != 200:
                headers["Content-Type"] = "text/html"
                await route.fulfill(status=status, headers=headers, body="<html>Unavailable</html>")
            else:
                await route.fulfill(status=status, headers=headers, json=body)
        except Error:
            # Native abort が先に終了した場合だけ transport の応答消失を許す。
            if not self.allow_cancelled:
                raise
        finally:
            self.returned.set()


async def prepare(page: Page, url: str, language: str = "zh") -> None:
    """実入力欄を使い、StrictMode の fixture が操作可能になるまで待つ。"""

    await page.goto(url)
    await page.wait_for_function("typeof window.updateLoginTestContext === 'function'")
    await page.evaluate("language => window.updateLoginTestContext({language})", language)
    await page.locator("input[name=email]").fill("reader@example.com")
    await page.locator("input[name=password]").fill("test-only long passphrase")


async def submit_with_keyboard(page: Page) -> None:
    """password から Tab/Enter で送信し、mouse だけの成功にしない。"""

    await page.locator("input[name=password]").focus()
    await page.keyboard.press("Tab")
    await expect(page.locator("button[type=submit]")).to_be_focused()
    await page.keyboard.press("Enter")


async def layout(page: Page) -> None:
    """エラー文と入力欄が窄幅でも切れず、ページ外へ溢れないことを確認する。"""

    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    for selector in (".authCard", "input[name=email]", "input[name=password]", "[role=alert]"):
        element = page.locator(selector)
        await expect(element).to_be_visible()
        assert await element.evaluate(
            "element => element.scrollWidth <= element.clientWidth + 1"
        ), selector


async def check(url: str, output: Path | None) -> None:
    """拒否 matrix と競争ケースを独立 context で実行し、通信先と storage を監査する。"""

    address = urlsplit(url)
    if address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Login fixture URL must use a loopback Vite server")
    if not address.path.endswith("/tests/browser/login.html"):
        raise ValueError("Only the dedicated Login browser fixture is supported")
    api_prefix = address.path.removesuffix("tests/browser/login.html") + "api/"
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
    cases = []
    for language in MESSAGES:
        for width in (390, 1440):
            for status, retry, non_json, feedback in (
                (429, "60", False, "wait"),
                (429, None, False, "rate"),
                (429, "1.5", False, "rate"),
                (429, "301", False, "rate"),
                (503, None, False, "unavailable"),
                (503, "30", True, "unavailable"),
                (401, None, False, "credentials"),
                (403, None, False, "csrf"),
                (500, None, False, "failed"),
            ):
                cases.append(
                    (
                        f"feedback-{language}-{width}-{status}-{retry}-{non_json}",
                        language,
                        width,
                        status,
                        retry,
                        non_json,
                        feedback,
                    )
                )
    cases += [
        (name, "zh", 390, 429, "60", False, "wait")
        for name in (
            "context-429",
            "context-503",
            "duplicate-manual-retry",
            "change-language",
            "native-context-abort",
            "native-login-abort",
            "late-context-abort",
            "late-login-abort",
        )
    ]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            for name, language, width, status, retry, non_json, feedback in cases:
                context = await browser.new_context(viewport={"width": width, "height": 1000})
                api = LoginApi(f"{address.scheme}://{address.netloc}", api_prefix)
                api.status, api.retry, api.non_json = status, retry, non_json
                errors: list[str] = []
                page = await context.new_page()
                page.set_default_timeout(5000)
                page.on("pageerror", lambda error, bucket=errors: bucket.append(str(error)))
                await context.route("**/*", api.route)
                await page.add_init_script(
                    "window.storageWrites=[]; const save=Storage.prototype.setItem;"
                    "Storage.prototype.setItem=function(key,value){window.storageWrites.push(key);"
                    "return save.call(this,key,value);};"
                )
                try:
                    if name.startswith("late-"):
                        # Transport が signal を無視しても、auth client/画面の確認が必要になる。
                        await page.add_init_script(
                            "const send=window.fetch; window.fetch=(input,init)=>"
                            "send(input,{...init,signal:undefined});"
                        )
                    await prepare(page, url, language)
                    if name.endswith("-abort"):
                        api.hold = "context" if "context" in name else "login"
                        api.status = 200
                        await submit_with_keyboard(page)
                        await asyncio.wait_for(api.received.wait(), timeout=5)
                        await expect(page.locator("form")).to_have_attribute("aria-busy", "true")
                        api.allow_cancelled = name.startswith("native-")
                        await page.evaluate("window.updateLoginTestContext({mounted:false})")
                        await expect(page.locator("form")).to_have_count(0)
                        api.returned.clear()
                        api.release.set()
                        await asyncio.wait_for(api.returned.wait(), timeout=5)
                        await page.wait_for_timeout(150)
                        assert api.calls == (
                            ["context"] if api.hold == "context" else ["context", "login"]
                        )
                        await expect(page.get_by_test_id("authenticated-count")).to_have_text("0")
                    elif name == "duplicate-manual-retry":
                        api.hold = "context"
                        await page.locator("form").evaluate(
                            "form => {for(let i=0;i<2;i++) form.dispatchEvent("
                            "new Event('submit',{bubbles:true,cancelable:true}));}"
                        )
                        await asyncio.wait_for(api.received.wait(), timeout=5)
                        await expect(page.locator("button[type=submit]")).to_be_disabled()
                        assert api.calls == ["context"]
                        api.release.set()
                        await expect(page.get_by_role("alert")).to_have_text(
                            MESSAGES[language][feedback]
                        )
                        await page.wait_for_timeout(350)
                        assert api.calls == ["context", "login"]
                        api.hold, api.status = None, 200
                        await submit_with_keyboard(page)
                        await expect(page.get_by_test_id("authenticated-count")).to_have_text("1")
                        assert api.calls == ["context", "login", "context", "login"]
                        assert len(set(api.tokens)) == 2
                        cookies = await context.cookies()
                        assert any(
                            cookie["name"] == "projectmind_session" and cookie["httpOnly"]
                            for cookie in cookies
                        )
                        assert "projectmind_session" not in await page.evaluate("document.cookie")
                    else:
                        if name.startswith("context-"):
                            api.stage = "context"
                            api.status = int(name.rsplit("-", 1)[1])
                            feedback = "wait" if api.status == 429 else "unavailable"
                        await submit_with_keyboard(page)
                        await expect(page.get_by_role("alert")).to_have_text(
                            MESSAGES[language][feedback]
                        )
                        await expect(page.locator("form")).to_have_attribute("aria-busy", "false")
                        await expect(page.locator("button[type=submit]")).to_be_enabled()
                        await layout(page)
                        await expect(page.get_by_test_id("authenticated-count")).to_have_text("0")
                        assert api.calls == (
                            ["context"] if api.stage == "context" else ["context", "login"]
                        )
                        if name == "change-language":
                            for next_language in ("ja", "en", "zh"):
                                await page.evaluate(
                                    "language => window.updateLoginTestContext({language})",
                                    next_language,
                                )
                                await expect(page.get_by_role("alert")).to_have_text(
                                    MESSAGES[next_language][feedback]
                                )
                            assert api.calls == ["context", "login"]
                        if (
                            output is not None
                            and status in (429, 503)
                            and not name.startswith("context-")
                        ):
                            await page.screenshot(path=output / f"{name}.png")
                    assert not errors, (name, errors)
                    assert not api.unexpected, (name, api.unexpected)
                    assert await page.evaluate("window.storageWrites") == []
                    print(f"{name}: passed ({len(api.calls)} API request(s))", flush=True)
                finally:
                    api.allow_cancelled = True
                    api.release.set()
                    await context.close()
        finally:
            await browser.close()
    print(json.dumps({"cases": len(cases), "real_credentials": False, "real_backend": False}))


def main() -> None:
    """専用 Vite と任意の外部 screenshot directory を指定して実行する。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default="http://127.0.0.1:5191/projectmind/tests/browser/login.html"
    )
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    asyncio.run(check(arguments.url, arguments.output))


if __name__ == "__main__":
    main()
