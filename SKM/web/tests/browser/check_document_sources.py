"""実 Workspace/Task Center の文書範囲を、mock API と共有 snapshot parser で検証する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from playwright.async_api import Page, Route, async_playwright, expect

from skillmind.documents.snapshot import DocumentSnapshot, parse_document_selection, parse_document_snapshot

from check_run_submission import ApiFixture, PROJECT, ROOT, confirmed, task_catalog

EXAMPLE = json.loads((ROOT.parent / "contracts/examples/run-detail-documents.v1.json").read_text())
FROZEN = parse_document_snapshot(
    EXAMPLE["document_snapshots"][0]["snapshot"], project_id=UUID(PROJECT), requirement_key="documents"
)


class DocumentApiFixture(ApiFixture):
    """公開候補と作成時の記録を別々に保持し、現在一覧への取り直しを検知する。"""

    def __init__(self, origin: str, required: bool) -> None:
        """Backend で検証する同じ example から、合法候補と本文版を作る。"""

        super().__init__(origin, [])
        self.catalog = task_catalog()
        task = self.catalog["tasks"][0]
        task["input_schema"] = {
            "type": "object", "properties": {"objective": {"type": "string", "title": "Objective"}},
            "required": ["objective"],
        }
        requirement = task["readiness"]["requirements"][0]
        requirement["required"] = required
        requirement["candidates"] = [requirement["candidates"][0]] + [
            {"key": f"document:{item.document_id}", "kind": "document", "provider": "project-documents", "label": item.path}
            for item in FROZEN.documents
        ]
        self.snapshots: dict[str, list] = {}
        self.schedule_posts: list[dict] = []
        self.preview_posts: list[dict] = []

    async def create(self, route: Route) -> None:
        """応答の前に選択を固定し、detail や再送で現在の候補へ戻らない。"""

        body = route.request.post_data_json
        key = route.request.headers["idempotency-key"]
        if key not in self.snapshots:
            token = body["sources"].get("docs")
            snapshots = []
            if token is not None:
                choice = parse_document_selection(token)
                members = FROZEN.documents if choice.mode == "ALL" else tuple(
                    item for item in FROZEN.documents if item.document_id in choice.document_ids
                )
                snapshot = DocumentSnapshot(UUID(PROJECT), "docs", choice.mode, members).to_json()
                parse_document_snapshot(snapshot, project_id=UUID(PROJECT), requirement_key="docs")
                snapshots = [{"requirement_key": "docs", "status": "FROZEN", "snapshot": snapshot}]
            self.snapshots[key] = snapshots
        await super().create(route)

    async def route(self, route: Route) -> None:
        """時間 preview は Run を作らず、調度保存と即時 POST を別々に数える。"""

        request = route.request
        url = urlsplit(request.url)
        if f"{url.scheme}://{url.netloc}" != self.origin:
            await super().route(route)
            return
        if request.method == "GET" and url.path.endswith("/tasks"):
            await route.fulfill(json=self.catalog)
        elif request.method == "GET" and url.path.endswith("/schedules"):
            await route.fulfill(json={"schedules": [], "total": 0, "limit": 100, "offset": 0})
        elif request.method == "POST" and url.path.endswith("/schedules/preview"):
            self.preview_posts.append(request.post_data_json)
            await route.fulfill(json={"occurrences": ["2026-09-09T03:00:00Z"]})
        elif request.method == "POST" and url.path.endswith("/schedules"):
            body = request.post_data_json
            self.schedule_posts.append({"body": body, "csrf": request.headers.get("x-csrf-token")})
            await route.fulfill(status=201, json={
                "cron_expression": None, "run_at": None, "end_at": None, "max_runs": None,
                **{key: value for key, value in body.items() if key != "definition"},
                **body["definition"], "schedule_id": str(uuid4()), "project_id": PROJECT,
                "status": "ACTIVE", "row_version": 1, "run_count": 0, "missed_count": 0,
                "next_run_at": "2026-09-09T03:00:00Z", "last_run_at": None, "last_run_id": None,
                "last_outcome": None, "last_error": None, "created_by": str(uuid4()),
                "created_at": "2026-09-08T00:00:00Z", "updated_at": "2026-09-08T00:00:00Z",
            })
        elif request.method == "GET" and url.path.endswith("/detail"):
            scope, record = next((scope, item) for scope, item in self.runs.items() if item["run_id"] in url.path)
            detail = deepcopy(EXAMPLE)
            detail.update({key: record[key] for key in ("run_id", "project_id", "task_id", "status", "row_version")})
            detail["document_snapshots"] = self.snapshots[scope[-1]]
            detail["selected_sources"] = {"docs": {
                "provider": "project-documents", "capability": "document.read/v1", "resource_kind": "document", "access": "read"
            }} if detail["document_snapshots"] else {}
            await route.fulfill(json=detail)
        elif request.method == "GET" and "/runs/" in url.path and url.path.rsplit("/", 1)[-1] in {item["run_id"] for item in self.runs.values()}:
            record = next(item for item in self.runs.values() if item["run_id"] == url.path.rsplit("/", 1)[-1])
            await route.fulfill(json=record)
        else:
            await super().route(route)


async def select_scope(page: Page, mode: str) -> dict[str, str]:
    """検索しても隠れた選択を保持し、未完成集合の段階では保存しない。"""

    field = page.locator('.documentSourceField')
    await expect(field.locator('select').first).to_have_value('')
    if mode == 'NONE':
        return {}
    await field.locator('select').first.select_option(mode)
    if mode == 'SINGLE':
        value = f'document:{FROZEN.documents[0].document_id}'
        await field.locator('select').nth(1).select_option(value)
        return {'docs': value}
    if mode == 'SET':
        await field.get_by_role('checkbox', name=FROZEN.documents[0].path).check()
        await page.locator('.runForm').evaluate("form => form.dispatchEvent(new Event('submit', {bubbles:true,cancelable:true}))")
        await field.get_by_role('searchbox').fill('notes')
        await field.get_by_role('checkbox', name=FROZEN.documents[1].path).focus()
        await page.keyboard.press('Space')
        await field.get_by_role('searchbox').fill('')
        await expect(field.get_by_role('checkbox', name=FROZEN.documents[0].path)).to_be_checked()
        return {'docs': 'documents:' + ','.join(str(item.document_id) for item in FROZEN.documents)}
    return {'docs': 'project-documents:all'}


async def show_frozen(page: Page, language: str, count: int) -> None:
    """結果と独立した入力事実を開き、元の path/hash と件数を確認する。"""

    title = {'zh': '结果与证据', 'ja': '結果と証拠', 'en': 'Result & evidence'}[language]
    await page.get_by_role('tab', name=title).click()
    await expect(page.locator('.resultView')).to_be_visible()
    if count == 0:
        await expect(page.locator('.frozenDocuments')).to_have_count(0)
        return
    await expect(page.locator('.frozenDocuments')).to_be_visible()
    await page.locator('.frozenDocuments summary').click()
    await expect(page.locator('.frozenDocumentMembers > li')).to_have_count(count)
    await expect(page.locator('.frozenDocuments')).to_contain_text(FROZEN.documents[0].path)
    await expect(page.locator('.frozenDocuments')).to_contain_text(FROZEN.documents[0].content_hash)


async def exercise(page: Page, api: DocumentApiFixture, url: str, screen: str, mode: str, language: str, output: Path | None) -> None:
    """実際の form から送った入力/範囲を検査し、detail の再読込でも元の集合を守る。"""

    await page.goto(url)
    await page.evaluate('next => window.updateSubmissionTestContext(next)', {'screen': screen, 'language': language})
    if screen == 'tasks':
        await page.locator('.taskCard .formRow button').first.click()
    else:
        await page.locator('.runLauncher > button').click()
    await expect(page.get_by_role('dialog')).to_be_visible()
    await page.get_by_label('Objective').fill('Confirmed document input')
    if screen == 'tasks':
        await page.locator('.scheduleConfiguration .formRow button').click()
        await expect(page.locator('.schedulePreview li')).to_have_count(1)
        assert len(api.preview_posts) == 1 and not api.schedule_posts and not api.posts
    elif mode != 'NONE':
        await page.locator('.runForm button[type="submit"]').click()
        assert not api.posts
    sources = await select_scope(page, mode)
    if screen == 'tasks':
        await page.locator('[data-schedule-confirm]').check()
    assert not api.schedule_posts and not api.posts
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if output is not None and mode == 'SET':
        await page.screenshot(path=str(output / f'{screen}-{language}-selection.png'))
    await page.locator('.runForm button[type="submit"]').click()
    if screen == 'tasks':
        await expect(page.get_by_role('dialog')).to_have_count(0)
        assert len(api.schedule_posts) == 1 and not api.posts
        assert api.schedule_posts[0]['body']['sources'] == sources
        assert api.schedule_posts[0]['body']['input'] == {'objective': 'Confirmed document input'}
        assert api.schedule_posts[0]['csrf'] == 'c' * 32
    else:
        await confirmed(page)
        assert len(api.posts) == 1
        body = json.loads(api.posts[0]['body'])
        assert body['sources'] == sources and body['input'] == {'objective': 'Confirmed document input'}
        count = 0 if mode == 'NONE' else 1 if mode == 'SINGLE' else 2
        await show_frozen(page, language, count)
        if output is not None and mode == 'SET':
            await page.screenshot(path=str(output / f'{language}-frozen.png'))
        api.catalog['tasks'][0]['readiness']['requirements'][0]['candidates'] = []
        run = next(iter(api.runs.values()))
        await page.goto(url + '?run=' + run['run_id'])
        await page.evaluate('language => window.updateSubmissionTestContext({language})', language)
        await show_frozen(page, language, count)
        assert len(api.posts) == 1


async def check(url: str, output: Path | None) -> None:
    """24 case を別 context に隔離し、モデル・外部 API・実 DB は使用しない。"""

    address = urlsplit(url)
    if address.scheme != 'http' or address.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('Browser fixture URL must use loopback')
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        for screen in ('workspace', 'tasks'):
            for mode in ('SINGLE', 'SET', 'ALL', 'NONE'):
                for language in ('zh', 'ja', 'en'):
                    context = await browser.new_context(viewport={'width': 390 if language == 'ja' else 1440, 'height': 1000})
                    page = await context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    api = DocumentApiFixture(f'{address.scheme}://{address.netloc}', mode != 'NONE')
                    await context.route('**/*', api.route)
                    try:
                        await exercise(page, api, url, screen, mode, language, output)
                        assert not errors and not api.unexpected, (errors, api.unexpected)
                        print(f'{screen}/{mode}/{language}: passed', flush=True)
                    finally:
                        await context.close()
        await browser.close()


def main() -> None:
    """明示された loopback fixture と任意の外置 screenshot 先だけを受け付ける。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    asyncio.run(check(args.url, args.output))


if __name__ == '__main__':
    main()
