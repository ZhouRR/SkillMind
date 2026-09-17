"""同名 task key と遅延 module 選択でも、表示対象と送信先を一致させる。"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from itertools import product
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from check_run_submission import ApiFixture, PROJECT, VERSION, task_catalog
from playwright.async_api import async_playwright, expect

REVIEW_VERSION = '00000000-0000-4000-8000-000000000062'
MODULE = '00000000-0000-4000-8000-000000000070'
TARGET = f'{VERSION}::execute'


class LaunchApi(ApiFixture):
    """二つの Skill は同じ execute を持つ。POST は記録のみで実行しない。"""

    def __init__(self, origin):
        """計画とレビューを分離した公開 catalog を作る。"""
        super().__init__(origin, [])
        plan = task_catalog()['tasks'][0]
        plan.update(task_key='execute', title='Planning fixture', skill_name='Planner')
        plan['readiness']['requirements'] = []
        review = deepcopy(plan)
        review.update(skill_version_id=REVIEW_VERSION, skill_id=REVIEW_VERSION,
                      skill_name='Reviewer', title='Review fixture',
                      task_id='00000000-0000-4000-8000-000000000031')
        self.catalog = {'tasks': [plan, review]}

    async def route(self, route):
        """既存の通信制限を保ち、module 一覧だけを拡張する。"""
        address = urlsplit(route.request.url)
        if f'{address.scheme}://{address.netloc}' == self.origin and address.path.endswith('/modules'):
            await route.fulfill(json={'modules': [{
                'module_id': MODULE, 'project_id': PROJECT, 'name': 'Review module',
                'description': 'Synthetic fixture', 'skills': [{
                    'skill_version_id': REVIEW_VERSION, 'skill_id': REVIEW_VERSION,
                    'skill_key': 'review-fixture', 'skill_name': 'Reviewer',
                    'version': '1.0.0', 'sort_order': 0,
                }], 'created_at': '2026-09-17T00:00:00Z', 'updated_at': '2026-09-17T00:00:00Z',
            }]})
        else:
            await super().route(route)

    async def create(self, route):
        """HTTP 本文を観測し、実 Run 作成・SSE へは進めない。"""
        self.posts.append(route.request.post_data_json)
        self.received.set()
        await route.fulfill(status=409, json={
            'type': 'about:blank', 'title': 'Fixture stop', 'status': 409,
            'detail': 'No real execution', 'code': 'idempotency_conflict',
        })


async def check(url, output):
    """三語・両テーマで遅延 module・手動選択・失効リンクを検証する。"""
    output.mkdir(parents=True, exist_ok=True)
    address = urlsplit(url)
    origin = f'{address.scheme}://{address.netloc}'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language, theme in product(('ja', 'zh', 'en'), ('light', 'dark')):
                api = LaunchApi(origin)
                context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
                await context.add_init_script(f"""
                    localStorage.setItem('skillmind.theme', '{theme}');
                    document.addEventListener('DOMContentLoaded', () => {{
                        document.documentElement.dataset.theme = '{theme}';
                    }});
                """)
                await context.route('**/*', api.route)
                page = await context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                try:
                    await page.goto(f'{url}?{urlencode({"task": TARGET})}')
                    await page.wait_for_function('Boolean(window.updateSubmissionTestContext)')
                    await page.evaluate('(language) => window.updateSubmissionTestContext({language})', language)
                    dialog = page.get_by_role('dialog')
                    choice = dialog.locator('select').first
                    await expect(dialog).to_be_visible()
                    await expect(choice).to_have_value(TARGET)
                    # App の既定 module が task/link より遅れて届く状況を再現する。
                    await page.evaluate('(moduleId) => window.updateSubmissionTestContext({moduleId})', MODULE)
                    await expect(page.locator('.shellHeader, .pageHeader').first).to_contain_text('Review module')
                    await expect(choice).to_have_value(TARGET)
                    await expect(choice.locator('option:checked')).to_contain_text('Planning fixture')
                    # 同じ task_key を手動で選び直しても精確 version が切り替わる。
                    await choice.select_option(f'{REVIEW_VERSION}::execute')
                    await page.evaluate('window.updateSubmissionTestContext({moduleId: ""})')
                    await expect(choice.locator('option')).to_have_count(2)
                    await choice.select_option(TARGET)
                    await page.evaluate('(moduleId) => window.updateSubmissionTestContext({moduleId})', MODULE)
                    await expect(choice).to_have_value(TARGET)
                    await page.screenshot(path=str(output / f'launch-{language}-{theme}.png'))
                    await dialog.locator('button[type=submit]').click()
                    await asyncio.wait_for(api.received.wait(), timeout=5)
                    assert api.posts[0]['skill_version_id'] == VERSION, api.posts
                    assert api.posts[0]['task_key'] == 'execute', api.posts

                    # 失効した明示リンクは最初の task へ代替せず、利用者の選び直しを待つ。
                    missing = '00000000-0000-4000-8000-000000000099::execute'
                    await page.goto(f'{url}?{urlencode({"task": missing})}')
                    await page.wait_for_function('Boolean(window.updateSubmissionTestContext)')
                    await page.evaluate('(language) => window.updateSubmissionTestContext({language})', language)
                    await expect(dialog).to_be_visible()
                    await expect(choice).to_have_value('')
                    await expect(dialog.get_by_role('alert')).to_be_visible()
                    await expect(dialog.locator('button[type=submit]')).to_be_disabled()
                    assert len(api.posts) == 1
                    await choice.select_option(TARGET)
                    await expect(dialog.locator('button[type=submit]')).to_be_enabled()

                    # 再認可時に精確版が一覧から消えても、同名 execute へ草稿を移さない。
                    api.catalog['tasks'] = api.catalog['tasks'][1:]
                    await page.evaluate('window.updateSubmissionTestContext({csrfToken: "d".repeat(32)})')
                    await expect(dialog.get_by_role('alert')).to_be_visible()
                    await expect(choice).to_have_value('')
                    await expect(dialog.locator('button[type=submit]')).to_be_disabled()
                    assert len(api.posts) == 1
                    assert not api.unexpected, api.unexpected
                    assert not errors, errors
                    print(f'PASS launch identity {language} {theme}', flush=True)
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
    if target.scheme != 'http' or target.hostname not in {'localhost', '127.0.0.1'} or not target.path.endswith('/tests/browser/run-submission.html'):
        parser.error('Only the loopback run-submission.html mock harness is allowed')
    asyncio.run(check(args.url, args.output))
