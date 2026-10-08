"""文書と履歴の削除確認だけを全面 mock し、長名・狭幅・取消を検証する。実削除は禁止する。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from check_document_organization import OrganizationApi
from check_history_actions import HistoryApi
from check_projects import PROJECT, RUN, messages
from check_workspace_reports import CONTRACTS
from playwright.async_api import Page, Route, async_playwright, expect


class ConfirmationDocumentsApi(OrganizationApi):
    """同名の異なる原文書を返し、確認後の write があれば fixture 自体を失敗させる。"""

    async def respond(self, route: Route) -> None:
        request = route.request
        address = urlsplit(request.url)
        suffix = address.path.removeprefix(self.prefix)
        if suffix == f'projects/{PROJECT}/document-operations' or (
            suffix.startswith(f'projects/{PROJECT}/documents') and request.method != 'GET'
        ):
            raise AssertionError('This layout fixture never authorizes a document mutation')
        if suffix == f'projects/{PROJECT}/documents' and request.method == 'GET':
            self.reads += 1
            wanted = parse_qs(address.query).get('trashed') == ['true']
            rows = [{**self.row, 'document_id': f'00000000-0000-4000-8000-{index + 200:012d}',
                     'folder': f'specs/nested/{index}', 'name': 'unbroken-file-name-' * 14 + '.md'}
                    for index in range(12)]
            await route.fulfill(json={'documents': rows if wanted == self.trashed else []})
            return
        await super().respond(route)


class ConfirmationHistoryApi(HistoryApi):
    """長い原タスク名と参照保護された成果を返す。write は認めない。"""

    def preview(self) -> dict:
        outputs = [{'document_id': f'00000000-0000-4000-8000-{index + 400:012d}',
                    'folder': f'reports/{index}', 'name': 'unbroken-output-name-' * 14 + '.md',
                    'protected': index == 0} for index in range(12)]
        return {'run_id': RUN, 'deleted': self.deleted, 'cleanup_pending': 0,
                'output_count': 12, 'protected_output_count': 1, 'outputs': outputs}

    async def respond(self, route: Route) -> None:
        request = route.request
        address = urlsplit(request.url)
        suffix = address.path.removeprefix(self.prefix)
        base = f'projects/{PROJECT}/runs'
        if suffix.startswith(base) and request.method != 'GET':
            raise AssertionError('This layout fixture never authorizes a run mutation')
        if suffix == base and request.method == 'GET':
            query = parse_qs(address.query)
            template = json.loads((CONTRACTS / 'examples/run-history.v1.json').read_text())
            row = {**template['items'][0], 'run_id': RUN, 'project_id': PROJECT,
                   'task_title': 'Long original task / 原任务 / 元タスク ' * 12, 'status': 'SUCCEEDED'}
            visible = (query.get('trashed') == ['true']) == self.deleted
            await route.fulfill(json={**template, 'items': [row] if visible else [],
                                     'offset': 0, 'limit': int(query['limit'][0]), 'has_more': False})
            return
        await super().respond(route)


async def layout(page: Page, count: int, targets: str) -> None:
    """実寸法で水平 overflow、本文 clipping、操作不能を検知する。短い縦画面は scroll を許す。"""
    dialog = page.get_by_role('dialog')
    await expect(dialog).to_be_visible()
    await expect(dialog.locator(targets)).to_have_count(count)
    bounds = await dialog.bounding_box()
    viewport = page.viewport_size
    assert bounds and viewport and bounds['width'] <= 460.5, bounds
    assert bounds['x'] >= 0 and bounds['y'] >= 0, bounds
    assert bounds['x'] + bounds['width'] <= viewport['width'] + 1, bounds
    assert bounds['y'] + bounds['height'] <= viewport['height'] + 1, bounds
    assert await dialog.evaluate('el => el.scrollWidth <= el.clientWidth + 1')
    alignment = await dialog.evaluate('''el => {
      const header = getComputedStyle(el.querySelector('.modalHeader'));
      const body = getComputedStyle(el.querySelector('.modalBody'));
      return [header.paddingLeft, body.paddingLeft, body.paddingRight, body.scrollbarGutter];
    }''')
    assert alignment[0] == alignment[1] == alignment[2] and alignment[3] == 'auto', alignment
    for button in await dialog.locator('.confirmActions button').all():
        await button.scroll_into_view_if_needed()
        assert await button.evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        box = await button.bounding_box()
        assert box and box['y'] >= bounds['y'] and box['y'] + box['height'] <= bounds['y'] + bounds['height'] + 1, box
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')


async def check(url: str, output: Path) -> None:
    """三語・双テーマ・PC/狭幅/低い画面で確認し、取消と再表示だけで終える。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ('zh', 'ja', 'en'):
                for theme in ('light', 'dark'):
                    for width, height in ((1440, 900), (1366, 768), (390, 844), (390, 360)):
                        for kind in ('documents', 'history'):
                            api = ConfirmationDocumentsApi(url, language) if kind == 'documents' else ConfirmationHistoryApi(url, language)
                            context = await browser.new_context(viewport={'width': width, 'height': height})
                            await context.route('**/*', api.route)
                            await context.add_init_script(f"if (window === window.top) localStorage.setItem('skillmind.theme', '{theme}')")
                            page = await context.new_page()
                            errors: list[str] = []
                            page.on('pageerror', lambda error, captured=errors: captured.append(str(error)))
                            try:
                                await page.goto(f'{url}#/{kind}?project={PROJECT}')
                                labels = (await messages(page, language))['fileManagement']
                                for action in ('trashAction', 'purge'):
                                    if action == 'purge':
                                        if kind == 'documents':
                                            api.trashed = True
                                        else:
                                            api.deleted = True
                                        await page.get_by_role('button', name=labels['trash'], exact=True).click()
                                    if kind == 'documents':
                                        toolbar = page.locator('.documentSelectionToolbar')
                                        await expect(page.locator('.documentItem')).to_have_count(12)
                                        await toolbar.get_by_role('checkbox').check()
                                        trigger = toolbar.get_by_role('button', name=labels[action], exact=True)
                                        await trigger.click()
                                    else:
                                        trigger = page.locator('.historyItem button[aria-haspopup="menu"]')
                                        await trigger.click()
                                        await page.get_by_role('menuitem', name=labels[action], exact=True).click()
                                        await expect(page.locator('.runDeletionOutputs')).to_be_visible()
                                        await page.locator('.runDeletionOutputs summary').click()
                                    selector = '.confirmTargetList li' if kind == 'documents' else '.runDeletionOutputs li'
                                    await layout(page, 12, selector)
                                    dialog = page.get_by_role('dialog')
                                    if action == 'purge':
                                        consequence = labels['purgeDocuments'] if kind == 'documents' else labels['purgeConfirm']
                                        await expect(dialog.locator('.confirmMessage')).to_have_text(consequence)
                                    name = f'{kind}-{action}-{language}-{theme}-{width}x{height}'
                                    await page.screenshot(path=str(output / f'{name}.png'))
                                    await dialog.get_by_role('button', name=labels['cancel'], exact=True).click()
                                    await expect(page.get_by_role('dialog')).to_have_count(0)
                                    await expect(trigger).to_be_focused()
                                    if kind == 'documents':
                                        assert not api.operations
                                    else:
                                        assert not api.actions
                                    print(f'PASS {name}', flush=True)
                                assert not errors and not api.failures and not api.unexpected, (errors, api.failures, api.unexpected)
                            finally:
                                await context.close()
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    target = urlsplit(args.url)
    if target.scheme != 'http' or target.hostname not in {'localhost', '127.0.0.1'} or not target.path.endswith('/tests/browser/projects.html'):
        parser.error('Only the loopback App mock harness is allowed')
    asyncio.run(check(args.url, args.output))
