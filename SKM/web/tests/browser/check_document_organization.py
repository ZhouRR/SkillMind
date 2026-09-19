"""文書整理の空態・拒否診断・草稿保持・読取専用と狭幅を mock API で回帰する。"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from check_accounts import CSRF
from check_document_management import document
from check_projects import ARCHIVED, PROJECT, ProjectsApi, messages
from playwright.async_api import Route, async_playwright, expect


class OrganizationApi(ProjectsApi):
    """操作は架空の一文書に限定し、実 storage や外部通信へ到達させない。"""

    def __init__(self, url: str, language: str) -> None:
        super().__init__(url, language)
        self.row = document(PROJECT)
        self.row['name'] = 'テスト仕様書とレビュー結果の長い名称を省略せず確認する文書-' * 3 + '.json'
        self.row['mime'] = 'application/json'
        self.trashed = False
        self.refusal: str | None = None
        self.operations: list[str] = []
        self.reads = 0

    async def respond(self, route: Route) -> None:
        """現在一覧/回収箱と原 ID の整理応答を返し、未知 API は共通拒否へ渡す。"""
        request = route.request
        address = urlsplit(request.url)
        parts = address.path.removeprefix(self.prefix).split('/')
        if len(parts) < 3 or parts[0] != 'projects':
            await super().respond(route)
            return
        if parts[2] == 'document-folders':
            assert request.method == 'GET'
            await route.fulfill(json={'folders': ['specs', 'empty']})
        elif parts[2] == 'documents' and len(parts) == 3:
            self.reads += 1
            wanted = 'trashed=true' in address.query
            row = {**self.row, 'project_id': parts[1]}
            await route.fulfill(json={'documents': [row] if wanted == self.trashed else []})
        elif parts[2] == 'document-operations':
            assert request.method == 'POST' and request.headers['x-csrf-token'] == CSRF
            body = request.post_data_json
            self.operations.append(body['action'])
            if self.refusal:
                await route.fulfill(status=409, json={
                    'status': 409, 'code': self.refusal, 'title': 'Fixture refusal',
                    'detail': 'Private reference text must never be displayed',
                })
                return
            if body['action'] == 'MOVE':
                change = body['changes'][0]
                assert change['document_id'] == self.row['document_id']
                self.row.update(folder=change['folder'], name=change['name'])
            elif body['action'] in {'TRASH', 'RESTORE'}:
                self.trashed = body['action'] == 'TRASH'
            else:
                raise AssertionError('Unexpected mutation')
            await route.fulfill(status=204)
        else:
            await super().respond(route)


async def check(url: str, output: Path) -> None:
    """三語・二色・PC/狭幅で描画を確認し、同じ操作 owner の回復を検証する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for language in ('ja', 'zh', 'en'):
            for theme in ('dark', 'light'):
                api = OrganizationApi(url, language)
                context = await browser.new_context(viewport={'width': 1440, 'height': 900})
                await context.route('**/*', api.route)
                await context.add_init_script(f"localStorage.setItem('skillmind.theme', '{theme}')")
                page = await context.new_page()
                errors: list[str] = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(f'{url}#/documents?project={PROJECT}')
                await expect(page.locator('.documentItem')).to_have_count(1)
                labels = await messages(page, language)
                m, d = labels['fileManagement'], labels['documentsPanel']
                for width in (1440, 1366, 390):
                    await page.set_viewport_size({'width': width, 'height': 900})
                    await page.screenshot(path=str(output / f'{language}-{theme}-{width}.png'), full_page=True)
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    item = page.locator('.documentItem')
                    bounds = await item.evaluate('''el => {
                      const name = el.querySelector('.documentInfo strong').getBoundingClientRect();
                      const actions = el.querySelector('.documentActions').getBoundingClientRect();
                      return {name: {r:name.right,b:name.bottom}, actions:{l:actions.left,t:actions.top}};
                    }''')
                    assert (bounds['name']['r'] <= bounds['actions']['l'] + 1
                            or bounds['name']['b'] <= bounds['actions']['t'] + 1), bounds
                await page.set_viewport_size({'width': 1440, 'height': 900})
                await page.get_by_role('button', name=m['trash'], exact=True).click()
                await expect(page.get_by_text(m['emptyTrash'], exact=True)).to_be_visible()
                refresh = page.get_by_role('button', name=d['refresh'], exact=True)
                before = api.reads
                await refresh.click()
                await expect(refresh).to_be_enabled()
                assert api.reads > before and not api.operations
                await page.get_by_role('button', name=m['active'], exact=True).click()
                row = page.locator('.documentItem')
                await expect(row).to_have_count(1)
                await row.get_by_role('button', name=f"{m['rename']} / {m['move']}", exact=True).click()
                dialog = page.get_by_role('dialog')
                await dialog.get_by_label(m['name'], exact=True).fill('renamed.json')
                api.refusal = 'document_conflict'
                await dialog.get_by_role('button', name=m['save'], exact=True).click()
                await expect(dialog.get_by_role('alert')).to_have_text(m['failure'])
                await expect(dialog.get_by_label(m['name'], exact=True)).to_have_value('renamed.json')
                assert api.operations == ['MOVE']
                api.refusal = None
                await dialog.get_by_role('button', name=m['save'], exact=True).click()
                await expect(dialog).to_have_count(0)
                await expect(row).to_contain_text('renamed.json')
                api.refusal = 'document_references_unavailable'
                await row.get_by_role('button', name=m['trashAction'], exact=True).click()
                await page.get_by_role('dialog').get_by_role('button', name=m['trashAction'], exact=True).click()
                await expect(page.get_by_role('alert')).to_have_text(d['failures']['referencesUnavailable'])
                assert 'Private reference' not in await page.locator('body').inner_text()
                assert api.operations == ['MOVE', 'MOVE', 'TRASH']
                api.refusal = None
                await row.get_by_role('button', name=m['trashAction'], exact=True).click()
                await page.get_by_role('dialog').get_by_role('button', name=m['trashAction'], exact=True).click()
                await expect(page.get_by_role('button', name=m['trash'], exact=True)).to_be_enabled()
                await page.get_by_role('button', name=m['trash'], exact=True).click()
                await expect(row).to_have_count(1)
                await row.get_by_role('button', name=m['restore'], exact=True).click()
                await page.get_by_role('dialog').get_by_role('button', name=m['restore'], exact=True).click()
                await expect(page.get_by_text(m['emptyTrash'], exact=True)).to_be_visible()
                await page.get_by_role('textbox', name=m['search'], exact=True).fill('unmatched')
                await expect(page.get_by_text(m['noMatches'], exact=True)).to_be_visible()
                await page.goto(f'{url}#/documents?project={ARCHIVED}')
                await expect(page.locator('.documentItem')).to_have_count(1)
                await expect(page.locator('input[type=file]').first).to_be_disabled()
                await expect(page.get_by_role('button', name=m['trash'], exact=True)).to_be_enabled()
                await page.get_by_role('button', name=m['trash'], exact=True).click()
                await expect(page.get_by_text(m['emptyTrash'], exact=True)).to_be_visible()
                await page.get_by_role('button', name=m['active'], exact=True).click()
                await expect(page.locator('.documentItem').get_by_role('button', name=m['trashAction'], exact=True)).to_be_disabled()
                assert not errors and not api.failures and not api.unexpected, (errors, api.failures, api.unexpected)
                await context.close()
        await browser.close()
    print('Document organization: 18 layout views and 6 action/read-only regressions passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:5173/skillmind/tests/browser/projects.html')
    parser.add_argument('--output', type=Path, default=Path('/tmp/skillmind-document-organization'))
    args = parser.parse_args()
    asyncio.run(check(args.url, args.output))
