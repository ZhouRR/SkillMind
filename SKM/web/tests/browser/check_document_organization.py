"""文書整理の空態・拒否診断・草稿保持・読取専用と狭幅を mock API で回帰する。"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from check_accounts import CSRF
from check_document_management import SECOND, DocumentsApi, document, row_menu
from check_projects import ARCHIVED, PROJECT, ProjectsApi, messages
from playwright.async_api import Route, async_playwright, expect


class OrganizationApi(ProjectsApi):
    """操作は架空の一文書に限定し、実 storage や外部通信へ到達させない。"""

    def __init__(self, url: str, language: str) -> None:
        super().__init__(url, language)
        self.row = document(PROJECT)
        self.row['name'] = 'テスト仕様書とレビュー結果の長い名称を省略せず確認する文書-' * 3 + '.json'
        self.row['mime'] = 'application/json'
        self.row['folder'] = 'specs/nested/deep'
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
            await route.fulfill(json={'folders': ['specs', 'specs/nested/deep', 'empty/nested']})
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


async def directory_layout(page) -> None:
    """汎用 input 幅が目录名を押し出さず、checkbox と操作入口が行内に収まる。"""
    rows = await page.locator('.docFolder > summary').evaluate_all('''nodes => nodes.map(row => {
      const checkbox = row.querySelector('input[type="checkbox"]').getBoundingClientRect();
      const name = row.querySelector('strong').getBoundingClientRect();
      const action = row.querySelector('[aria-haspopup="menu"]').getBoundingClientRect();
      const bounds = row.getBoundingClientRect();
      return {width:checkbox.width,height:checkbox.height,nameWidth:name.width,
        nameRight:name.right,actionLeft:action.left,actionRight:action.right,right:bounds.right};
    })''')
    assert rows
    for row in rows:
        assert row['width'] == row['height'] == 16 and row['nameWidth'] > 0, row
        assert row['nameRight'] <= row['actionLeft'] + 1, row
        assert row['actionRight'] <= row['right'] + 1, row


async def check(url: str, output: Path) -> None:
    """三語・二色・PC/狭幅で描画を確認し、同じ操作 owner の回復を検証する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for language in ('ja', 'zh', 'en'):
            for theme in ('dark', 'light'):
                print(f'Organization {language}/{theme}', flush=True)
                api = OrganizationApi(url, language)
                context = await browser.new_context(viewport={'width': 1440, 'height': 900})
                await context.route('**/*', api.route)
                await context.add_init_script(f"if (window === window.top) localStorage.setItem('skillmind.theme', '{theme}')")
                page = await context.new_page()
                errors: list[str] = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(f'{url}#/documents?project={PROJECT}')
                await expect(page.locator('.documentItem')).to_have_count(1)
                labels = await messages(page, language)
                m, d = labels['fileManagement'], labels['documentsPanel']
                search = page.get_by_role('textbox', name=m['search'], exact=True)
                await search.fill('テスト仕様書')
                await expect(page.locator('.documentItem')).to_be_visible()
                await search.fill('empty/nested')
                await expect(page.locator('.docFolder > summary strong', has_text='nested')).to_be_visible()
                await expect(page.get_by_role('checkbox', name=f"{m['selectFolder']}: nested", exact=True)).to_be_disabled()
                await directory_layout(page)
                await expect(page.get_by_text(m['noMatches'], exact=True)).to_have_count(0)
                await search.fill('テスト仕様書')
                for width in (1440, 1366, 390):
                    await page.set_viewport_size({'width': width, 'height': 900})
                    await directory_layout(page)
                    await page.screenshot(path=str(output / f'{language}-{theme}-{width}.png'), full_page=True)
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    item = page.locator('.documentItem')
                    bounds = await item.evaluate('''el => {
                      const name = el.querySelector('.documentName').getBoundingClientRect();
                      const actions = el.querySelector('.documentActions').getBoundingClientRect();
                      return {name: {r:name.right,b:name.bottom}, actions:{l:actions.left,t:actions.top}};
                    }''')
                    assert (bounds['name']['r'] <= bounds['actions']['l'] + 1
                            or bounds['name']['b'] <= bounds['actions']['t'] + 1), bounds
                await page.set_viewport_size({'width': 1440, 'height': 900})
                await search.fill('')
                await page.get_by_role('button', name=m['trash'], exact=True).click()
                await expect(page.get_by_text(m['emptyTrash'], exact=True)).to_be_visible()
                refresh = page.get_by_role('button', name=d['refresh'], exact=True)
                before = api.reads
                await refresh.click()
                await expect(refresh).to_be_enabled()
                assert api.reads > before and not api.operations
                await page.get_by_role('button', name=m['active'], exact=True).click()
                await search.fill('specs')
                row = page.locator('.documentItem')
                await expect(row).to_have_count(1)
                actions = await row_menu(page, row)
                await expect(actions.get_by_role('menuitem', name=d['download'], exact=True)).to_have_attribute('download', api.row['name'])
                await actions.get_by_role('menuitem', name=f"{m['rename']} / {m['move']}", exact=True).click()
                dialog = page.get_by_role('dialog')
                await dialog.locator('form').get_by_role('button', name=m['cancel'], exact=True).click()
                await expect(dialog).to_have_count(0)
                await expect(row.locator('button[aria-haspopup="menu"]')).to_be_focused()
                assert not api.operations
                actions = await row_menu(page, row)
                await actions.get_by_role('menuitem', name=f"{m['rename']} / {m['move']}", exact=True).click()
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
                actions = await row_menu(page, row)
                await actions.get_by_role('menuitem', name=m['trashAction'], exact=True).click()
                await page.get_by_role('dialog').get_by_role('button', name=m['trashAction'], exact=True).click()
                await expect(page.get_by_role('alert')).to_have_text(d['failures']['referencesUnavailable'])
                assert 'Private reference' not in await page.locator('body').inner_text()
                assert api.operations == ['MOVE', 'MOVE', 'TRASH']
                api.refusal = None
                actions = await row_menu(page, row)
                await actions.get_by_role('menuitem', name=m['trashAction'], exact=True).click()
                await page.get_by_role('dialog').get_by_role('button', name=m['trashAction'], exact=True).click()
                await expect(page.get_by_role('button', name=m['trash'], exact=True)).to_be_enabled()
                await search.fill('')
                await page.get_by_role('button', name=m['trash'], exact=True).click()
                await expect(row).to_have_count(1)
                await search.fill('specs')
                actions = await row_menu(page, row)
                await expect(actions.get_by_role('menuitem', name=m['purge'], exact=True)).to_be_enabled()
                await expect(actions.get_by_role('menuitem', name=f"{m['rename']} / {m['move']}", exact=True)).to_have_count(0)
                await actions.get_by_role('menuitem', name=m['restore'], exact=True).click()
                await page.get_by_role('dialog').get_by_role('button', name=m['restore'], exact=True).click()
                await search.fill('')
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
                await search.fill('specs')
                actions = await row_menu(page, page.locator('.documentItem'))
                await expect(actions.get_by_role('menuitem', name=m['trashAction'], exact=True)).to_be_disabled()
                assert not errors and not api.failures and not api.unexpected, (errors, api.failures, api.unexpected)
                await context.close()
        for language in ('ja', 'zh', 'en'):
            for theme in ('dark', 'light'):
                for width in (390, 1440):
                    await folder_selection(browser, url, output, language, theme, width)
        for language, theme, width in [('ja', 'dark', 1440), ('en', 'light', 390)]:
            await rename_close_focus(browser, url, output, language, theme, width)
        await browser.close()
    print('Document organization: 18 layout views, 6 action/read-only, 12 directory-selection and 2 rename-focus regressions passed')


async def rename_close_focus(browser, url: str, output: Path, language: str, theme: str, width: int) -> None:
    """条件 mount の初期 focus と、普通取消/拒否後取消から元行への復帰を確認する。"""
    api = OrganizationApi(url, language)
    context = await browser.new_context(viewport={'width': width, 'height': 900})
    await context.route('**/*', api.route)
    await context.add_init_script(f"if (window === window.top) localStorage.setItem('skillmind.theme', '{theme}')")
    page = await context.new_page()
    errors: list[str] = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        await page.goto(f'{url}#/documents?project={PROJECT}')
        await expect(page.locator('.documentItem')).to_have_count(1)
        labels = await messages(page, language)
        m = labels['fileManagement']
        await page.get_by_role('textbox', name=m['search'], exact=True).fill('specs')
        row = page.locator('.documentItem')
        trigger = row.locator('button[aria-haspopup="menu"]')
        dialog = page.get_by_role('dialog')
        for refuse in (False, True):
            actions = await row_menu(page, row)
            await actions.get_by_role('menuitem', name=f"{m['rename']} / {m['move']}", exact=True).click()
            await expect(dialog).to_be_visible()
            await expect(dialog).to_be_focused()
            await dialog.get_by_label(m['name'], exact=True).fill('unsaved-focus-draft.json')
            if refuse:
                original_trigger = await trigger.element_handle()
                api.refusal = 'document_conflict'
                await dialog.get_by_role('button', name=m['save'], exact=True).click()
                await expect(dialog.get_by_role('alert')).to_have_text(m['failure'])
                await expect(dialog.get_by_label(m['name'], exact=True)).to_have_value('unsaved-focus-draft.json')
                # 既に再描画された同 ID の入口への復帰を検査する。未取得の行へ遅延 focus は要求しない。
                await page.wait_for_function('original => { const current = document.getElementById(original.id); return current && current !== original }', arg=original_trigger)
                await expect(trigger).to_be_visible()
                await page.keyboard.press('Escape')
            else:
                await dialog.locator('form').get_by_role('button', name=m['cancel'], exact=True).click()
            await expect(dialog).to_have_count(0)
            await expect(trigger).to_be_focused()
            await expect(page.get_by_role('menu')).to_have_count(0)
            assert api.operations == (['MOVE'] if refuse else [])
            assert api.row['name'] != 'unsaved-focus-draft.json'
        assert not api.failures and not api.unexpected and not errors, (api.failures, api.unexpected, errors)
        await page.screenshot(path=str(output / f'rename-focus-{language}-{theme}-{width}.png'))
        print(f'PASS rename-focus-{language}-{theme}-{width}', flush=True)
    finally:
        await context.close()


async def check_focus(url: str, output: Path) -> None:
    """局所 focus 修正だけを PC/狭幅で再実行し、既存整理 suite を繰り返さない。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language, theme, width in [('ja', 'dark', 1440), ('en', 'light', 390)]:
                await rename_close_focus(browser, url, output, language, theme, width)
        finally:
            await browser.close()


async def folder_selection(browser, url: str, output: Path, language: str, theme: str, width: int) -> None:
    """検索中の子孫選択、半選択、別目录選択の保持と menu/preview の独立操作を検証する。"""
    api = DocumentsApi(url, language, 'success')
    third = '00000000-0000-4000-8000-000000000093'
    api.rows[PROJECT] = [
        {**document(PROJECT), 'folder': 'specs/nested'},
        document(PROJECT, SECOND),
        {**document(PROJECT, third), 'name': 'archive.zip', 'mime': 'application/zip', 'folder': 'elsewhere'},
    ]
    context = await browser.new_context(viewport={'width': width, 'height': 900})
    await context.route('**/*', api.route)
    await context.add_init_script(f"if (window === window.top) localStorage.setItem('skillmind.theme', '{theme}')")
    page = await context.new_page()
    errors: list[str] = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    name = f'directory-{language}-{theme}-{width}'
    try:
        await page.goto(f'{url}#/documents?project={PROJECT}')
        await expect(page.locator('.documentItem')).to_have_count(3)
        catalog = await messages(page, language)
        m, d = catalog['fileManagement'], catalog['documentsPanel']
        toolbar = page.locator('.documentSelectionToolbar')
        await expect(toolbar.get_by_role('button')).to_have_count(0)
        specs = page.get_by_role('checkbox', name=f"{m['selectFolder']}: specs", exact=True)
        summary = page.locator('.docFolder > summary').filter(has=page.locator('strong[title="specs"]'))
        folder = summary.locator('..')
        await expect(folder).to_have_attribute('open', '')
        menu = await row_menu(page, summary)
        await expect(folder).to_have_attribute('open', '')
        await menu.get_by_role('menuitem', name=d['uploadHere'], exact=True).click()
        await expect(page.get_by_role('combobox', name=d['targetFolder'], exact=True)).to_have_value('specs')
        await expect(folder).to_have_attribute('open', '')
        outside = page.locator('.documentItem').filter(has=page.get_by_role('link', name=f"{d['download']}: archive.zip", exact=True))
        await expect(outside.get_by_role('link')).to_have_attribute('download', 'archive.zip')
        await expect(outside.get_by_role('link')).to_have_attribute('href', f'{api.prefix}projects/{PROJECT}/documents/{third}/content')
        await outside.get_by_role('checkbox').check()
        await specs.check()
        await expect(folder).to_have_attribute('open', '')
        await expect(page.locator('.documentItem input:checked')).to_have_count(3)
        nested_summary = page.locator('.docFolder > summary').filter(has=page.locator('strong[title="specs/nested"]'))
        await nested_summary.locator('strong').click()
        first = page.locator('.documentItem').filter(has=page.get_by_role('button', name=f"{d['previewButton']}: overview.md", exact=True))
        await first.get_by_role('checkbox').uncheck()
        await expect(specs).to_have_js_property('indeterminate', True)
        await specs.check()
        await expect(page.locator('.documentItem input:checked')).to_have_count(3)
        await specs.uncheck()
        await expect(specs).to_have_js_property('indeterminate', False)
        await expect(page.locator('.documentItem input:checked')).to_have_count(1)
        await expect(outside.get_by_role('checkbox')).to_be_checked()
        await toolbar.get_by_role('button', name=d['clearSelection'], exact=True).click()
        await expect(toolbar.get_by_role('button')).to_have_count(0)
        # 検索で除外された同目录の文書を、目录選択で暗黙に追加しない。
        search = page.get_by_role('textbox', name=m['search'], exact=True)
        await search.fill('overview')
        await expect(page.locator('.documentItem')).to_have_count(1)
        await specs.check()
        await expect(page.locator('.documentItem input:checked')).to_have_count(1)
        await expect(specs).to_have_js_property('indeterminate', False)
        await expect(page.get_by_role('checkbox', name=d['selectAll'], exact=True)).to_be_checked()
        preview = page.get_by_role('button', name=f"{d['previewButton']}: overview.md", exact=True)
        await preview.click()
        await expect(page.frame_locator('iframe.previewFrame').get_by_role('heading', name='Fixture', exact=True)).to_be_visible()
        await page.keyboard.press('Escape')
        await expect(preview).to_be_focused()
        await expect(page.locator('.documentItem input:checked')).to_have_count(1)
        await search.fill('')
        await expect(page.locator('.documentItem input:checked')).to_have_count(0)
        await expect(toolbar.get_by_role('button')).to_have_count(0)
        assert not api.delete_calls and not api.unexpected and not api.failures and not errors, (
            api.delete_calls, api.unexpected, api.failures, errors,
        )
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.screenshot(path=str(output / f'{name}.png'), full_page=True)
        print(f'PASS {name}', flush=True)
    finally:
        await context.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:5173/skillmind/tests/browser/projects.html')
    parser.add_argument('--output', type=Path, default=Path('/tmp/skillmind-document-organization'))
    parser.add_argument('--focus-only', action='store_true', help='Run only rename-dialog cancellation/focus regressions')
    args = parser.parse_args()
    asyncio.run((check_focus if args.focus_only else check)(args.url, args.output))
