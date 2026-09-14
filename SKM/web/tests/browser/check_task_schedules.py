"""Task 一覧だけで検索・予定設定・元予定管理ができることを実 App / mock HTTP で検証する。"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from check_projects import PROJECT, layout, messages
from playwright.async_api import async_playwright, expect
from schedule_management_fixture import ScheduleManagementApi, schedule


def configure(api: ScheduleManagementApi) -> None:
    """一 task 一状態と複数状態の task を合成し、外部実行を伴わない。"""
    base = api.catalog["tasks"][0]
    api.catalog["tasks"] = []
    api.records = []
    for index, state in enumerate(
        ["UNCONFIGURED", "ACTIVE", "PAUSED", "COMPLETED", "ERROR", "ARCHIVED"]
    ):
        task = deepcopy(base)
        task.update(
            title=f"Task {state}",
            task_key=f"task-{index}",
            task_id=f"00000000-0000-4000-8000-{2000 + index:012}",
        )
        api.catalog["tasks"].append(task)
        if index:
            record = schedule(index)
            record.update(name=f"Plan {state}", task_key=task["task_key"], status=state)
            api.records.append(record)
    mixed = deepcopy(api.records[0])
    mixed.update(schedule_id="00000000-0000-4000-8000-000000009001", status="PAUSED")
    api.records.append(mixed)


async def check(url: str, output: Path) -> None:
    """Loopback の合成 API だけに接続し、実際の schedule / Run を作らない。"""
    if urlsplit(url).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Only loopback fixtures are supported")
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            for language in ["ja", "zh", "en"]:
                for width in [390, 1440]:
                    api = ScheduleManagementApi(url, language)
                    configure(api)
                    context = await browser.new_context(viewport={"width": width, "height": 1000})
                    await context.route("**/*", api.route)
                    page = await context.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                    try:
                        # 旧 bookmark も Task 一覧へ戻り、独立の画面/Tab を作らない。
                        await page.goto(f"{url}#/schedules?project={PROJECT}")
                        await expect(page.locator("[data-task-filters]")).to_be_visible()
                        await expect(page.locator("[data-task-card]")).to_have_count(6)
                        assert await page.locator('a[href^="#/schedules"]').count() == 0
                        assert await page.locator("[role=tab]").count() == 0
                        labels = await messages(page, language)
                        form = page.locator("[data-task-filters]")
                        for state, count in [
                            ("UNCONFIGURED", 1),
                            ("ACTIVE", 1),
                            ("PAUSED", 2),
                            ("COMPLETED", 1),
                            ("ERROR", 1),
                            ("ARCHIVED", 1),
                        ]:
                            await page.locator("[data-task-status]").select_option(state)
                            await form.locator("button[type=submit]").click()
                            await expect(page.locator("[data-task-card]")).to_have_count(count)
                        await page.locator("[data-task-status]").select_option("PAUSED")
                        await page.locator("[data-task-search]").fill("TASK-2")
                        await form.locator("button[type=submit]").click()
                        await expect(page.locator("[data-task-card]")).to_have_count(1)
                        card = page.locator("[data-task-card]")
                        assert (
                            await card.locator(".taskCardFacts dd").nth(1).inner_text()
                            != labels["tasks"]["noSchedule"]
                        )
                        await page.locator("[data-task-search]").fill("does-not-match")
                        await form.locator("button[type=submit]").click()
                        await expect(page.locator("[data-task-card]")).to_have_count(0)
                        await expect(page.get_by_text(labels["tasks"]["noMatches"])).to_be_visible()
                        await page.locator("[data-task-search]").fill("")
                        await page.locator("[data-task-status]").select_option("")
                        await form.locator("button[type=submit]").click()
                        await expect(page.locator("[data-task-card]")).to_have_count(6)
                        await layout(page)
                        if width == 1440:
                            tops = await form.locator('input,select,button').evaluate_all(
                                '(els)=>els.map(e=>e.getBoundingClientRect().top)'
                            )
                            assert max(tops) - min(tops) <= 2, tops

                        await page.screenshot(
                            path=str(output / f"tasks-{language}-{width}.png"), full_page=True
                        )
                        # 元の ACTIVE を明示 GET して編集し、status 変更後も選択を保持する。
                        card = page.locator("[data-task-card]").nth(1)
                        await card.locator("summary").click()
                        await card.locator("[data-task-schedule-manage]").first.click()
                        await expect(page.locator("[data-schedule-edit]")).to_be_enabled()
                        await page.locator("[data-schedule-edit]").click()
                        await page.locator("[data-schedule-editor] input[name=name]").fill(
                            "Edited plan"
                        )
                        await page.locator("[data-schedule-preview]").click()
                        await expect(page.locator("[data-schedule-confirm]")).to_be_visible()
                        await page.locator("[data-schedule-confirm]").check()
                        await page.locator("[data-schedule-editor] button[type=submit]").click()
                        await expect(page.locator("[data-schedule-editor]")).to_be_hidden()
                        await expect(
                            page.locator("[data-task-schedule-details] h3").first
                        ).to_have_text("Edited plan")
                        await page.locator("[data-schedule-status=PAUSED]").click()
                        await expect(page.locator("[data-schedule-status=ACTIVE]")).to_be_visible()
                        await expect(page.locator("[data-task-schedule-close]")).to_be_enabled()
                        await page.locator("[data-task-schedule-close]").click()
                        # 未設定 Task の既存設定入口を利用する。
                        await (
                            page.locator("[data-task-card]")
                            .first.get_by_role(
                                "button", name=labels["tasks"]["addSchedule"], exact=True
                            )
                            .click()
                        )
                        await expect(page.locator("[data-schedule-form]")).to_be_visible()
                        await page.locator("[data-schedule-preview]").click()
                        await expect(page.locator("[data-schedule-confirm]")).to_be_visible()
                        await page.locator("[data-schedule-confirm]").check()
                        await page.locator("[data-schedule-form] button[type=submit]").click()
                        await expect(page.locator("[data-schedule-form]")).to_have_count(0)
                        await expect(page.locator("[data-task-card]")).to_have_count(6)
                        assert len(api.writes) == 3
                        if language == "ja" and width == 1440:
                            # 応答喪失でも検索で元の編集 owner を捨てず、勝手に再送しない。
                            await page.locator("[data-task-card]").first.locator("summary").click()
                            await (
                                page.locator("[data-task-card]")
                                .first.locator("[data-task-schedule-manage]")
                                .click()
                            )
                            await expect(page.locator("[data-schedule-edit]")).to_be_enabled()
                            await page.locator("[data-schedule-edit]").click()
                            await page.locator("[data-schedule-preview]").click()
                            await expect(page.locator("[data-schedule-confirm]")).to_be_visible()
                            await page.locator("[data-schedule-confirm]").check()
                            api.actions = ["drop"]
                            await page.locator("[data-schedule-editor] button[type=submit]").click()
                            await expect(
                                page.locator("[data-schedule-review=unknown]")
                            ).to_be_visible()
                            await (
                                page.get_by_role("dialog")
                                .get_by_role("button", name=labels["elements"]["close"], exact=True)
                                .click()
                            )
                            await expect(
                                page.locator("[data-task-schedule-close]")
                            ).to_be_disabled()
                            await page.locator("[data-task-search]").fill("no-matches")
                            await form.locator("button[type=submit]").click()
                            await expect(page.locator("[data-task-card]")).to_have_count(0)
                            await expect(page.locator("[data-schedule-reopen]")).to_be_visible()
                            assert len(api.writes) == 4
                        assert not errors and not api.failures and not api.unexpected, (
                            errors,
                            api.failures,
                            api.unexpected,
                        )
                        print(f"PASS tasks schedules {language} {width}", flush=True)
                    except Exception:
                        await page.screenshot(
                            path=str(output / f"FAILED-{language}-{width}.png"), full_page=True
                        )
                        raise
                    finally:
                        await context.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(check(args.url, args.output))
