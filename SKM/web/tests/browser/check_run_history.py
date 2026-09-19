"""実 App の履歴空ページ復帰と詳細の範囲表示を全面 mock API で検証する。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from check_projects import PROJECT, RUN
from check_result_references import ResultApi
from playwright.async_api import Browser, Route, async_playwright, expect


class HistoryApi(ResultApi):
    """初回の次頁ありと次頁取得前の一覧変更を再現し、実データを操作しない。"""

    def __init__(self, url: str, language: str) -> None:
        """既存の実行詳細と無関係な sidebar module を用意する。"""
        super().__init__(url, language, "contract")
        self.with_module = True
        self.with_tasks = True
        self.offsets: list[int] = []

    async def respond(self, route: Route) -> None:
        """Project 履歴だけを差し替え、それ以外は共有 fixture の認証境界に従う。"""
        address = urlsplit(route.request.url)
        suffix = address.path.removeprefix(self.prefix)
        if suffix == f"projects/{PROJECT}/runs" and route.request.method == "GET":
            query = parse_qs(address.query)
            offset = int(query.get("offset", ["0"])[0])
            trashed = query.get("trashed") == ["true"]
            self.offsets.append(offset)
            item = {
                **{key: value for key, value in self.run().items() if key != "idempotent_replay"},
                "task_title": "Saved task title",
                "started_at": "2026-09-09T00:00:00Z",
                "finished_at": "2026-09-09T00:01:00Z",
                "input": {}, "selected_sources": {}, "result_summary": "Saved result summary",
                "result_confidence": None, "result_needs_review": False,
            }
            await route.fulfill(json={
                "items": [item] if offset == 0 and not trashed else [],
                "offset": offset, "limit": int(query.get("limit", ["20"])[0]),
                "has_more": offset == 0 and not trashed,
            })
            return
        await super().respond(route)


async def check(browser: Browser, url: str, language: str, theme: str, output: Path) -> None:
    """空の後続頁から戻り、回収箱と詳細で別スコープを誤表示しないことを確認する。"""
    api = HistoryApi(url, language)
    context = await browser.new_context(viewport={"width": 1366, "height": 768}, locale=language)
    await context.add_init_script(f"localStorage.setItem('skillmind.theme', '{theme}')")
    await context.route("**/*", api.route)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        await page.goto(f"{url}#/history?project={PROJECT}")
        labels = await page.evaluate(
            "async language => (await import('/skillmind/src/lib/i18n/messages.ts')).MESSAGES[language]",
            language,
        )
        history = page.locator(".historyPage")
        await expect(history.locator(".historyItem")).to_have_count(1, timeout=15000)
        await history.get_by_role("button", name=labels["runHistory"]["next"], exact=True).click()
        await expect(history.get_by_text(labels["runHistory"]["emptyPage"], exact=True)).to_be_visible()
        await expect(history.get_by_role("button", name=labels["runHistory"]["next"], exact=True)).to_be_disabled()
        await history.get_by_role("button", name=labels["runHistory"]["previous"], exact=True).click()
        await expect(history.locator(".historyItem")).to_have_count(1)
        assert 20 in api.offsets and api.offsets[-1] == 0, api.offsets
        await history.get_by_role("button", name=labels["fileManagement"]["trash"], exact=True).click()
        await expect(history.get_by_text(labels["runHistory"]["emptyTrash"], exact=True)).to_be_visible()
        await expect(history.get_by_text(labels["runHistory"]["empty"], exact=True)).to_have_count(0)
        await page.screenshot(path=str(output / f"history-trash-{language}-{theme}.png"))
        await page.goto(f"{url}#/history?project={PROJECT}&run={RUN}")
        await expect(page.locator(".runPanel .statusBadge")).to_be_visible()
        await expect(page.locator(".pageHeader .scopeBadge")).to_have_text(labels["workspace"]["projectWideScope"])
        await expect(page.locator(".pageHeader")).not_to_contain_text("Fixture module")
        await page.screenshot(path=str(output / f"history-detail-{language}-{theme}.png"))
        assert not errors and not api.failures and not api.unexpected, (errors, api.failures, api.unexpected)
        assert not api.mutations(), api.mutations()
    finally:
        await context.close()


async def main() -> None:
    """三語・両テーマで通常の browser 操作を実施する。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5173/skillmind/")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ("zh", "ja", "en"):
                for theme in ("light", "dark"):
                    await check(browser, args.url, language, theme, args.output)
                    print(f"PASS {language} {theme}", flush=True)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
