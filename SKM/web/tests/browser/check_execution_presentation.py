"""実行中・長文・空結果・版選択・表の省略を、実 App と全面 mock API で確認する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from check_document_management import DOCUMENT, document, open_ancestor_folders
from check_projects import PROJECT, RUN, SECOND_VERSION, layout, messages
from check_run_submission import task_catalog
from check_visual_style import VisualApi
from playwright.async_api import Route, async_playwright, expect

MESSAGE = 'Synthetic UI review message. ' * 14 + '\nSecond line remains visible.'
TABLE = ('# Synthetic document\n\n| Excel row | A | B | C |\n|---|---|---|---|\n'
         '| 1 | | | |\n| 3 | | **Title** | |\n| 18 | Company | | |\n\n'
         'Warning: static style facts remain available.')


class PresentationApi(VisualApi):
    """合成 Run と同一 Skill の二版だけを持ち、外部送信と業務 mutation を許可しない。"""

    def __init__(self, url: str, language: str) -> None:
        """外部資源を含まない原 request と表示用記録を固定する。"""
        self.live = False
        super().__init__(url, language)
        self.with_module = True
        self.body.update(input={'message': MESSAGE}, interactions=[], document_snapshots=[],
                         change_proposals=[], approvals=[], effect_executions=[])
        result = self.body['result']
        result.update(artifact_refs=[], summary='Synthetic workflow accepted; delivery is unconfirmed.')
        result['data'].update(summary=result['summary'], deliverables=[], artifact_refs=[], effects=[], change_proposal_refs=[],
                              findings=[{'key': 'response', 'title': 'Synthetic response accepted',
                                         'detail': 'HTTP acceptance does not establish message delivery.',
                                         'evidence_refs': []}])
        self.result = deepcopy(result)
        base = self.body['evidence'][0]
        self.body['evidence'] = [
            {**base, 'evidence_ref': f'ev_presentation_{phase}', 'evidence_type': 'resource',
             'source_uri': f'http-resource://synthetic/operation/{phase}',
             'source_locator': {'phase': phase}, 'excerpt': None}
            for phase in ('request', 'response')
        ]

    def run(self) -> dict:
        """一覧・詳細・SSE の状態を同じ合成 snapshot に揃える。"""
        return {**super().run(), 'project_id': PROJECT,
                'status': 'RUNNING' if self.live else 'SUCCEEDED'}

    async def respond(self, route: Route) -> None:
        """既知の読取だけを上書きし、未知の通信は既存拒否 handler へ渡す。"""
        address = urlsplit(route.request.url)
        suffix = address.path.removeprefix(self.prefix)
        if f'{address.scheme}://{address.netloc}' == self.origin and route.request.method == 'GET':
            if suffix == f'projects/{PROJECT}/tasks':
                task = task_catalog()['tasks'][0]
                task.update(readiness=None, input_schema={'type': 'object', 'properties': {
                    'subject': {'type': 'string', 'maxLength': 100}, 'message': {'type': 'string'}}})
                await route.fulfill(json={'tasks': [task, {**task, 'skill_version_id': SECOND_VERSION,
                    'task_id': '00000000-0000-4000-8000-000000000032', 'version': '0.9.0'}]})
                return
            if suffix == f'projects/{PROJECT}/runs/{RUN}/detail':
                await route.fulfill(json={**self.body, 'status': self.run()['status'],
                    'result': None if self.live else self.result})
                return
            if suffix == f'projects/{PROJECT}/runs/{RUN}/artifacts':
                await route.fulfill(json=[])
                return
            if suffix == f'runs/{RUN}/events':
                events = []
                for index, kind in enumerate(('RUN_SNAPSHOT', 'TOOL_REQUESTED', 'TOOL_COMPLETED'), 1):
                    event = {'run_id': RUN, 'run_attempt_id': None, 'agent_session_id': None,
                             'sequence': index, 'event_type': kind, 'occurred_at': self.body['created_at'],
                             'payload': {'status': self.run()['status'], 'row_version': 4}, 'trace_id': None}
                    name = ('run.snapshot', 'tool.requested', 'tool.completed')[index - 1]
                    events.append(f'event: {name}\ndata: {json.dumps(event)}\n\n')
                await route.fulfill(content_type='text/event-stream', body=''.join(events))
                return
            if suffix == f'projects/{PROJECT}/documents':
                await route.fulfill(json={'documents': [{**document(PROJECT), 'name': 'preview.md',
                    'mime': 'text/markdown', 'size': len(TABLE.encode())}]})
                return
            if suffix == f'projects/{PROJECT}/document-folders':
                await route.fulfill(json={'folders': [document(PROJECT)['folder']]})
                return
            if suffix == f'projects/{PROJECT}/documents/{DOCUMENT}/content':
                await route.fulfill(content_type='text/markdown', body=TABLE)
                return
        await super().respond(route)


async def check(url: str, output: Path) -> None:
    """三語・双テーマ・PC/狭幅で実 control と省略の復元を検証する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ('zh', 'ja', 'en'):
                for theme in ('dark', 'light'):
                    for width in (1440, 390):
                        api = PresentationApi(url, language)
                        context = await browser.new_context(viewport={'width': 1440, 'height': 900}, reduced_motion='reduce')
                        await context.route('**/*', api.route)
                        page = await context.new_page()
                        errors: list[str] = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        name = f'{language}-{theme}-{width}'
                        try:
                            await page.goto(f'{url}#/workspace?project={PROJECT}')
                            labels = await messages(page, language)
                            await page.locator('.themeToggle').get_by_role('button', name=labels['theme'][theme], exact=True).click()
                            await page.set_viewport_size({'width': width, 'height': 900})
                            await page.locator('.runLauncher > button').click()
                            field = page.locator('.schemaTextInput').first
                            await field.fill(MESSAGE)
                            await expect(field).to_have_value(MESSAGE)
                            assert await field.evaluate('el => el.scrollHeight <= el.clientHeight + 1')
                            await layout(page)
                            await page.screenshot(path=str(output / f'launch-{name}.png'))
                            await page.keyboard.press('Escape')
                            api.live = True
                            await page.goto(f'{url}#/workspace?project={PROJECT}&run={RUN}')
                            await expect(page.locator('.runLiveProgress')).to_be_visible()
                            await expect(page.locator('.conversation .promptInputs dd')).to_have_text(MESSAGE)
                            await expect(page.locator('.resultView > .emptyState')).to_have_count(0)
                            await expect(page.locator('.runArtifacts')).to_have_count(0)
                            await layout(page)
                            await page.screenshot(path=str(output / f'running-{name}.png'))
                            api.live = False
                            await page.goto(f'{url}#/history?project={PROJECT}&run={RUN}')
                            await page.reload()
                            await page.get_by_role('tab', name=labels['workspace']['tabResult'], exact=True).click()
                            await expect(page.locator('.runArtifactsEmpty')).to_be_visible()
                            await expect(page.locator('.outcomeEmpty').first).to_be_visible()
                            await page.get_by_role('button', name=labels['runResult']['evidenceTitle'], exact=False).click()
                            drawer = page.get_by_role('dialog', name=labels['runResult']['evidenceTitle'], exact=True)
                            for role in ('request', 'response'):
                                await expect(drawer.get_by_text(labels['runResult']['evidenceRoles'][role], exact=True)).to_be_visible()
                            await layout(page)
                            await page.screenshot(path=str(output / f'evidence-{name}.png'))
                            await page.keyboard.press('Escape')
                            await page.goto(f'{url}#/projects?project={PROJECT}')
                            await page.get_by_role('tab', name=labels['projects']['tabModules'], exact=True).click()
                            await expect(page.locator('.moduleList')).to_be_visible()
                            await expect(page.locator('.moduleForm')).to_be_hidden()
                            await page.locator('.modulesPanel > .panelHeader button').click()
                            await expect(page.locator('.moduleSkillGroup')).to_have_count(1)
                            await expect(page.locator('.moduleSkillGroup details')).not_to_have_attribute('open', '')
                            await page.locator('.moduleSkillGroup summary').click()
                            await expect(page.locator('.moduleSkillOption')).to_have_count(2)
                            await layout(page)
                            await page.screenshot(path=str(output / f'module-drawer-{name}.png'))
                            await page.keyboard.press('Escape')
                            await page.goto(f'{url}#/documents?project={PROJECT}')
                            await expect(page.locator('.documentItem')).to_have_count(1)
                            await open_ancestor_folders(page.locator('.documentItem').first)
                            await page.locator('.documentName').click()
                            frame = page.frame_locator('iframe.previewFrame')
                            await expect(frame.locator('tbody tr')).to_have_count(2)
                            await expect(frame.locator('thead th')).to_have_count(3)
                            await expect(frame.get_by_text('Warning: static style facts remain available.')).to_be_visible()
                            await page.get_by_role('button', name=labels['documentsPanel']['restoreTableSpacing'], exact=True).click()
                            await expect(frame.locator('tbody tr')).to_have_count(3)
                            await expect(frame.locator('thead th')).to_have_count(4)
                            await page.get_by_role('button', name=labels['documentsPanel']['compactTables'], exact=True).click()
                            await layout(page)
                            await page.screenshot(path=str(output / f'document-{name}.png'))
                            assert not api.mutations() and not api.failures and not api.unexpected and not errors, (api.unexpected, api.failures, errors)
                            print(f'PASS execution-presentation-{name}', flush=True)
                        finally:
                            await context.close()
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    address = urlsplit(args.url)
    if address.scheme != 'http' or address.hostname not in ('127.0.0.1', 'localhost') or not address.path.endswith('/tests/browser/projects.html') or address.query or address.fragment:
        parser.error('Only the owned loopback projects.html mock harness is allowed')
    asyncio.run(check(args.url, args.output))
