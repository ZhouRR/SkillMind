"""古い下書き応答が新しい解釈要求の UUID を消さないことを実 App で検証する。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from check_accounts import CSRF, ResponseGate
from check_interpretation_requests import ADJUSTMENT, RECEIPT, RESULT, InterpretationApi
from check_projects import PROJECT, messages
from check_skill_library_delete import VERSION
from playwright.async_api import Route, async_playwright, expect


class DelayedDraftApi(InterpretationApi):
    """保存済み解釈を復元し、下書きの成功応答だけを保留する。"""

    def __init__(self, url: str, language: str) -> None:
        """下書きと新しい調整を区別する独立した原 request を持つ。"""
        super().__init__(url, language, 'lost-response')
        self.original = str(uuid4())
        self.state = 'SUCCEEDED'
        self.draft_gate = ResponseGate()
        self.drafts = 0

    async def respond(self, route: Route) -> None:
        """HTTP の abort は Server 保存を巻き戻す証拠にせず、同じ応答を返す。"""
        suffix = urlsplit(route.request.url).path.removeprefix(self.prefix)
        if suffix == f'skill-interpretations/{RESULT}/draft':
            assert route.request.method == 'POST'
            assert route.request.headers.get('x-csrf-token') == CSRF
            self.drafts += 1
            self.draft_gate.received.set()
            await self.draft_gate.release.wait()
            await route.fulfill(status=201, json=VERSION)
            self.draft_gate.returned.set()
            return
        await super().respond(route)


async def check(url: str, output: Path) -> None:
    """同一画面と離頁後の両方で新 UUID を保持し、reload は元 GET だけで再開する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ('ja', 'zh', 'en'):
                for remount in (None, False, True):
                    api = DelayedDraftApi(url, language)
                    context = await browser.new_context(viewport={'width': 1440, 'height': 900})
                    await context.route('**/*', api.route)
                    await context.add_init_script('''if (!sessionStorage.getItem(%s)) sessionStorage.setItem(%s,%s);
                        const nativeFetch=window.fetch;
                        window.fetch=(url,init={})=>nativeFetch(url,String(url).endsWith('/draft')
                          ?{...init,signal:undefined}:init);
                        ''' % (json.dumps(RECEIPT), json.dumps(RECEIPT), json.dumps(api.original)))
                    page = await context.new_page()
                    errors: list[str] = []
                    page.on('pageerror', lambda error, captured=errors: captured.append(str(error)))
                    try:
                        await page.goto(f'{url}#/skills?project={PROJECT}')
                        labels = (await messages(page, language))['skills']
                        panel = page.locator('.interpretationPanel')
                        await expect(panel).to_be_visible()
                        await panel.get_by_role('button', name=labels['createDraftFromThis'], exact=True).click()
                        await asyncio.wait_for(api.draft_gate.received.wait(), 5)
                        if remount is None:
                            # 通常の下書き成功では元 receipt を閉じ、復元が永続的に残らない。
                            api.draft_gate.release.set()
                            await asyncio.wait_for(api.draft_gate.returned.wait(), 5)
                            await expect(page.locator('.skillVersionDetail')).to_be_visible()
                            assert await page.evaluate('(key)=>sessionStorage.getItem(key)', RECEIPT) is None
                            assert api.drafts == 1 and api.posts == 0 and api.adjustments == 0
                            assert not errors and not api.failures and not api.unexpected
                            print(f'PASS Skill confirmed draft cleanup {language}', flush=True)
                            continue
                        if remount:
                            await page.evaluate('(hash)=>{window.location.hash=hash}', f'#/projects?project={PROJECT}')
                            await page.locator('[data-project-form]').wait_for()
                            await page.evaluate('(hash)=>{window.location.hash=hash}', f'#/skills?project={PROJECT}')
                            await expect(panel).to_be_visible()
                        await page.locator('.adjustForm textarea').fill(ADJUSTMENT)
                        await page.locator('.adjustForm button[type="submit"]').click()
                        confirm = page.get_by_role('button', name=labels['confirmInterpretation'], exact=True)
                        await expect(confirm).to_be_visible()
                        new_request = await page.evaluate('(key)=>sessionStorage.getItem(key)', RECEIPT)
                        assert new_request == api.original and api.adjustments == 1
                        api.draft_gate.release.set()
                        await asyncio.wait_for(api.draft_gate.returned.wait(), 5)
                        # 応答 callback と React commit の後も、新要求の確認画面だけを保つ。
                        await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                        assert await page.evaluate('(key)=>sessionStorage.getItem(key)', RECEIPT) == new_request
                        await expect(confirm).to_be_visible()
                        await expect(page.locator('.skillVersionDetail')).to_have_count(0)
                        await page.reload()
                        await expect(confirm).to_be_visible()
                        assert api.reads[-1] == new_request and api.posts == 0 and api.adjustments == 1 and api.drafts == 1
                        assert not errors and not api.failures and not api.unexpected, (errors, api.failures, api.unexpected)
                        await page.screenshot(path=str(output / f'skill-lifecycle-{language}-{remount}.png'))
                        print(f'PASS Skill request ownership {language}, remount={remount}', flush=True)
                    finally:
                        api.draft_gate.release.set()
                        await context.close()
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    address = urlsplit(args.url)
    if address.scheme != 'http' or address.hostname not in {'127.0.0.1', 'localhost'} or not address.path.endswith('/tests/browser/projects.html'):
        parser.error('Only the loopback App mock harness is allowed')
    asyncio.run(check(args.url, args.output))
