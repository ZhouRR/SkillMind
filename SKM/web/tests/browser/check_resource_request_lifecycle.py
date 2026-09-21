"""資源管理の送信所有権、遅延応答と Secret 中間成功を実 App で検証する。"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from check_accounts import NEXT_PROJECT, ResponseGate
from check_document_upload import UploadBrowserAudit
from check_projects import PROJECT, messages
from check_resource_connections import ConnectionsApi
from check_resource_editing import EditingApi
from playwright.async_api import Route, async_playwright, expect


class DelayedEditingApi(EditingApi):
    """送信済み要求の応答だけを遅らせ、画面を替えても Server 処理は取り消さない。"""

    def __init__(self, url: str, language: str) -> None:
        """実資格情報を持たない既存 fixture と独立した応答 gate を使う。"""
        super().__init__(url, language)
        self.write_gate: ResponseGate | None = None
        self.drop_write = False
        self.attempts = 0

    async def respond(self, route: Route) -> None:
        """既知の request だけを保留し、他 Project の資源は常に空で返す。"""
        suffix = urlsplit(route.request.url).path.removeprefix(self.prefix)
        if suffix.startswith(f"projects/{NEXT_PROJECT}/") and suffix.split('/')[-1] in {
            'integrations', 'secret-references', 'resource-bindings', 'effect-preauthorizations'
        }:
            await route.fulfill(json={"items": []})
            return
        if '/integrations/' in suffix and route.request.method == 'PUT':
            self.attempts += 1
            if self.drop_write:
                await route.fulfill(status=200, content_type='text/plain', body='Fixture unreadable response')
                return
            if self.write_gate:
                self.write_gate.received.set()
                await self.write_gate.release.wait()
            await super().respond(route)
            if self.write_gate:
                self.write_gate.returned.set()
            return
        await super().respond(route)


class PartialConnectionApi(ConnectionsApi):
    """Secret の成功後、接続登録だけを既知拒否にする。"""

    def __init__(self, url: str, language: str) -> None:
        """再試行時に同じ Secret が利用されるか request ごとに観測する。"""
        super().__init__(url, language)
        self.refuse_connection = True
        self.secret_posts = 0
        self.connection_posts: list[dict] = []

    async def respond(self, route: Route) -> None:
        """API 契約と CSRF は既存 fixture で検証し、拒否点だけを追加する。"""
        suffix = urlsplit(route.request.url).path.removeprefix(f'{self.prefix}projects/{PROJECT}/')
        if route.request.method == 'POST':
            if suffix == 'secret-references':
                self.secret_posts += 1
            elif suffix == 'integrations':
                self.connection_posts.append(route.request.post_data_json)
                if self.refuse_connection:
                    await route.fulfill(status=409, json={'status': 409, 'title': 'Conflict',
                        'detail': 'Fixture connection conflict', 'code': 'fixture_conflict'})
                    return
        await super().respond(route)


async def check(url: str, output: Path) -> None:
    """二重 submit、離頁、原 request の失敗、部分成功を三語で確認する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ('ja', 'zh', 'en'):
                api = DelayedEditingApi(url, language)
                context = await browser.new_context(viewport={'width': 1440, 'height': 900})
                await context.route('**/*', api.route)
                # Server に届いた mutation は Browser abort で巻き戻らない境界を再現する。
                await context.add_init_script('''window.resourceRequests=[];
                    const nativeFetch=window.fetch; window.fetch=(url, init={})=>{
                      const write=String(url).includes('/integrations/') && init.method==='PUT';
                      window.resourceRequests.push({url:String(url),method:init.method||'GET'});
                      return nativeFetch(url,write?{...init,signal:undefined}:init);
                    };''')
                page = await context.new_page()
                audit = UploadBrowserAudit(page, api)
                try:
                    await page.goto(f'{url}#/resources?project={PROJECT}')
                    labels = (await messages(page, language))['resources']
                    panel = page.get_by_role('region', name=labels['integrationListTitle'])
                    await expect(panel.get_by_text('Review DB', exact=True)).to_be_visible()
                    await expect(page.locator('.loadingSkeleton')).to_have_count(0)
                    await page.evaluate('window.resourceRequests=[]')
                    await panel.get_by_role('button', name=labels['edit'], exact=True).click()
                    dialog = page.get_by_role('dialog')
                    await expect(dialog).to_be_visible()
                    reads = await page.evaluate('window.resourceRequests.filter(x=>x.method==="GET")')
                    assert len(reads) == 1 and '/integrations/' in reads[0]['url'], reads
                    api.write_gate = ResponseGate()
                    await page.evaluate('''const form=document.querySelector('.resourceForm');
                        form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
                        form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));''')
                    await asyncio.wait_for(api.write_gate.received.wait(), 5)
                    assert api.attempts == 1
                    await expect(dialog.get_by_role('button', name=labels['save'], exact=True)).to_be_disabled()
                    api.write_gate.release.set()
                    await expect(dialog).to_have_count(0)
                    assert len(api.updates) == 1
                    # 新 Project が開いてから古い成功を返しても、旧 dialog/一覧は復活しない。
                    await panel.get_by_role('button', name=labels['edit'], exact=True).click()
                    api.write_gate = None
                    api.drop_write = True
                    await dialog.get_by_label(labels['databaseHost'], exact=True).fill('unknown.example.test')
                    await dialog.get_by_role('button', name=labels['save'], exact=True).click()
                    await expect(dialog.get_by_role('alert')).to_contain_text('API returned a non-JSON response')
                    await expect(dialog.get_by_label(labels['databaseHost'], exact=True)).to_have_value('unknown.example.test')
                    assert api.attempts == 2 and len(api.updates) == 1
                    api.drop_write = False
                    api.write_gate = ResponseGate()
                    await dialog.get_by_label(labels['databaseHost'], exact=True).fill('late.example.test')
                    await dialog.get_by_role('button', name=labels['save'], exact=True).click()
                    await asyncio.wait_for(api.write_gate.received.wait(), 5)
                    await page.evaluate('(hash) => { window.location.hash = hash }', f'#/resources?project={NEXT_PROJECT}')
                    await expect(panel.get_by_text('Review DB', exact=True)).to_have_count(0)
                    api.write_gate.release.set()
                    await asyncio.wait_for(api.write_gate.returned.wait(), 5)
                    await expect(dialog).to_have_count(0)
                    await expect(panel.get_by_text('Review DB', exact=True)).to_have_count(0)
                    assert api.attempts == 3 and len(api.updates) == 2
                    audit.verify()
                finally:
                    if api.write_gate:
                        api.write_gate.release.set()
                    await context.close()

                api2 = PartialConnectionApi(url, language)
                context = await browser.new_context(viewport={'width': 390, 'height': 900})
                await context.route('**/*', api2.route)
                page = await context.new_page()
                audit = UploadBrowserAudit(page, api2)
                try:
                    await page.goto(f'{url}#/resources?project={PROJECT}')
                    await expect(page.locator('.resourceAdmin')).to_be_visible()
                    await page.get_by_role('button', name=labels['connectTitle'], exact=True).click()
                    dialog = page.get_by_role('dialog')
                    await dialog.get_by_role('combobox', name=labels['providerLabel'], exact=True).select_option('postgres')
                    for key, value in (('nameLabel', 'Partial DB'), ('databaseHost', 'db.example.test'),
                                       ('databaseName', 'review'), ('databaseUser', 'reviewer')):
                        await dialog.get_by_label(labels[key], exact=True).fill(value)
                    password = dialog.get_by_label(labels['credentialValueLabels']['password'], exact=True)
                    await password.fill('fixture-only')
                    await dialog.locator('textarea').fill('public.reports')
                    await dialog.get_by_role('button', name=labels['connectSubmit'], exact=True).click()
                    await expect(dialog.get_by_role('alert')).to_contain_text('Fixture connection conflict')
                    await expect(password).to_have_value('')
                    await expect(dialog.get_by_role('combobox', name=labels['credentialLabel'], exact=True)).to_have_value(api2.secrets[0]['secret_reference_id'])
                    assert api2.secret_posts == 1 and len(api2.connection_posts) == 1
                    api2.refuse_connection = False
                    await dialog.get_by_role('button', name=labels['connectSubmit'], exact=True).click()
                    await expect(dialog).to_have_count(0)
                    assert api2.secret_posts == 1 and len(api2.connection_posts) == 2
                    assert api2.connection_posts[0] == api2.connection_posts[1]
                    audit.verify()
                    await page.screenshot(path=str(output / f'resource-lifecycle-{language}.png'))
                finally:
                    await context.close()
                print(f'PASS resource lifecycle {language}', flush=True)
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    target = urlsplit(args.url)
    if target.scheme != 'http' or target.hostname not in {'127.0.0.1', 'localhost'} or not target.path.endswith('/tests/browser/projects.html'):
        parser.error('Only the loopback App mock harness is allowed')
    asyncio.run(check(args.url, args.output))
