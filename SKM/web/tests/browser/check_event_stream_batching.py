"""実 Workspace と全面 mock API で burst・終態・Run 切替の本文保存を検証する。"""

from __future__ import annotations

import argparse
import asyncio
from urllib.parse import urlsplit

from playwright.async_api import Route, async_playwright, expect

from check_run_submission import ApiFixture, PROJECT, TASK

RUN_A = "00000000-0000-4000-8000-000000000081"
RUN_B = "00000000-0000-4000-8000-000000000082"


class StreamFixture(ApiFixture):
    """元の認証/Run fixture を再利用し、読取と取消だけ追加する。"""

    def __init__(self, origin: str) -> None:
        super().__init__(origin, [])
        for run_id in (RUN_A, RUN_B):
            self.runs[(run_id,)] = {
                "run_id": run_id, "project_id": PROJECT, "task_id": TASK,
                "status": "RUNNING", "row_version": 1,
                "created_at": "2026-09-08T00:00:00Z", "idempotent_replay": False,
            }

    async def route(self, route: Route) -> None:
        """Run 読取を mock し、その他は既存の外部アクセス禁止を使う。"""
        target = urlsplit(route.request.url)
        if f"{target.scheme}://{target.netloc}" != self.origin:
            await super().route(route)
            return
        path = target.path
        for run_id in (RUN_A, RUN_B):
            if path.endswith(f"/runs/{run_id}") and route.request.method == "GET":
                await route.fulfill(json=self.runs[(run_id,)])
                return
        await super().route(route)


FAKE_SOURCE = """() => {
  window.streamSources = [];
  window.EventSource = class extends EventTarget {
    constructor(url) { super(); this.url = url; this.closed = false; window.streamSources.push(this); }
    close() { this.closed = true; }
  };
  window.emitStream = (source, sequence, type, payload) => {
    const runId = source.url.split('/runs/')[1].split('/')[0];
    source.dispatchEvent(new MessageEvent(type.toLowerCase().replaceAll('_', '.'), { data: JSON.stringify({
      run_id: runId, sequence, event_type: type, occurred_at: '2026-09-08T00:00:00Z',
      run_attempt_id: null, agent_session_id: null, trace_id: null, payload,
    }) }));
  };
}"""


async def check(url: str, executable: str | None) -> None:
    """同じ call stack の大量配送と React effect cleanup を実 browser で照合する。"""
    target = urlsplit(url)
    api = StreamFixture(f"{target.scheme}://{target.netloc}")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=executable)
        context = await browser.new_context()
        await context.add_init_script(f"({FAKE_SOURCE})()")
        await context.route("**/*", api.route)
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(f"{url}?run={RUN_A}")
            await page.wait_for_function("window.updateSubmissionTestContext !== undefined")
            await page.evaluate("window.updateSubmissionTestContext({detailView:true})")
            await page.wait_for_function("window.streamSources.some(s => !s.closed)")
            # rAF が進まない環境でも短い timer により partial が配送される。
            await page.evaluate("""() => {
              window.originalRAF = window.requestAnimationFrame;
              window.requestAnimationFrame = () => 123;
              window.a = window.streamSources.findLast(s => !s.closed);
              window.emitStream(window.a, 1, 'TEXT_DELTA', {text:'pending text'});
            }""")
            await expect(page.locator('.messageStreaming pre')).to_have_text('pending text')
            # status effect が再作成されても pending text を失わず、replay cursor は durable のみ。
            await page.evaluate("""() => {
              window.emitStream(window.a, 2, 'TEXT_DELTA', {text:' plus'});
              window.emitStream(window.a, 3, 'RUN_SNAPSHOT', {status:'WAITING_FOR_INPUT', row_version:2});
            }""")
            await expect(page.locator('.messageStreaming pre')).to_have_text('pending text plus')
            await page.wait_for_function(
                "window.streamSources.some(s => !s.closed && s.url.endsWith('after=3'))",
                polling=20,
            )
            # old timer・closed EventSource を意図的に遅配し、次 Run へ混入しないことを確認する。
            await page.evaluate("""(id) => {
              window.old = window.streamSources.findLast(s => !s.closed);
              window.emitStream(window.old, 4, 'TEXT_DELTA', {text:'STALE_PENDING'});
              window.updateSubmissionTestContext({initialRunId:id});
            }""", RUN_B)
            await page.wait_for_function(
                "(id) => window.streamSources.some(s => !s.closed && s.url.includes(id))",
                arg=RUN_B, polling=20,
            )
            await page.evaluate("""() => {
              window.emitStream(window.old, 5, 'TEXT_COMPLETED', {text:'STALE_LATE'});
              window.b = window.streamSources.findLast(s => !s.closed);
              for (let i=1; i<=10000; i++) window.emitStream(window.b,i,'TEXT_DELTA',{text:'x'});
              window.emitStream(window.b,10001,'TEXT_COMPLETED',{text:'x'.repeat(10000)});
              window.emitStream(window.b,10002,'RUN_SNAPSHOT',{status:'CANCELLED',row_version:4});
            }""")
            await expect(page.locator('.messageAgent pre')).to_have_count(1)
            await expect(page.locator('.messageAgent pre')).to_have_text('x' * 10000)
            await expect(page.locator('.messageStreaming')).to_have_count(0)
            assert await page.evaluate('window.b.closed')
            assert 'STALE' not in await page.locator('body').inner_text()
            await page.evaluate('window.requestAnimationFrame = window.originalRAF')
            await page.get_by_role('tab').last.click()
            await expect(page.locator('.timeline > li')).to_have_count(2)
            # アンマウント済み subscription へ追加しても例外や新しい UI は発生しない。
            await page.evaluate("window.updateSubmissionTestContext({screen:'tasks'})")
            await page.evaluate("window.emitStream(window.b,10003,'TEXT_DELTA',{text:'UNMOUNTED'})")
            assert not errors and not api.unexpected, (errors, api.unexpected)
            print('PASS Workspace timer fallback, same-run flush/reconnect, run switch, 10k burst, cancellation/terminal full text, unmount')
        finally:
            await context.close()
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--executable')
    args = parser.parse_args()
    target = urlsplit(args.url)
    if (target.scheme != 'http' or target.hostname not in {'127.0.0.1', 'localhost'}
            or not target.path.endswith('/tests/browser/run-submission.html')):
        parser.error('Only loopback run-submission.html is allowed')
    asyncio.run(check(args.url, args.executable))
