"""全ページの両テーマを PC 優先・三語・狭幅で、実 App と全面 mock API で確認する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from check_projects import PROJECT, RUN, layout, messages, settle
from check_result_references import CONTRACTS, ResultApi
from playwright.async_api import Browser, Page, Route, async_playwright, expect


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


async def brand_identity(page: Page, *, login: bool = False) -> None:
    """製品印はログインだけに残し、導航には重複したブランド領域を置かない。"""
    if not login:
        await expect(page.locator(".sidebar .brand, .sidebar .brandMark")).to_have_count(0)
        return
    mark = page.locator(".authBrand .brandMark")
    await expect(mark).to_be_visible()
    await expect(mark).to_have_attribute("alt", "")
    await mark.evaluate("el => el.decode()")
    size = await mark.bounding_box()
    assert size and size["width"] == size["height"] == 42, size
    await expect(page.locator(".authBrand strong")).to_have_text("Skillmind")


async def header_navigation(page: Page) -> None:
    """側欄・tab と同じ遷移を頁見出しへ重複させず、共通の予定 icon を使う。"""
    await expect(page.locator(".pageHeader button, .pageHeader a")).to_have_count(0)
    await expect(page.locator(".historyPage .panelHeader a")).to_have_count(0)
    icon = page.locator('.sideNav a[href*="/schedules"] svg')
    await expect(icon.locator("rect")).to_have_count(1)
    await expect(icon.locator("circle")).to_have_count(0)


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
        await page.get_by_role("tab", name=labels["skills"]["tabWorkbench"], exact=True).click()
        await page.locator(".skillTextSource > summary").click()
        source = page.locator(".skillForm textarea").first
        await source.fill("Browser-only unsaved draft")
        await page.locator(".skillTextSource > summary").click()
        await page.get_by_role("tab", name=labels["skills"]["libraryTitle"]).click()
        await page.get_by_role("tab", name=labels["skills"]["tabWorkbench"], exact=True).click()
        await page.locator(".skillTextSource > summary").click()
        await expect(source).to_have_value("Browser-only unsaved draft")
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
        await expect(page.locator('meta[name="theme-color"]')).to_have_attribute("content", "#f7f5ef")
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
        await brand_identity(page, login=True)
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


async def sidebar_layout(page: Page) -> None:
    """親だけが縦スクロールし、footer が menu を覆わず横にも溢れないことを守る。"""
    metrics = await page.evaluate("""() => {
      const panel = document.querySelector('.navigationPanel');
      const nav = document.querySelector('.sideNav');
      const footer = document.querySelector('.sidebarFooter');
      return {
        widths: [panel, nav, footer].map(el => [el.clientWidth, el.scrollWidth]),
        panelScroll: getComputedStyle(panel).overflowY,
        navScroll: getComputedStyle(nav).overflowY,
        footerGap: footer.getBoundingClientRect().top - nav.getBoundingClientRect().bottom,
      };
    }""")
    assert all(scroll <= client + 1 for client, scroll in metrics["widths"]), metrics
    assert metrics["panelScroll"] == "auto" and metrics["navScroll"] == "visible", metrics
    assert metrics["footerGap"] >= -1, metrics
    # focus による親 scroll で、退出・言語・外観の全操作へ到達できる。
    for selector in (".sidebarLogout", ".sidebarLanguage select", ".themeToggle button"):
        control = page.locator(selector).first
        await control.focus()
        await expect(control).to_be_focused()
        assert await control.evaluate("""el => {
          const r = el.getBoundingClientRect();
          const p = el.closest('.navigationPanel').getBoundingClientRect();
          return r.top >= p.top - 1 && r.bottom <= p.bottom + 1;
        }""")
    await page.locator(".navigationPanel").evaluate("el => el.scrollTop = 0")


async def empty_project_layout(browser: Browser, url: str, output: Path) -> None:
    """空 Project の三語・両テーマで、notice の本文間隔と再読込を実測する。"""
    for language, width in (("zh", 1366), ("ja", 1440), ("en", 1920), ("zh", 390)):
        for theme in ("dark", "light"):
            api = VisualApi(url, language)
            api.projects = []
            api.details = {}
            api.preference = None
            context = await browser.new_context(
                viewport={"width": width, "height": 768 if width == 1366 else 900},
                locale=language, reduced_motion="reduce",
            )
            await context.route("**/*", api.route)
            await context.add_init_script(f"localStorage.setItem('skillmind.theme', '{theme}')")
            page = await context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
            try:
                await page.goto(f"{url}#/")
                labels = await messages(page, language)
                notice = page.locator('.projectContextNotice[data-project-context="empty"]')
                await expect(notice).to_be_visible()
                await expect(page.locator(".pageHeader h1")).to_be_visible()
                await settle(page)
                await page.evaluate("document.fonts.ready")
                metrics = await notice.evaluate("""el => {
                  const r = el.getBoundingClientRect();
                  const next = el.nextElementSibling.getBoundingClientRect();
                  return {height: r.height, gap: next.top - r.bottom};
                }""")
                assert metrics["gap"] >= 23, metrics
                if width > 960:
                    assert metrics["height"] < 110, metrics
                    await sidebar_layout(page)
                await notice.get_by_role("button", name=labels["runHistory"]["retry"]).click()
                await expect(notice).to_be_visible()
                await layout(page)
                assert not api.mutations() and not api.unexpected and not api.failures
                assert not errors
                await page.screenshot(path=str(output / f"empty-{language}-{width}-{theme}.png"))
                print(f"PASS empty-{language}-{width}-{theme} {metrics}", flush=True)
            finally:
                await context.close()


async def skill_identity_layout(browser: Browser, url: str, output: Path) -> None:
    """合成 parse/save 応答だけで技術 drawer を開き、二つの UUID 枠を実測する。"""
    manifest = json.loads((CONTRACTS / "examples/generic-native-manifest.v1alpha1.json").read_text())
    preview = {
        "normalized_package": {
            "package_format": "skillmind.normalized/v1",
            "source": {"type": "directory", "content_hash": manifest["identity"]["source_hash"],
                       "detected_adapter": "directory-skill/v1", "files": []},
            "metadata": {"name": "Browser skill", "description": "Layout fixture", "argument_hint": None},
            "resources": {"scripts": [], "references": [], "assets": []},
            "declared_tools": [], "diagnostics": [],
        },
        "runtime_manifest_draft": {**manifest, "extensions": {}}, "capability_blueprint": None,
    }
    stored = {
        "skill_source_id": "00000000-0000-4000-8000-000000000040",
        "interpretation_id": manifest["identity"]["interpretation_id"],
        "organization_id": "00000000-0000-4000-8000-000000000002",
        "name": "Browser skill", "source_hash": manifest["identity"]["source_hash"],
        "source_type": "directory", "interpretation_status": "PREVIEW_READY",
        "compatibility_level": "native", "confidence": 1,
        "interpreter_version": manifest["identity"]["interpreter_version"],
        "created_at": "2026-09-10T00:00:00Z", "preview": preview,
    }
    for language, width in (("ja", 1440), ("zh", 1366), ("en", 1920), ("ja", 390)):
        for theme in ("dark", "light"):
            api = VisualApi(url, language)
            context = await browser.new_context(viewport={"width": width, "height": 900},
                                                locale=language, reduced_motion="reduce")
            posts: list[str] = []

            async def respond(route: Route) -> None:
                """parse/save だけを fixture へ閉じ、モデル・実保存へ到達させない。"""
                suffix = route.request.url.removeprefix(f"{api.origin}{api.prefix}")
                if route.request.method == "POST" and suffix in ("skills/parse", "skill-imports"):
                    posts.append(suffix)
                    await route.fulfill(status=200 if suffix == "skills/parse" else 201,
                                        json=preview if suffix == "skills/parse" else stored)
                else:
                    await api.route(route)

            await context.route("**/*", respond)
            await context.add_init_script(f"localStorage.setItem('skillmind.theme', '{theme}')")
            page = await context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
            try:
                await page.goto(f"{url}#/skills?project={PROJECT}")
                labels = await messages(page, language)
                await page.get_by_role("tab", name=labels["skills"]["tabWorkbench"], exact=True).click()
                await page.locator(".skillTextSource > summary").click()
                await page.locator(".skillForm textarea").first.fill("# Browser-only layout fixture")
                await page.locator('.skillForm button[type="submit"]').click()
                await page.locator(".saveSkillButton").click()
                trigger = page.locator(".savedSkill .detailDrawerTrigger")
                await trigger.click()
                drawer = page.locator(".savedSkill [role=dialog]")
                await expect(drawer).to_be_visible()
                fields = drawer.locator(".runFacts > div")
                await expect(fields).to_have_count(2)
                boxes = [await field.bounding_box() for field in await fields.all()]
                assert all(boxes) and abs(boxes[0]["width"] - boxes[1]["width"]) < 1, boxes
                assert boxes[1]["y"] >= boxes[0]["y"] + boxes[0]["height"], boxes
                assert await drawer.locator("dd").all_text_contents() == [
                    stored["skill_source_id"], stored["interpretation_id"],
                ]
                for field in await drawer.locator("dd").all():
                    assert await field.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                await layout(page)
                await page.screenshot(path=str(output / f"skill-identity-{language}-{width}-{theme}.png"))
                await page.keyboard.press("Escape")
                await expect(trigger).to_be_focused()
                assert posts == ["skills/parse", "skill-imports"]
                assert not errors and not api.unexpected and not api.failures and not api.mutations()
                print(f"PASS skill-identity-{language}-{width}-{theme}", flush=True)
            finally:
                await context.close()


async def permission_feedback(browser: Browser, url: str, output: Path) -> None:
    """USER の一覧取得と技能操作の 403 を模擬し、三語・両テーマの翻訳を確認する。"""
    output.mkdir(parents=True, exist_ok=True)
    for language in ("zh", "ja", "en"):
        for theme in ("dark", "light"):
            api = VisualApi(url, language)
            api.role = "USER"
            api.users[api.actor]["system_role"] = "USER"
            context = await browser.new_context(viewport={"width": 1440, "height": 900}, locale=language)
            posts: list[str] = []

            async def respond(route: Route) -> None:
                """安定 code は維持し、英語の Problem 本文が画面へ漏れないことを試す。"""
                suffix = route.request.url.removeprefix(f"{api.origin}{api.prefix}")
                denied_read = route.request.method == "GET" and suffix in {
                    f"projects/{PROJECT}/{resource}" for resource in
                    ("secret-references", "integrations", "resource-bindings", "effect-preauthorizations")
                }
                denied_write = route.request.method == "POST" and suffix == "skills/parse"
                if denied_read or denied_write:
                    if denied_write:
                        posts.append(suffix)
                    await route.fulfill(status=403, json={"status": 403, "title": "Access denied",
                        "code": "administrator_required", "detail": "Administrator access is required."})
                else:
                    await api.route(route)

            await context.route("**/*", respond)
            await context.add_init_script(f"localStorage.setItem('skillmind.theme', '{theme}')")
            page = await context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
            try:
                for name in ("resources", "skills"):
                    await page.goto(f"{url}#/{name}?project={PROJECT}")
                    labels = await messages(page, language)
                    if name == "skills":
                        await page.get_by_role("tab", name=labels["skills"]["tabWorkbench"], exact=True).click()
                        await page.locator(".skillTextSource > summary").click()
                        await page.locator(".skillForm textarea").first.fill("# Browser-only permission fixture")
                        await page.locator('.skillForm button[type="submit"]').click()
                    await expect(page.get_by_text(labels["account"]["failures"]["adminRequired"], exact=True)).to_be_visible()
                    assert "Administrator access is required." not in await page.locator("main").inner_text()
                    await layout(page)
                    await page.screenshot(path=str(output / f"permission-{name}-{language}-{theme}.png"))
                assert posts == ["skills/parse"]
                assert not api.unexpected and not api.failures and not api.mutations() and not errors
                print(f"PASS permissions-{language}-{theme}", flush=True)
            finally:
                await context.close()


async def check(url: str, output: Path) -> None:
    """全ルートで横溢れ・描画例外・配色・非意図的な API 書込を検出する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            await theme_controls(browser, url, output)
            await empty_project_layout(browser, url, output)
            await skill_identity_layout(browser, url, output)
            await permission_feedback(browser, url, output)
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
                            await brand_identity(page)
                            await header_navigation(page)
                            if name == "skills" and language == "ja" and width > 960:
                                lines = await page.locator(".skillScopeBadge").evaluate("""el => {
                                  const range = document.createRange();
                                  range.selectNodeContents(el);
                                  return new Set([...range.getClientRects()].map(r => r.top)).size;
                                }""")
                                assert lines == 1, lines
                            if name == "documents":
                                await expect(page.locator(".documentUploadStatus, .documentUploadClosure")).to_have_count(0)
                                await expect(page.locator(".documentHelp")).not_to_have_attribute("open", "")
                                spacing = await page.locator(".documentToolbar").evaluate("""el =>
                                  el.nextElementSibling.getBoundingClientRect().top
                                  - el.getBoundingClientRect().bottom""")
                                assert spacing >= 16, spacing
                                if width > 960:
                                    controls = await page.locator(".documentToolbar").evaluate("""el =>
                                      [...el.children].map(child => child.getBoundingClientRect().top)""")
                                    assert max(controls) - min(controls) <= 1, controls
                                    bounds = await page.locator(".documentTree").bounding_box()
                                    assert bounds and bounds["y"] < 500, bounds
                            if name == "skills":
                                await expect(page.locator(".skillForm")).not_to_be_visible()
                            if name == "accounts":
                                await expect(page.locator('[data-account-form="password"]')).not_to_be_visible()
                                await expect(page.locator("[data-account-directory]")).not_to_be_visible()
                            if width > 960:
                                await sidebar_layout(page)
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
                                for index in range(await tabs.count()):
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
