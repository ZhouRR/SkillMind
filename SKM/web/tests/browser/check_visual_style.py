"""全ページの両テーマを PC 優先・三語・狭幅で、実 App と全面 mock API で確認する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from check_projects import PROJECT, RUN, layout, messages, settle
from check_result_references import CONTRACTS, ResultApi
from playwright.async_api import Browser, Route, async_playwright, expect


class VisualApi(ResultApi):
    """既存契約 fixture を表示専用に組み合わせ、未知の通信は拒否する。"""

    def __init__(self, url: str, language: str) -> None:
        """実プロジェクトや公開サンプルを変更せず、独立した表示状態を作る。"""
        super().__init__(url, language, "contract")
        self.with_tasks = True

    async def respond(self, route: Route) -> None:
        """リソース一覧を空状態、Skill を既存契約の公開状態で表示する。"""
        address = urlsplit(route.request.url)
        suffix = address.path.removeprefix(self.prefix)
        if f"{address.scheme}://{address.netloc}" == self.origin and route.request.method == "GET":
            bodies = {
                "skill-versions": json.loads(
                    (CONTRACTS / "examples/skill-version-list.v1.json").read_text()
                ),
                f"projects/{PROJECT}/skill-versions": {"skill_versions": []},
                f"projects/{PROJECT}/members": json.loads(
                    (CONTRACTS / "examples/project-member-list.v1.json").read_text()
                ),
                f"projects/{PROJECT}/documents": {
                    "documents": [json.loads((CONTRACTS / "examples/document.v1.json").read_text())]
                },
                **{
                    f"projects/{PROJECT}/{resource}": {"items": []}
                    for resource in (
                        "secret-references",
                        "integrations",
                        "resource-bindings",
                        "effect-preauthorizations",
                    )
                },
            }
            if suffix in bodies:
                await route.fulfill(json=bodies[suffix])
                return
        await super().respond(route)


async def theme_controls(browser: Browser, url: str, output: Path) -> None:
    """実 button・再読込・別 tab・storage 拒否を検証し、業務草稿の再 mount を検出する。"""
    api = VisualApi(url, "zh")
    context = await browser.new_context(
        viewport={"width": 1440, "height": 1000},
        color_scheme="light",
        locale="zh",
        reduced_motion="reduce",
    )
    await context.route("**/*", api.route)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        await page.goto(f"{url}#/skills?project={PROJECT}")
        labels = await messages(page, "zh")
        toggle = page.locator(".themeToggle")
        await expect(toggle.get_by_role("button", name=labels["theme"]["dark"])).to_have_attribute(
            "aria-pressed", "true"
        )
        assert await page.evaluate("localStorage.getItem('skillmind.theme')") is None
        source = page.locator(".skillForm textarea").first
        await source.fill("Browser-only unsaved draft")
        await toggle.get_by_role("button", name=labels["theme"]["light"]).click()
        await expect(page.locator("html")).to_have_attribute("data-theme", "light")
        await expect(source).to_have_value("Browser-only unsaved draft")
        # 同一 tab の route 変更でも表示 preference だけを引き継ぐ。
        await page.locator('.sideNav a[href*="/resources"]').click()
        await expect(page.locator("html")).to_have_attribute("data-theme", "light")
        await page.get_by_role(
            "button", name=labels["resources"]["connectTitle"], exact=True
        ).click()
        dialog = page.get_by_role("dialog", name=labels["resources"]["connectTitle"], exact=True)
        await expect(dialog).to_be_visible()
        await expect(page.locator(".modalDrawer:not([hidden])")).to_have_count(1)
        field = dialog.locator('input:not([type="password"])').first
        await field.fill("https://fixture.example.com")
        # 別 tab からの変更は開いた drawer を閉じず、原草稿を保持する。
        other = await context.new_page()
        await other.goto(f"{url}#/history?project={PROJECT}")
        await (
            other.locator(".themeToggle")
            .get_by_role("button", name=labels["theme"]["dark"])
            .click()
        )
        await expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        await expect(dialog).to_be_visible()
        await expect(field).to_have_value("https://fixture.example.com")
        await page.screenshot(path=str(output / "resources-drawer-zh-1440-dark.png"))
        await page.keyboard.press("Escape")
        await expect(dialog).to_be_hidden()
        await expect(
            page.get_by_role("button", name=labels["resources"]["connectTitle"], exact=True)
        ).to_be_focused()
        await other.close()
        await toggle.get_by_role("button", name=labels["theme"]["light"]).click()
        # 本番 HTML の同期 initializer も対象とする。API は同じ mock だけに閉じる。
        entry = url.removesuffix("tests/browser/projects.html")
        await page.goto(entry)
        await expect(page.locator("html")).to_have_attribute("data-theme", "light")
        await expect(page.locator('.themeToggle button[aria-pressed="true"]')).to_have_text(
            labels["theme"]["light"]
        )
        await page.reload()
        await expect(page.locator("html")).to_have_attribute("data-theme", "light")
        assert await page.locator('meta[name="theme-color"]').get_attribute("content") == "#f7f5ef"
        assert not api.mutations() and not api.unexpected and not api.failures and not errors
        print("PASS theme-default-persistence-cross-tab-draft-drawer", flush=True)
    finally:
        await context.close()

    # Login には session や Project を要求せず、storage 全面拒否でも手動切替できる。
    context = await browser.new_context(
        viewport={"width": 1440, "height": 1000},
        locale="zh",
        reduced_motion="reduce",
    )
    api = VisualApi(url, "zh")

    async def anonymous(route: Route) -> None:
        """未認証応答だけを差し替え、ログイン送信や実 backend 呼出は行わない。"""
        if urlsplit(route.request.url).path == f"{api.prefix}auth/session":
            await route.fulfill(status=401, json={"code": "authentication_required"})
        else:
            await api.route(route)

    await context.route("**/*", anonymous)
    await context.add_init_script("""Object.defineProperty(window, 'localStorage', {
      get() { throw new DOMException('Denied', 'SecurityError'); }
    });""")
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        await page.goto(url.removesuffix("tests/browser/projects.html"))
        await expect(page.locator(".authLayout")).to_be_visible()
        await expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        await page.screenshot(path=str(output / "login-zh-1440-dark.png"))
        await page.locator('input[name="email"]').fill("draft@example.com")
        toggle = page.locator(".themeToggle")
        await toggle.locator("button").first.focus()
        await page.keyboard.press("Enter")
        await expect(page.locator("html")).to_have_attribute("data-theme", "light")
        await expect(page.locator('input[name="email"]')).to_have_value("draft@example.com")
        await page.screenshot(path=str(output / "login-zh-1440-light.png"))
        assert not api.mutations() and not api.unexpected and not api.failures and not errors
        print("PASS theme-login-keyboard-storage-denied", flush=True)
    finally:
        await context.close()


async def check(url: str, output: Path) -> None:
    """全ルートで横溢れ・描画例外・配色・非意図的な API 書込を検出する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            await theme_controls(browser, url, output)
            for language, width in (
                ("zh", 1366),
                ("zh", 1440),
                ("zh", 1920),
                ("ja", 1440),
                ("en", 1440),
                ("zh", 390),
            ):
                for theme in ("dark", "light"):
                    api = VisualApi(url, language)
                    context = await browser.new_context(
                        viewport={
                            "width": width,
                            "height": {1366: 768, 1440: 900, 1920: 1080, 390: 844}[width],
                        },
                        locale=language,
                        reduced_motion="reduce",
                    )
                    await context.route("**/*", api.route)
                    if theme == "light":
                        await context.add_init_script(
                            "localStorage.setItem('skillmind.theme', 'light')"
                        )
                    page = await context.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                    try:
                        for name in (
                            "home",
                            "skills",
                            "projects",
                            "resources",
                            "documents",
                            "tasks",
                            "schedules",
                            "workspace",
                            "history",
                            "accounts",
                        ):
                            path = "" if name == "home" else name
                            target = f"{url}#/{path}?project={PROJECT}"
                            if name == "workspace":
                                target += f"&run={RUN}"
                            await page.goto(target)
                            await messages(page, language)
                            await expect(page.locator(".pageHeader h1")).to_be_visible()
                            if name == "workspace":
                                await expect(page.locator(".resultSummary")).to_be_visible()
                            if name == "tasks":
                                await expect(page.locator(".taskCard")).to_have_count(1)
                            await settle(page)
                            await page.evaluate("document.fonts.ready")
                            await layout(page)
                            if width > 960:
                                position = await page.locator(".themeToggle").bounding_box()
                                assert (
                                    position
                                        and position["y"] >= 0
                                    and (
                                        position["y"] + position["height"]
                                        <= page.viewport_size["height"]
                                    )
                                ), position
                            assert (
                                await page.evaluate(
                                    "getComputedStyle(document.documentElement).colorScheme"
                                )
                                == theme
                            )
                            if name == "home":
                                # 実際の cascade から色を読み、小さな本文にも 4.5:1 を要求する。
                                contrasts = await page.evaluate("""() => {
                                  const style = getComputedStyle(document.documentElement);
                                  const luminance = token => {
                                    const hex = style.getPropertyValue(token).trim();
                                    const rgb = [1, 3, 5].map(start => {
                                      const x = parseInt(hex.slice(start, start + 2), 16) / 255;
                                      return x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4;
                                    });
                                    return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
                                  };
                                  return [
                                    ['--text', '--bg'], ['--text-hi', '--surface'],
                                    ['--text-muted', '--surface'],
                                    ['--text-faint', '--surface-raised'],
                                    ['--accent', '--surface'], ['--accent', '--accent-soft'],
                                    ['--primary-ink', '--primary-bg'],
                                    ['--primary-ink', '--primary-hover'],
                                    ['--live', '--live-soft'], ['--ok', '--ok-soft'],
                                    ['--warn', '--warn-soft'], ['--danger', '--danger-soft'],
                                    ['--destructive-ink', '--destructive-bg'],
                                  ].map(pair => {
                                    const values = pair.map(luminance).sort((a, b) => a - b);
                                    return [pair.join('/'), (values[1] + .05) / (values[0] + .05)];
                                  });
                                }""")
                                assert all(ratio >= 4.5 for _, ratio in contrasts), contrasts
                            for selector, token in (
                                (".primaryButton:enabled", "--primary-bg"),
                                (".destructiveButton:enabled", "--destructive-bg"),
                            ):
                                for button in await page.locator(selector).all():
                                    assert await button.evaluate(
                                        """(el, token) => {
                                          const probe = document.createElement('span');
                                          probe.style.backgroundColor = `var(${token})`;
                                          document.body.append(probe);
                                          const expected = getComputedStyle(probe).backgroundColor;
                                          probe.remove();
                                          return getComputedStyle(el).backgroundColor === expected;
                                        }""",
                                        token,
                                    ), (name, selector)
                            assert not api.unexpected and not api.failures and not errors, (
                                api.unexpected,
                                api.failures,
                                errors,
                            )
                            assert not api.mutations()
                            await page.screenshot(
                                path=str(output / f"{name}-{language}-{width}-{theme}.png"),
                            )
                            print(f"PASS {name}-{language}-{width}-{theme}", flush=True)
                            if width == 1440 and name in {"skills", "projects", "resources"}:
                                # 読取 tab だけを実操作する。権限変更・公開・接続は実行しない。
                                tabs = page.get_by_role("tab")
                                for index in range(1, await tabs.count()):
                                    await tabs.nth(index).click()
                                    await settle(page)
                                    await layout(page)
                                    assert not api.unexpected and not api.failures and not errors
                                    assert not api.mutations()
                                    await page.screenshot(
                                        path=str(
                                            output
                                            / f"{name}-tab-{index}-{language}-{width}-{theme}.png"
                                        )
                                    )
                                    print(f"PASS {name}-tab-{index}-{language}-{theme}", flush=True)
                    finally:
                        await context.close()
        finally:
            await browser.close()


def main() -> None:
    """所有する loopback harness のみを許可し、本番サイトへの実行を防ぐ。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    address = urlsplit(args.url)
    if (
        address.scheme != "http"
        or address.hostname not in ("127.0.0.1", "localhost")
        or not address.path.endswith("/tests/browser/projects.html")
        or address.query
        or address.fragment
    ):
        parser.error("Only an owned loopback projects.html mock harness is allowed")
    asyncio.run(check(args.url, args.output))


if __name__ == "__main__":
    main()
