"""三言語の実 Key UI を mock HTTP で検証し、秘密保存や自動再発行を防ぐ。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from check_accounts import ACTOR, CSRF, NOW, AccountsApi
from playwright.async_api import Route, async_playwright, expect


class KeyApi(AccountsApi):
    """外部へ接続せず、応答喪失後にも発行事実が存在する API を模擬する。"""
    def __init__(self, url: str, language: str) -> None:
        super().__init__(url, role='ADMIN', language=language)
        self.keys = []
        self.token = 'skm1.' + 'A' * 43
        self.creates = 0
        self.lose = False

    async def respond(self, route: Route) -> None:
        suffix = urlsplit(route.request.url).path.removeprefix(self.prefix)
        if not suffix.startswith('api-keys'):
            return await super().respond(route)
        request = route.request
        if request.method == 'GET':
            return await route.fulfill(json={'items': self.keys})
        assert request.headers.get('x-csrf-token') == CSRF
        if suffix == 'api-keys':
            self.creates += 1
            record = dict(id=str(uuid4()), name=request.post_data_json['name'], key_prefix=self.token[:13], created_by=ACTOR, created_at=NOW, last_used_at=None, revoked_at=None)
            self.keys.insert(0, record)
            if self.lose:
                return await route.abort()
            return await route.fulfill(status=201, json={'api_key': record, 'token': self.token})
        record = next(k for k in self.keys if k['id'] == suffix.split('/')[1])
        record['revoked_at'] = NOW
        return await route.fulfill(json=record)


async def main() -> None:
    """作成、秘密の消去、失効、未知結果の手動確認を実 React で検証する。"""
    url = 'http://127.0.0.1:5177/skillmind/tests/browser/accounts.html'
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for language in ['ja', 'zh', 'en']:
            for width in [390, 1440]:
                api = KeyApi(url, language)
                context = await browser.new_context(viewport={'width': width, 'height': 1000})
                await context.route('**/*', api.route)
                page = await context.new_page()
                errors=[]
                page.on('pageerror',lambda error: errors.append(str(error)))
                await page.goto(f'{url}?app=1#/accounts')
                panel = page.locator('.apiKeyPanel')
                await panel.locator('..').locator('summary').click()
                form=panel.locator('form')
                await form.locator('input').fill('Example app')
                await form.locator('button').click()
                secret=panel.locator('.apiKeySecret input')
                await expect(secret).to_have_value(api.token)
                stored=await page.evaluate('JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)])')
                assert api.token not in stored
                await panel.locator('.apiKeySecret button').click()
                await expect(secret).to_have_count(0)
                row=panel.locator('.apiKeyRow').first
                await row.locator('button').click()
                await row.locator('.apiKeyRevoke button').first.click()
                await expect(row.locator('button')).to_have_count(0)
                api.lose=True
                await form.locator('input').fill('Lost response')
                await form.locator('button').click()
                await expect(panel.locator('[role=alert]')).to_be_visible()
                assert api.creates == 2
                await expect(form.locator('button')).to_be_disabled()
                await panel.locator('.buttonRow button').first.click()
                await expect(panel.locator('.apiKeyRow')).to_have_count(2)
                await panel.locator('.buttonRow button').last.click()
                assert api.creates == 2
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                assert not errors and not api.failures and not api.unexpected, (errors,api.failures,api.unexpected)
                if language=='ja' and width==1440:
                    await page.screenshot(path='/tmp/skm-api-keys-20260914/api-key-ui.png',full_page=True)
                await context.close()
        await browser.close()
    print('API key UI: 6 locale/width combinations passed; create/revoke/unknown and memory-only secret')


if __name__ == '__main__':
    asyncio.run(main())
