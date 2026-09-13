"""実行一覧の不変 task 名、失敗時の代替表示と三語・両テーマを検証する。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from check_artifacts import ArtifactApi
from check_projects import PROJECT, RUN, layout
from check_result_references import CONTRACTS
from playwright.async_api import Route, async_playwright, expect

TITLE = 'ログイン仕様書の RV レビュー'


class TitleApi(ArtifactApi):
    """失敗・成功・旧 API の三行を返し、結果の有無と名前を独立させる。"""

    async def respond(self, route: Route) -> None:
        """履歴だけを置き換え、他の API は既存 fixture を使う。"""
        suffix = urlsplit(route.request.url).path.removeprefix(self.prefix)
        if suffix == f'projects/{PROJECT}/runs':
            body = json.loads((CONTRACTS / 'examples/run-history.v1.json').read_text())
            base = body['items'][0]
            failed = {**base, 'run_id': RUN, 'task_title': TITLE, 'status': 'FAILED',
                      'result_summary': None}
            success = {**base, 'task_title': TITLE, 'result_summary': '2 files reviewed: 1 PASS / 1 FAIL'}
            legacy = {**base, 'run_id': '00000000-0000-4000-8000-000000000099',
                      'status': 'CANCELLED', 'result_summary': None}
            legacy.pop('task_title', None)
            await route.fulfill(json={**body, 'items': [failed, success, legacy]})
            return
        await super().respond(route)


async def check(url: str, output: Path) -> None:
    """PC の履歴と Workspace で名前・摘要・技術詳細の分離を確認する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language, fallback in [('ja', 'タスク実行'), ('zh', '任务执行'), ('en', 'Task execution')]:
                for theme in ('light', 'dark'):
                    api = TitleApi(url, language, 'contract')
                    context = await browser.new_context(viewport={'width': 1440, 'height': 900})
                    await context.route('**/*', api.route)
                    page = await context.new_page()
                    errors: list[str] = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    try:
                        await page.goto(f'{url}#/history?project={PROJECT}')
                        await page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                        await expect(page.locator('.historyPrimary strong')).to_have_text([TITLE, TITLE, fallback])
                        await expect(page.locator('.historySummary')).to_have_text('2 files reviewed: 1 PASS / 1 FAIL')
                        await expect(page.locator('.historyTechnical code').first).to_be_hidden()
                        await page.locator('.historyTechnical summary').first.click()
                        await expect(page.locator('.historyTechnical code').first).to_have_text(RUN)
                        await layout(page)
                        await page.screenshot(path=str(output / f'history-{language}-{theme}.png'))
                        await page.goto(f'{url}#/workspace?project={PROJECT}')
                        await expect(page.locator('.workspaceQueue strong')).to_have_text([TITLE, TITLE, fallback])
                        assert not errors and not api.failures and not api.unexpected, (errors, api.failures, api.unexpected)
                        print(f'PASS run titles {language}/{theme}', flush=True)
                    finally:
                        await context.close()
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    target = urlsplit(args.url)
    if target.hostname not in {'127.0.0.1', 'localhost'} or not target.path.endswith('/tests/browser/projects.html'):
        parser.error('Only loopback projects.html is allowed')
    asyncio.run(check(args.url, args.output))
