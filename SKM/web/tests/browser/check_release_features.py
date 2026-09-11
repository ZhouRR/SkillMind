"""首版の実 App を全面 mock HTTP で検証し、後置 GET への依存を検出する。"""

from __future__ import annotations

import argparse
import asyncio
from itertools import product
from pathlib import Path
from urllib.parse import urlsplit

from check_accounts import session
from check_document_upload import UploadBrowserAudit
from check_projects import PROJECT, messages
from check_schedule_management import ScheduleManagementApi
from playwright.async_api import Route, async_playwright, expect


class ReleaseApi(ScheduleManagementApi):
    """会話の配備上限を閉じ、核心ページの不要 request を即座に失敗させる。"""

    async def route(self, route: Route) -> None:
        """外部 origin と API 以外の fetch を拒否する。"""
        request = route.request
        address = urlsplit(request.url)
        if f"{address.scheme}://{address.netloc}" != self.origin or (
            request.resource_type in {"fetch", "xhr", "eventsource"}
            and not address.path.startswith(self.prefix)
        ):
            self.unexpected.append(f"Non-mock request: {request.method} {request.url}")
            await route.abort()
            return
        await super().route(route)

    def __init__(self, url: str, language: str) -> None:
        """一覧と精確履歴は同じ元予定を保持する。"""
        super().__init__(url, language)
        self.allow_schedule_reads = False
        self.records = self.records[:1]

    async def respond(self, route: Route) -> None:
        """定時一覧は管理画面だけ許可し、資源の補助 GET は明示する。"""
        path = urlsplit(route.request.url).path
        if not path.startswith(self.prefix):
            await super().respond(route)
            return
        suffix = path.removeprefix(self.prefix)
        if suffix == "auth/session":
            await route.fulfill(
                json={
                    **session(self.actor, self.role),
                    "deferred_features_enabled": False,
                }
            )
            return
        if suffix.endswith("/effect-preauthorizations") or (
            "/schedules" in suffix and not self.allow_schedule_reads
        ):
            raise AssertionError(f"Deferred dependency: {suffix}")
        if suffix.rsplit("/", 1)[-1] in {
            "integrations",
            "secret-references",
            "resource-bindings",
        }:
            assert route.request.method == "GET"
            await route.fulfill(json={"items": []})
            return
        await super().respond(route)


async def check(url: str, output: Path) -> None:
    """三語・両 theme・四幅で手動入口、只読接続、履歴操作を確認する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language, theme, width in product(
                ("zh", "ja", "en"), ("light", "dark"), (390, 1366, 1440, 1920)
            ):
                api = ReleaseApi(url, language)
                context = await browser.new_context(
                    viewport={"width": width, "height": 900}
                )
                await context.add_init_script(
                    f"localStorage.setItem('skillmind.theme', '{theme}')"
                )
                await context.route("**/*", api.route)
                page = await context.new_page()
                audit = UploadBrowserAudit(page, api)
                try:
                    await page.goto(f"{url}#/tasks?project={PROJECT}")
                    labels = await messages(page, language)
                    await expect(
                        page.get_by_role(
                            "link", name=labels["tasks"]["runNow"], exact=True
                        )
                    ).to_be_visible()
                    await expect(
                        page.get_by_role(
                            "button", name=labels["tasks"]["addSchedule"], exact=True
                        )
                    ).to_have_count(0)
                    await page.screenshot(
                        path=str(output / f"tasks-{language}-{theme}-{width}.png")
                    )
                    await page.goto(f"{url}#/resources?project={PROJECT}")
                    resources = labels["resources"]
                    await expect(
                        page.get_by_role("tab", name=resources["tabPolicy"], exact=True)
                    ).to_have_count(0)
                    await page.get_by_role(
                        "button", name=resources["connectTitle"], exact=True
                    ).click()
                    dialog = page.get_by_role("dialog")
                    await expect(
                        dialog.locator('[name="connect-access"]')
                    ).to_have_count(0)
                    for provider in ("postgres", "mcp"):
                        await dialog.get_by_role(
                            "combobox", name=resources["providerLabel"], exact=True
                        ).select_option(provider)
                        await expect(dialog.locator("textarea")).to_be_visible()
                    await page.screenshot(
                        path=str(output / f"resources-{language}-{theme}-{width}.png")
                    )
                    api.allow_schedule_reads = True
                    await page.goto(f"{url}#/schedules?project={PROJECT}")
                    await expect(
                        page.get_by_text(
                            labels["scheduleManager"]["deferredDisabled"], exact=True
                        )
                    ).to_be_visible()
                    await page.locator("[data-schedule-select]").first.click()
                    await expect(page.locator("[data-schedule-edit]")).to_be_disabled()
                    await expect(
                        page.locator('[data-schedule-status="ACTIVE"]')
                    ).to_have_count(0)
                    await expect(
                        page.locator('[data-schedule-status="PAUSED"]')
                    ).to_be_enabled()
                    await expect(
                        page.locator('[data-schedule-status="ARCHIVED"]')
                    ).to_be_enabled()
                    await page.screenshot(
                        path=str(output / f"schedules-{language}-{theme}-{width}.png")
                    )
                    audit.verify()
                    print(f"PASS {language}-{theme}-{width}", flush=True)
                finally:
                    if api.failures or api.unexpected:
                        print(api.failures, api.unexpected, flush=True)
                    await context.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    target = urlsplit(args.url)
    if (
        target.scheme != "http"
        or target.hostname not in {"127.0.0.1", "localhost"}
        or not target.path.endswith("/tests/browser/projects.html")
    ):
        parser.error("Only the loopback projects.html mock harness is allowed")
    asyncio.run(check(args.url, args.output))
