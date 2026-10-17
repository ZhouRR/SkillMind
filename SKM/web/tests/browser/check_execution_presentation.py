"""実行中・長文・空結果・版選択・表の省略を、実 App と全面 mock API で確認する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from account_fixture import PROJECT
from browser_helpers import messages
from check_projects import RUN, SECOND_VERSION, layout
from check_run_submission import task_catalog
from check_visual_style import VisualApi
from document_fixture import DOCUMENT, document, open_ancestor_folders
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
        self.event_count = 3
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
                    **({'finished_at': None} if self.live and 'finished_at' in self.body else {}),
                    'result': None if self.live else self.result})
                return
            if suffix == f'projects/{PROJECT}/runs/{RUN}/artifacts':
                await route.fulfill(json=[])
                return
            if suffix == f'runs/{RUN}/events':
                events = []
                for index in range(1, self.event_count + 1):
                    kind = ('RUN_SNAPSHOT', 'TOOL_REQUESTED', 'TOOL_COMPLETED')[(index - 1) % 3]
                    # 終態 snapshot は replay の末尾へ置き、途中で購読を閉じて監査を欠落させない。
                    if self.event_count > 3:
                        kind = 'RUN_SNAPSHOT' if index == self.event_count else 'TOOL_REQUESTED' if index % 2 else 'TOOL_COMPLETED'
                    event = {'run_id': RUN, 'run_attempt_id': None, 'agent_session_id': None,
                             'sequence': index, 'event_type': kind, 'occurred_at': self.body['created_at'],
                             'payload': {'status': self.run()['status'], 'row_version': 4}, 'trace_id': None}
                    name = kind.lower().replace('_', '.')
                    if self.event_count > 3 and index == self.event_count:
                        # 進行中の表示へ完成済み Result を混ぜず、実際の partial text を再現する。
                        text_kind = 'TEXT_DELTA' if self.live else 'TEXT_COMPLETED'
                        text = {**event, 'event_type': text_kind, 'payload': {'text':
                            'Synthetic review is in progress. Checking the next section.' if self.live else json.dumps(self.result['data'])}}
                        events.append(f'event: {text_kind.lower().replace("_", ".")}\ndata: {json.dumps(text)}\n\n')
                        event['sequence'] = index + 1
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


async def check_details(url: str, output: Path) -> None:
    """長いレビュー結果・多数の証拠/監査を使い、詳細三タブと実行中の階層を検証する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ('zh', 'ja', 'en'):
                for theme in ('dark', 'light'):
                    api = PresentationApi(url, language)
                    api.event_count = 120
                    api.body['task_title'] = 'テスト仕様書レビュー · サンプル'
                    finished = datetime.now(timezone.utc)
                    api.body.update(started_at=(finished - timedelta(minutes=6)).isoformat(), finished_at=finished.isoformat())
                    api.result.update(summary='サンプル仕様書の確認を終えました。確認事項が二件あります。生成した資料と根拠を確認し、修正後に次の工程へ進んでください。実プロジェクトの判定ではありません。', needs_review=True)
                    api.result['data'].update(summary=api.result['summary'], deliverables=[
                        {'key': 'spec', 'kind': 'report', 'title': '確認済み仕様書 · サンプル', 'content': '# サンプル仕様書\n\n確認対象・手順・期待値を整理した合成資料です。'},
                        {'key': 'report', 'kind': 'report', 'title': 'レビュー報告 · サンプル', 'content': '# サンプル報告\n\n未確認事項を含む表示検証用の資料です。'}], findings=[
                        {'key': 'first', 'title': '期待値の記載を確認してください', 'severity': 'HIGH', 'detail': 'この確認事項は画面検証用の合成データです。'},
                        {'key': 'second', 'title': '例外時の手順を確認してください', 'severity': 'MEDIUM', 'detail': 'この確認事項は画面検証用の合成データです。'}])
                    base = api.body['evidence'][0]
                    api.body['evidence'] = [{**base, 'evidence_ref': f'ev_sample_{index}', 'source_locator': {'name': f'Synthetic evidence {index}'}, 'source_uri': f'document://synthetic/{index}'} for index in range(72)]
                    context = await browser.new_context(viewport={'width': 1440, 'height': 900}, reduced_motion='reduce')
                    await context.route('**/*', api.route)
                    page = await context.new_page()
                    errors: list[str] = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    try:
                        await page.goto(f'{url}#/history?project={PROJECT}&run={RUN}')
                        labels = await messages(page, language)
                        await page.locator('.themeToggle').get_by_role('button', name=labels['theme'][theme], exact=True).click()
                        for width, height in ((1440, 900), (1366, 768), (1920, 1080), (390, 844)):
                            name = f'{language}-{theme}-{width}'
                            await page.set_viewport_size({'width': width, 'height': height})
                            await expect(page.locator('.executionHeader h1')).to_have_text(api.body['task_title'])
                            await expect(page.locator('.executionHeader .statusBadge')).to_be_visible()
                            await expect(page.locator('.executionMetadata .runDuration')).to_be_visible()
                            for tab in ('tabResult', 'tabConversation', 'tabEvents'):
                                await page.get_by_role('tab', name=labels['workspace'][tab], exact=False).click()
                                await layout(page)
                                # タブ下線と本文 card は別の線として読める間隔を保つ。
                                assert await page.locator('.observationPanel').evaluate('''el => {
                                  const tabs=el.querySelector(':scope > .tabBar').getBoundingClientRect();
                                  const body=el.querySelector(':scope > .tabPanel').getBoundingClientRect();
                                  return body.top - tabs.bottom >= 15.5;
                                }''')
                                if tab == 'tabResult':
                                    await expect(page.locator('.reportSummary')).to_be_visible()
                                    await expect(page.locator('.resultActions > .runDuration')).to_be_hidden()
                                    await expect(page.locator('.outcomeCard').first).to_be_visible()
                                    if width >= 1366:
                                        assert await page.locator('.outcomeCard').first.evaluate('el => el.getBoundingClientRect().top < innerHeight - 80')
                                elif tab == 'tabEvents':
                                    await expect(page.locator('[role="tabpanel"]:visible .timeline > li')).to_have_count(121)
                                elif tab == 'tabConversation':
                                    await expect(page.locator('.messageAgent')).to_be_visible()
                                await page.screenshot(path=str(output / f'{tab}-{name}.png'))
                            print(f'PASS execution-detail-{name}', flush=True)
                        api.live = True
                        await page.set_viewport_size({'width': 1440, 'height': 900})
                        await page.reload()
                        await expect(page.locator('.runLiveProgress')).not_to_have_attribute('open', '')
                        await expect(page.locator('.runLiveLatest')).to_be_visible()
                        await page.locator('.runLiveProgress > summary').click()
                        await expect(page.locator('.runLiveProgress .timeline > li')).to_have_count(3)
                        await page.locator('.runLiveProgress > summary').click()
                        await layout(page)
                        await page.screenshot(path=str(output / f'running-{language}-{theme}.png'))
                        assert not api.mutations() and not api.failures and not api.unexpected and not errors, (api.unexpected, api.failures, errors)
                    finally:
                        await context.close()
        finally:
            await browser.close()


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
    parser.add_argument('--details-only', action='store_true')
    args = parser.parse_args()
    address = urlsplit(args.url)
    if address.scheme != 'http' or address.hostname not in ('127.0.0.1', 'localhost') or not address.path.endswith('/tests/browser/projects.html') or address.query or address.fragment:
        parser.error('Only the owned loopback projects.html mock harness is allowed')
    asyncio.run(check_details(args.url, args.output) if args.details_only else check(args.url, args.output))
