"""実 App の Workspace 起動から進行中・詳細遷移までを全面 mock で描画検証する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from check_accounts import CSRF
from check_event_stream_batching import FAKE_SOURCE
from check_execution_presentation import PresentationApi
from check_projects import PROJECT, layout, messages
from playwright.async_api import Page, Route, async_playwright, expect

PROMPT = '表示確認用の合成仕様書について、手順と期待値を確認してください。'
PARTIAL = '合成仕様書の確認を進めています。\n手順と期待値を照合しています。'


class ExecutionApi(PresentationApi):
    """起動 POST だけをローカルで受理し、状態遷移と結果を独立に制御する。"""

    def __init__(self, url: str, language: str) -> None:
        """実入力を含まない待機 Run を用意し、起動数と状態を case ごとに分離する。"""
        super().__init__(url, language)
        self.live = True
        self.status = 'QUEUED'
        self.version = 1
        self.submissions: list[dict] = []
        self.body.update(task_title='仕様書レビュー · サンプル', created_at=datetime.now(timezone.utc).isoformat(),
                         row_version=1, started_at=None, finished_at=None, input={'message': PROMPT},
                         selected_sources={}, evidence=[], tool_calls=[], attempts=[], sessions=[])

    def run(self) -> dict:
        """親 fixture の初期化中にも使える、現在の正本 snapshot を返す。"""
        return {**super().run(), 'status': getattr(self, 'status', 'QUEUED'),
                'row_version': getattr(self, 'version', 1),
                **({'created_at': self.body['created_at']} if hasattr(self, 'body') else {})}

    async def respond(self, route: Route) -> None:
        """既知の一回の起動以外の mutation は既存の拒否経路へ渡す。"""
        request = route.request
        target = urlsplit(request.url)
        if f'{target.scheme}://{target.netloc}' == self.origin and request.method == 'POST' and target.path == f'{self.prefix}projects/{PROJECT}/task-runs':
            assert request.headers.get('x-csrf-token') == CSRF
            assert request.headers.get('origin') == self.origin
            assert request.headers.get('idempotency-key')
            self.submissions.append(request.post_data_json)
            await route.fulfill(status=201, json=self.run())
            return
        await super().respond(route)


async def emit(page: Page, sequence: int, kind: str, payload: dict) -> None:
    """現在の購読へだけ配送し、status 切替で閉じた source を再利用しない。"""
    await page.wait_for_function('window.streamSources.some(source => !source.closed)')
    await page.evaluate('''([sequence, kind, payload]) => {
      window.emitStream(window.streamSources.findLast(source => !source.closed), sequence, kind, payload);
    }''', [sequence, kind, payload])


async def geometry(page: Page, *, detail: bool = False) -> dict:
    """線の実測間隔と操作の矩形を記録し、CSS 文字列の一致を寸法検証の代用にしない。"""
    await layout(page)
    facts = await page.evaluate('''() => {
      const rect = selector => {
        const element = document.querySelector(selector);
        if (!element) return null;
        const r = element.getBoundingClientRect();
        return {x:r.x, y:r.y, right:r.right, bottom:r.bottom, width:r.width, height:r.height};
      };
      return {tabs:rect('.observationPanel > .tabBar'), progress:rect('.runLiveProgress'),
        content:rect('.observationPanel > .tabPanel'), request:rect('.messageUser'),
        launcher:rect('.runLauncher > button'), navigation:rect('.workspaceRunNavigation'),
        run:rect('.runPanel'), controls:rect('.runLiveActions'),
        buttons:[...document.querySelectorAll('.workspace button, .workspaceRunNavigation a')]
          .filter(el => el.getBoundingClientRect().width && el.getBoundingClientRect().height)
          .map(el => {const r=el.getBoundingClientRect();return {text:el.textContent,x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:r.width};})};
    }''')
    preceding = facts['progress'] or facts['tabs']
    assert preceding and facts['content']['y'] - preceding['bottom'] >= 15.5, facts
    if detail:
        assert facts['progress']['y'] - facts['tabs']['bottom'] >= 15.5, facts
    else:
        assert facts['run']['y'] - max(facts['launcher']['bottom'], facts['navigation']['bottom']) >= 20, facts
        assert facts['progress']['y'] - max(facts['run']['bottom'], facts['controls']['bottom']) >= 19.5, facts
        for button in facts['buttons']:
            assert button['width'] < 300, button
        for index, first in enumerate(facts['buttons']):
            for second in facts['buttons'][index + 1:]:
                assert min(first['right'], second['right']) - max(first['x'], second['x']) <= 1 or min(first['bottom'], second['bottom']) - max(first['y'], second['y']) <= 1, (first, second)
    return facts


async def check(url: str, output: Path) -> None:
    """三語・両テーマ・PC/狭幅で実 submit と段階的な streaming を検証する。"""
    output.mkdir(parents=True, exist_ok=True)
    measurements = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ('zh', 'ja', 'en'):
                for theme in ('dark', 'light'):
                    for width in (1440, 390):
                        name = f'{language}-{theme}-{width}'
                        api = ExecutionApi(url, language)
                        context = await browser.new_context(viewport={'width': 1440, 'height': 900}, reduced_motion='reduce')
                        await context.add_init_script(f'({FAKE_SOURCE})()')
                        await context.route('**/*', api.route)
                        page = await context.new_page()
                        errors: list[str] = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        try:
                            await page.goto(f'{url}#/workspace?project={PROJECT}')
                            labels = await messages(page, language)
                            await page.locator('.themeToggle').get_by_role('button', name=labels['theme'][theme], exact=True).click()
                            await page.set_viewport_size({'width': width, 'height': 900})
                            await page.locator('.runLauncher > button').click()
                            await page.locator('.schemaTextInput').first.fill(PROMPT)
                            await layout(page)
                            # 固定 footer は本文を覆わず、同意と送信の間にも読める余白を残す。
                            assert await page.locator('.launchFooter').evaluate('''el => {
                              const consent=el.querySelector('.submissionAcknowledgement').getBoundingClientRect();
                              const action=el.querySelector('[data-run-submit]').getBoundingClientRect();
                              const body=el.closest('.modalDialog').querySelector('.modalBody').getBoundingClientRect();
                              return action.top - consent.bottom >= 9.5 && el.getBoundingClientRect().top >= body.bottom;
                            }''')
                            await page.screenshot(path=str(output / f'launch-form-{name}.png'))
                            await page.locator('[data-run-submit]').click()
                            await expect(page.get_by_role('dialog')).to_have_count(0)
                            await expect(page.locator('.workspaceActiveRun')).to_be_visible()
                            assert '#/workspace' in page.url and len(api.submissions) == 1
                            assert api.submissions[0]['input']['message'] == PROMPT
                            for sequence, status in enumerate(('QUEUED', 'PREPARING', 'RUNNING'), 1):
                                api.status, api.version = status, sequence
                                api.body['row_version'] = sequence
                                if status == 'RUNNING':
                                    api.body['started_at'] = api.body['created_at']
                                await emit(page, sequence, 'RUN_SNAPSHOT', {'status': status, 'row_version': sequence})
                                await expect(page.locator('.runPanel .statusBadge')).to_have_text(labels['enums']['runStatus'][status])
                                measurements[f'{status}-{name}'] = await geometry(page)
                                await page.screenshot(path=str(output / f'{status.lower()}-{name}.png'))
                            await emit(page, 4, 'TEXT_DELTA', {'text': PARTIAL})
                            await expect(page.locator('.messageStreaming pre')).to_have_text(PARTIAL)
                            await page.locator('.runLiveProgress > summary').click()
                            await expect(page.locator('.runLiveProgress .timeline > li')).to_have_count(3)
                            measurements[f'expanded-{name}'] = await geometry(page)
                            await page.locator('.runLiveProgress > summary').click()
                            await page.locator('.runLiveActions button').first.click()
                            await expect(page.locator('.messageStreaming pre')).to_have_text(PARTIAL)
                            await page.locator('.runLauncher > button').click()
                            await expect(page.get_by_role('dialog')).to_be_visible()
                            await page.keyboard.press('Escape')
                            await expect(page.locator('.messageStreaming pre')).to_have_text(PARTIAL)
                            assert len(api.submissions) == 1
                            await page.screenshot(path=str(output / f'streaming-{name}.png'))
                            await page.locator('.workspaceRunNavigation a').click()
                            await expect(page.locator('.executionDetailPage')).to_be_visible()
                            await page.get_by_role('tab', name=labels['workspace']['tabConversation'], exact=True).click()
                            await emit(page, 5, 'TEXT_DELTA', {'text': PARTIAL})
                            await expect(page.locator('.messageStreaming pre')).to_have_text(PARTIAL)
                            measurements[f'detail-{name}'] = await geometry(page, detail=True)
                            for tab in ('tabConversation', 'tabResult', 'tabEvents'):
                                await page.get_by_role('tab', name=labels['workspace'][tab], exact=False).click()
                                await geometry(page, detail=True)
                            await page.get_by_role('tab', name=labels['workspace']['tabConversation'], exact=True).click()
                            await page.screenshot(path=str(output / f'detail-streaming-{name}.png'))
                            assert not api.mutations() and not api.failures and not api.unexpected and not errors, (api.unexpected, api.failures, errors)
                            print(f'PASS workspace-submit, queued/preparing/running, streaming, refresh, detail-tabs: {name}', flush=True)
                        finally:
                            await context.close()
        finally:
            await browser.close()
    (output / 'measurements.json').write_text(json.dumps(measurements, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    target = urlsplit(args.url)
    if target.scheme != 'http' or target.hostname not in ('127.0.0.1', 'localhost') or not target.path.endswith('/tests/browser/projects.html') or target.query or target.fragment:
        parser.error('Only the owned loopback projects.html mock harness is allowed')
    asyncio.run(check(args.url, args.output))
