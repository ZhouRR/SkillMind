"""待機 Run の履歴遷移と未有効化 Skill の操作配置を実 App で検証する。"""
import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlsplit
from check_projects import PROJECT, layout, messages
from check_skill_library_delete import LibraryApi
from check_workspace_reports import WorkspaceApi
from playwright.async_api import async_playwright, expect


async def check(url, output):
    """三語・両テーマで復帰導線と長いボタンの読みやすさを確認する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            for language in ('ja', 'zh', 'en'):
                api = WorkspaceApi(url, language)
                context = await browser.new_context(viewport={'width': 1440, 'height': 900})
                await context.route('**/*', api.route)
                page = await context.new_page()
                await page.goto(f'{url}#/workspace?project={PROJECT}')
                await expect(page.locator('.workspaceQueue .pendingItem')).to_have_count(1)
                await page.locator('.workspaceQueue .pendingItem').click()
                await expect(page.locator('main')).to_have_attribute('data-page', 'history')
                await expect(page.locator('.sideNav a[aria-current="page"]')).to_have_attribute('href', f'#/history?project={PROJECT}')
                await expect(page.locator('.runFacts')).to_be_visible()
                await page.locator('.sideNavSubItem').first.click()
                await expect(page.locator('main')).to_have_attribute('data-page', 'workspace')
                await expect(page.locator('.workspaceQueue')).to_be_visible()
                # 概要の最近実行と対応待ちは同じ履歴詳細を開き、戻る/再読込でも所属を保つ。
                for selector in ('.homeRunItem', '.homeAttention .pendingItem'):
                    await page.goto(f'{url}#/?project={PROJECT}')
                    link = page.locator(selector).first
                    await expect(link).to_be_visible()
                    target = await link.get_attribute('href')
                    assert target.startswith(f'#/history?project={PROJECT}&run='), target
                    await link.click()
                    await expect(page.locator('main')).to_have_attribute('data-page', 'history')
                    await expect(page.locator('.sideNav a[aria-current="page"]')).to_have_attribute('href', f'#/history?project={PROJECT}')
                    await page.reload()
                    await expect(page.locator('main')).to_have_attribute('data-page', 'history')
                    await page.locator('.sideNavSubItem').first.click()
                    await expect(page.locator('main')).to_have_attribute('data-page', 'workspace')
                    # 古い Bookmark も入口ごとの修正に依存せず履歴へ正規化する。
                    await page.goto(f'{url}{target.replace("#/history?", "#/workspace?")}')
                    await expect(page.locator('main')).to_have_attribute('data-page', 'history')
                assert not api.failures and not api.unexpected
                await context.close()
                for width in (1440, 1366, 390):
                    api = LibraryApi(url, language)
                    api.versions[0]['status'] = 'PUBLISHED'
                    api.versions[0]['description'] = 'テスト仕様書の取得、レビュー、結果の保存と報告を一つのタスクで実行します。' * 3
                    context = await browser.new_context(viewport={'width': width, 'height': 900})
                    await context.route('**/*', api.route)
                    page = await context.new_page()
                    await page.goto(f'{url}#/skills?project={PROJECT}')
                    labels = await messages(page, language)
                    card = page.locator('.skillLibraryList > li').first
                    actions = card.locator('.skillActions')
                    await expect(actions.get_by_role('button', name=labels['skills']['enableForProject'], exact=True)).to_be_visible()
                    await expect(actions.get_by_role('button', name=labels['skills']['deprecateVersion'], exact=True)).to_be_visible()
                    for theme in ('dark', 'light'):
                        await page.set_viewport_size({'width': 1440, 'height': 900})
                        await page.locator('.themeToggle').get_by_role('button', name=labels['theme'][theme], exact=True).click()
                        await page.set_viewport_size({'width': width, 'height': 900})
                        await layout(page)
                        boxes = await actions.locator('button').evaluate_all('els => els.map(e => ({width:e.clientWidth, scroll:e.scrollWidth, height:e.getBoundingClientRect().height, y:e.getBoundingClientRect().y}))')
                        assert all(b['width'] >= b['scroll'] and b['height'] < 65 for b in boxes), boxes
                        if width > 1000:
                            assert abs(boxes[0]['y'] - boxes[1]['y']) < 2, boxes
                            identity = await card.locator('.skillLibraryIdentity').bounding_box()
                            action_box = await actions.bounding_box()
                            assert action_box['y'] >= identity['y'] + identity['height']
                        await page.screenshot(path=str(output / f'library-{language}-{theme}-{width}.png'))
                    assert not api.failures and not api.unexpected
                    await context.close()
                print(f'PASS navigation and Skill actions {language}', flush=True)
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    address = urlsplit(args.url)
    if address.hostname not in ('127.0.0.1', 'localhost') or not address.path.endswith('/tests/browser/projects.html'):
        parser.error('Only the loopback App harness is permitted')
    asyncio.run(check(args.url, args.output))
