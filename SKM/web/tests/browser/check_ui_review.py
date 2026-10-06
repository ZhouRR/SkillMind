"""RV 指摘の長一覧・旧タスク名・繰返し設定を実 UI と合成 API で確認する。"""
from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from check_projects import PROJECT, RUN, layout, messages
from check_schedule_times import ScheduleApi, opened, previewed
from check_visual_style import CONTRACTS, VisualApi
from playwright.async_api import async_playwright, expect
from select_helpers import select_option


class ReviewApi(VisualApi):
    """同一 Skill の十版と、catalog から消えた Run の凍結名を供給する。"""

    async def respond(self, route):
        """GET だけを合成し、操作 API は元 fixture の拒否境界に残す。"""
        suffix = urlsplit(route.request.url).path.removeprefix(self.prefix)
        if route.request.method == "GET" and suffix == "skill-versions":
            body = json.loads((CONTRACTS / "examples/skill-version-list.v1.json").read_text())
            seed = body["skill_versions"][0]
            body["skill_versions"] = [dict(deepcopy(seed),
                skill_version_id=f"00000000-0000-4000-8000-{index + 1000:012d}",
                version=f"0.1.{index + 1}") for index in range(10)]
            await route.fulfill(json=body)
            return
        await super().respond(route)


async def check(url: str, output: Path):
    """四幅・両テーマの表示と keyboard 展開を検査する。実サービスは呼ばない。"""
    if urlsplit(url).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Only loopback fixtures are supported")
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            for width, language in ((1366, "zh"), (1440, "ja"), (1920, "en"), (390, "ja")):
                for theme in ("dark", "light"):
                    api = ReviewApi(url, language)
                    api.with_tasks = False
                    api.body["task_title"] = "Original archived review"
                    context = await browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
                    await context.route("**/*", api.route)
                    await context.add_init_script(f"localStorage.setItem('skillmind.theme', '{theme}')")
                    page = await context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    try:
                        await page.goto(f"{url}#/skills?project={PROJECT}")
                        await messages(page, language)
                        await expect(page.locator(".skillVersionGroup")).to_have_count(1)
                        await expect(page.locator(".skillLibraryIdentity:visible")).to_have_count(1)
                        await expect(page.locator(".skillLibraryIdentity:visible")).to_contain_text("v0.1.10")
                        await layout(page)
                        await page.screenshot(path=str(output / f"skills-{width}-{theme}.png"), full_page=True)
                        trigger = page.locator(".skillOtherVersions > summary")
                        await trigger.focus()
                        await page.keyboard.press("Enter")
                        await expect(page.locator(".skillLibraryIdentity:visible")).to_have_count(10)
                        await page.locator('.skillLibraryFilters input[type="search"]').fill("0.1.3")
                        await expect(page.locator(".skillLibraryIdentity:visible")).to_have_count(1)
                        await expect(page.locator(".skillLibraryIdentity:visible")).to_contain_text("v0.1.3")
                        await page.goto(f"{url}#/history?project={PROJECT}&run={RUN}")
                        await expect(page.locator(".runPanel h2")).to_have_text("Original archived review")
                        await layout(page)
                        delta = await page.evaluate("""() => Math.abs(document.querySelector('.pageHeader').getBoundingClientRect().right - document.querySelector('.workspaceReading').getBoundingClientRect().right)""")
                        assert delta < 2, delta
                        await page.screenshot(path=str(output / f"detail-{width}-{theme}.png"), full_page=True)
                        assert not errors and not api.unexpected and not api.failures
                        print(f"PASS lists-title-{width}-{theme}", flush=True)
                    finally:
                        await context.close()
            # 日時入力は原 preview 契約に同じ timezone と cron を渡し、書込を一切実行しない。
            entry = url.replace("projects.html", "run-submission.html")
            address = urlsplit(entry)
            api = ScheduleApi(f"{address.scheme}://{address.netloc}")
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            await context.route("**/*", api.route)
            page = await context.new_page()
            try:
                await opened(page, entry)
                await page.locator('[name="timezone"]').fill("Asia/Tokyo")
                await select_option(page.locator('[role="combobox"][data-field-name="recurrence"]'), "weekly")
                await page.locator('[name="recurrence_time"]').fill("09:30")
                await select_option(page.locator('[role="combobox"][data-field-name="recurrence_weekday"]'), "2")
                await previewed(page)
                assert api.previews[-1]["body"]["definition"]["cron_expression"] == "30 9 * * 2"
                assert api.previews[-1]["body"]["definition"]["timezone"] == "Asia/Tokyo"
                await select_option(page.locator('[role="combobox"][data-field-name="recurrence"]'), "custom")
                await expect(page.locator('[name="cron_expression"]')).to_have_value("30 9 * * 2")
                await page.locator('[name="cron_expression"]').fill("*/15 9-17 * * 1-5")
                await expect(page.locator('[role="combobox"][data-field-name="recurrence"]')).to_have_attribute("data-value", "custom")
                assert not api.saves and not api.unexpected
                print("PASS recurrence-presets", flush=True)
            finally:
                await context.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(check(args.url, args.output))
