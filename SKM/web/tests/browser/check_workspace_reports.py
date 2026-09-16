"""Workspace の作業一覧・公開レポートと履歴詳細の分離を実 App で確認する。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from check_artifacts import ARTIFACT, HEADERS, ArtifactApi, metadata
from check_projects import PROJECT, RUN, TASK, SECOND_VERSION, layout, messages
from check_run_submission import task_catalog
from check_result_references import CONTRACTS
from playwright.async_api import Route, async_playwright, expect

SECOND_TASK = '00000000-0000-4000-8000-000000000032'

REPORT = '# Desktop login report\n\nTested: 20\n\nPassed: 17\n\nFailed: 3\n'


class WorkspaceApi(ArtifactApi):
    """状態 filter と公開された Markdown/HTML の原 byte だけを返す。"""

    def __init__(self, url: str, language: str) -> None:
        """実行済み一件を用意し、全 query の filter/offset を記録する。"""
        super().__init__(url, language, 'contract')
        self.queries: list[dict] = []
        self.with_tasks = True
        self.with_module = True
        self.failed = False
        self.empty = False
        self.wrong_scope = False
        self.preview_requests = 0
        self.report = REPORT
        self.report_path = 'output/login-report.md'
        self.body['result']['summary'] = 'Desktop login: 17 passed, 3 failed'
        self.body['result']['data']['summary'] = self.body['result']['summary']
        self.body['result']['data']['deliverables'] = [
            {'key': 'report', 'kind': 'report', 'title': 'Rendered result', 'content': REPORT},
            {'key': 'data', 'kind': 'structured_data', 'title': 'Structured evidence',
             'content': '{"passed":17,"failed":3}'},
        ]

    async def respond(self, route: Route) -> None:
        """索引・本文の hash と、server 絞り込み済みの一覧を返す。"""
        address = urlsplit(route.request.url)
        suffix = address.path.removeprefix(self.prefix)
        if route.request.method == 'GET' and suffix == f'projects/{PROJECT}/tasks':
            catalog = task_catalog()
            first = catalog['tasks'][0]
            first['readiness']['requirements'] = []
            catalog['tasks'].append({**deepcopy(first), 'task_id': SECOND_TASK,
                                    'task_key': 'resume', 'title': 'Resume review'})
            catalog['tasks'].append({**deepcopy(first), 'task_id': '00000000-0000-4000-8000-000000000033',
                                    'skill_version_id': SECOND_VERSION, 'title': 'Other module task'})
            await route.fulfill(json=catalog)
            return
        if route.request.method == 'GET' and suffix == f'projects/{PROJECT}/runs':
            query = parse_qs(address.query)
            self.queries.append(query)
            template = json.loads((CONTRACTS / 'examples/run-history.v1.json').read_text())
            row = deepcopy(template['items'][0])
            requested = query.get('status', ['SUCCEEDED'])
            row.update(run_id=RUN, project_id=PROJECT, task_id=SECOND_TASK if self.wrong_scope else TASK, status='FAILED' if self.failed else requested[0],
                       result_summary=None if self.failed else self.body['result']['summary'])
            items = [] if self.empty or query.get('task_id') == [SECOND_TASK] else [row]
            await route.fulfill(json={**template, 'items': items, 'limit': int(query['limit'][0]),
                'offset': int(query.get('offset', ['0'])[0]), 'has_more': query.get('offset', ['0'])[0] == '0'})
            return
        if self.failed and suffix == f'projects/{PROJECT}/runs/{RUN}/detail':
            await route.fulfill(json={**self.body, 'status': 'FAILED', 'result': None})
            return
        if route.request.method == 'GET' and suffix == f'projects/{PROJECT}/runs/{RUN}/artifacts':
            item = metadata()
            item.update(path=self.report_path, mime_type='text/plain', size_bytes=len(self.report.encode()),
                        checksum='sha256:' + hashlib.sha256(self.report.encode()).hexdigest())
            await route.fulfill(json=[item])
            return
        if route.request.method == 'GET' and suffix == f'projects/{PROJECT}/runs/{RUN}/artifacts/{ARTIFACT}/content':
            self.preview_requests += 1
            await route.fulfill(headers=HEADERS, body=self.report)
            return
        await super().respond(route)


async def check(url: str, output: Path) -> None:
    """PC 三語で作業・詳細 route、page 2 と静的レポートを操作する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ('ja', 'zh', 'en'):
                api = WorkspaceApi(url, language)
                context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
                await context.route('**/*', api.route)
                page = await context.new_page()
                errors: list[str] = []
                page.on('pageerror', lambda error, captured=errors: captured.append(str(error)))
                try:
                    await page.goto(f'{url}#/workspace?project={PROJECT}&module=00000000-0000-4000-8000-000000000501')
                    labels = await messages(page, language)
                    work = labels['workspace']
                    await expect(page.locator('.workspaceQueue .pendingItem')).to_have_count(1)
                    await expect(page.get_by_role('tab', name=work['tabEvents'], exact=True)).to_have_count(0)
                    await page.get_by_role('tab', name=work['queue']['reports'], exact=True).click()
                    await expect(page.locator('.workspaceReports .deliverableContent')).to_have_count(2)
                    await expect(page.locator('.workspaceReports iframe.outcomeReportFrame')).to_have_count(0)
                    await expect(page.locator('.workspaceQueue .pendingItem')).to_have_count(0)
                    await expect(page.locator('.workspaceQueue .pagination')).to_have_count(0)
                    assert api.queries[-1]['status'] == ['SUCCEEDED', 'FAILED', 'CANCELLED']
                    assert api.queries[-1]['task_id'] == [TASK]
                    assert api.queries[-1]['limit'] == ['1']
                    chooser = page.locator('.reportToolbar select')
                    await expect(chooser.locator('option')).to_have_count(2)
                    await chooser.select_option(SECOND_TASK)
                    await expect(page.get_by_text(work['latestReportEmpty'], exact=True)).to_be_visible()
                    await expect(page.locator('iframe.outcomeReportFrame')).to_have_count(0)
                    await chooser.select_option(TASK)
                    await expect(page.locator('.resultSummary .readingMarkdown')).to_have_text(api.body['result']['summary'])
                    await expect(page.get_by_role('tab', name=work['tabConversation'], exact=True)).to_have_count(0)
                    await expect(page.get_by_role('tab', name=work['tabEvents'], exact=True)).to_have_count(0)
                    await expect(page.locator('.runFacts')).to_have_count(0)
                    await page.locator('.deliverableContent > summary').first.click()
                    await expect(page.locator('.deliverableContent .readingMarkdown').get_by_role('heading', name='Desktop login report')).to_be_visible()
                    report = page.frame_locator('iframe.outcomeReportFrame')
                    structured = page.locator('.outcomeCard details').filter(has_text='"passed":17')
                    await expect(structured).to_have_count(1)
                    assert not await structured.evaluate('(element) => element.open')
                    await expect(page.get_by_text(labels['runResult']['toolCalls'], exact=True)).to_have_count(0)
                    preview = page.locator('.runArtifacts').get_by_role('button', name=labels['runResult']['artifacts']['preview'], exact=True)
                    await preview.click()
                    frame = page.frame_locator('iframe.runReportPreview')
                    await expect(page.locator('.artifactMarkdownPreview').get_by_role('heading', name='Desktop login report')).to_be_visible()
                    await expect(page.locator('.artifactMarkdownPreview')).to_contain_text('Passed: 17')
                    await expect(page.locator('iframe.runReportPreview')).to_have_count(0)
                    await page.screenshot(path=str(output / f'report-{language}.png'))
                    await page.get_by_role('dialog').get_by_role('button', name=labels['elements']['close'], exact=True).click()
                    html = ('<!doctype html><html lang="en"><head><style>'
                        'body{margin:24px;background:#faf8f3;color:#183330}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:20px}'
                        '</style></head><body><h1>HTML login report</h1><div class="metrics"><p>Passed: 17</p><p>Failed: 3</p></div>'
                        '<svg viewBox="0 0 100 20"><rect width="85" height="20" fill="#287466"/></svg>'
                        '<script>parent.document.body.dataset.injected="yes";fetch("https://forbidden.invalid")</script>'
                        '<img src="https://forbidden.invalid/x"><a href="https://forbidden.invalid">Evidence</a></body></html>')
                    api.body['result']['data']['deliverables'][0]['content'] = html
                    await page.locator('.reportToolbar button').click()
                    await expect(report.get_by_role('heading', name='HTML login report')).to_be_visible()
                    assert await report.locator('.metrics').evaluate('(e) => getComputedStyle(e).display') == 'grid'
                    await expect(report.locator('svg rect')).to_have_count(1)
                    await expect(report.locator('script, [src], [href]')).to_have_count(0)
                    assert await page.locator('body').get_attribute('data-injected') is None
                    for theme in ('dark', 'light'):
                        await page.locator('.themeToggle').get_by_role('button', name=labels['theme'][theme], exact=True).click()
                        await layout(page)
                        await expect(report.get_by_role('heading', name='HTML login report')).to_be_visible()
                    for width, height in ((1366, 768), (1920, 1080), (390, 844)):
                        await page.set_viewport_size({'width': width, 'height': height})
                        await layout(page)
                    await page.set_viewport_size({'width': 1440, 'height': 1000})
                    await page.screenshot(path=str(output / f'inline-html-{language}.png'))
                    api.failed = True
                    await page.locator('.reportToolbar button').click()
                    await expect(page.locator('iframe.outcomeReportFrame')).to_have_count(0)
                    await expect(page.locator('.reportRunMeta .statusBadge')).to_be_visible()
                    assert api.queries[-1]['task_id'] == [TASK]
                    api.failed = False
                    await page.locator('.reportToolbar button').click()
                    await expect(report.get_by_role('heading', name='HTML login report')).to_be_visible()
                    # 他 Task の誤応答はレポートとして受け入れず、旧結果も最新扱いしない。
                    api.wrong_scope = True
                    await page.locator('.reportToolbar button').click()
                    await expect(page.locator('.workspaceReports [role="alert"]')).to_be_visible()
                    await expect(page.locator('iframe.outcomeReportFrame')).to_have_count(0)
                    api.wrong_scope = False
                    await page.locator('.reportToolbar button').click()
                    await expect(report.get_by_role('heading', name='HTML login report')).to_be_visible()
                    await page.get_by_role('link', name=work['executionDetail'], exact=True).click()
                    assert '#/history?' in page.url and f'run={RUN}' in page.url
                    await expect(page.get_by_role('tab', name=work['tabConversation'], exact=True)).to_be_visible()
                    await expect(page.get_by_role('tab', name=work['tabEvents'], exact=False)).to_be_visible()
                    await page.reload()
                    await expect(page.locator('.runFacts')).to_be_visible()
                    await page.get_by_role('link', name=labels['routes']['history']['label'], exact=True).last.click()
                    await expect(page.locator('.historyItem')).to_have_count(1)
                    await page.locator('.historyItem > button').click()
                    assert '#/history?' in page.url and f'run={RUN}' in page.url
                    await expect(page.locator('.runFacts')).to_be_visible()
                    await layout(page)
                    await page.screenshot(path=str(output / f'history-detail-{language}.png'))
                    # HTML は inline CSS/SVG を保ち、script や外部アクセスを実行しない。
                    api.report_path = 'output/login-report.html'
                    api.report = '<html><body><h1>HTML login report</h1><script>fetch("https://forbidden.invalid")</script><img src="https://forbidden.invalid/x"><p>Passed: 17</p></body></html>'
                    await page.goto(f'{url}#/workspace?project={PROJECT}&run={RUN}')
                    # 別 fixture の本文へ変えたので索引も再取得する。同一 hash の頁移動だけでは更新されない。
                    await page.reload()
                    await preview.click()
                    await expect(frame.get_by_role('heading', name='HTML login report')).to_be_visible()
                    assert not errors and not api.failures and not api.unexpected, (errors, api.failures, api.unexpected)
                    print(f'PASS workspace/report/history {language}', flush=True)
                except Exception:
                    print({'errors': errors, 'failures': api.failures, 'unexpected': api.unexpected,
                           'body': (await page.locator('body').inner_text())[-1800:]}, flush=True)
                    raise
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
    if (target.scheme != 'http' or target.hostname not in {'127.0.0.1', 'localhost'}
            or not target.path.endswith('/tests/browser/projects.html')):
        parser.error('Only loopback projects.html is allowed')
    asyncio.run(check(args.url, args.output))
