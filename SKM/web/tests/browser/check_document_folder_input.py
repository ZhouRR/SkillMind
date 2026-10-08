"""upload 先の自由入力・候補 popup の連続 frame と keyboard を隔離 API で検証する。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from check_document_organization import OrganizationApi
from check_projects import PROJECT, messages
from popup_geometry import check_popup_geometry
from playwright.async_api import Route, async_playwright, expect


class FolderInputApi(OrganizationApi):
    """既存の隔離 API に長い候補だけを加え、root と折返し・scroll を同時に検証する。"""

    async def respond(self, route: Route) -> None:
        """候補一覧だけを拡張し、文書や write の契約は共通 fixture に委ねる。"""
        parts = urlsplit(route.request.url).path.removeprefix(self.prefix).split('/')
        if len(parts) == 3 and parts[0] == 'projects' and parts[2] == 'document-folders':
            assert route.request.method == 'GET'
            await route.fulfill(json={'folders': ['specs', 'specs/nested/deep', 'empty/nested', *[
                f'zz-long-{index:02d}-' + 'directory_name_' * 8 for index in range(36)
            ]]})
            return
        await super().respond(route)


async def check(url: str, output: Path, engine: str) -> None:
    """三語・双テーマ・狭幅で既存文書画面を開き、実 write は一切発生させない。"""
    address = urlsplit(url)
    if (address.scheme != 'http' or address.hostname not in {'localhost', '127.0.0.1', '::1'}
            or address.username is not None or address.password is not None or address.fragment
            or not address.path.endswith('/tests/browser/projects.html')):
        raise ValueError('Only the isolated loopback projects.html fixture is supported')
    output.mkdir(parents=True, exist_ok=True)
    results = []
    async with async_playwright() as playwright:
        for browser_name in ('chromium', 'firefox', 'webkit') if engine == 'all' else (engine,):
            browser = await getattr(playwright, browser_name).launch()
            for language in ('ja', 'zh', 'en'):
                for theme in ('light', 'dark'):
                    for width, height in ((1440, 900), (390, 900), (390, 360)):
                        api = FolderInputApi(url, language)
                        context = await browser.new_context(viewport={'width': width, 'height': height})
                        await context.route('**/*', api.route)
                        await context.add_init_script(f"localStorage.setItem('skillmind.theme', '{theme}')")
                        page = await context.new_page()
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        await page.goto(f'{url}#/documents?project={PROJECT}')
                        await expect(page.locator('.documentItem')).to_have_count(1)
                        labels = (await messages(page, language))['documentsPanel']
                        field = page.get_by_role('combobox', name=labels['targetFolder'], exact=True)
                        arrow = page.locator('.documentFolderTrigger')
                        await expect(field).to_have_value('')
                        await expect(page.locator('input[list="document-upload-folders"]')).to_have_count(0)
                        for opening in range(3):
                            await arrow.click()
                            popup = page.locator('.documentFolderPopup')
                            await expect(popup).to_be_visible()
                            await expect(popup.locator('[data-folder-path="specs/nested/deep"]')).to_be_visible()
                            await expect(popup.locator('[data-folder-path="empty/nested"]')).to_be_visible()
                            await expect(popup.locator('[data-folder-path=""]')).to_have_count(1)
                            # 初回 positioning 後に候補が空になったり DOM が差し替わったりしない。
                            frames = await popup.evaluate('''async popup => {
                              const list = popup.querySelector('[role="listbox"]');
                              const input = document.querySelector('.documentFolderInputGroup input');
                              const baseline = list.textContent;
                              const rows = [];
                              for (let i = 0; i < 20; i++) {
                                await new Promise(requestAnimationFrame);
                                const rect = popup.getBoundingClientRect();
                                rows.push({same: popup.isConnected && list === popup.querySelector('[role="listbox"]'),
                                  text: list.textContent === baseline && baseline.length > 0,
                                  expanded: input.getAttribute('aria-expanded'),
                                  visible: getComputedStyle(popup).visibility !== 'hidden' && rect.width > 0 && rect.height > 0,
                                  inside: rect.left >= 0 && rect.right <= innerWidth + 1 && rect.top >= 0 && rect.bottom <= innerHeight + 1});
                              }
                              return rows;
                            }''')
                            assert all(row['same'] and row['text'] and row['visible'] and row['inside']
                                       and row['expanded'] == 'true' for row in frames), frames
                            if opening == 0:
                                long_geometry = await check_popup_geometry(popup, indicators=False, scrollable=True)
                                last = popup.get_by_role('option').last
                                await last.scroll_into_view_if_needed()
                                assert await popup.evaluate('element => element.scrollTop > 0')
                                scrolled_geometry = await check_popup_geometry(popup, indicators=False, scrollable=True)
                                await page.screenshot(path=str(output / f'{browser_name}-{language}-{theme}-{width}x{height}.png'))
                            await field.press('Escape')
                            await expect(field).to_have_attribute('aria-expanded', 'false')
                            await expect(field).to_have_value('')
                        await arrow.click()
                        await page.locator('[data-folder-path="specs/nested/deep"]').click()
                        await expect(field).to_have_value('specs/nested/deep')
                        await expect(field).to_be_focused()
                        await field.fill('new/custom-path')
                        await expect(field).to_have_attribute('aria-expanded', 'true')
                        await field.press('Escape')
                        await field.press('Escape')
                        await expect(field).to_have_value('new/custom-path')
                        await arrow.click()
                        await page.locator('[data-folder-path=""]').click()
                        await expect(field).to_have_value('')
                        # 絞込み後の root と完全一致パスでは scrollbar が不要になっても余白を保つ。
                        await field.fill('specs/nested/deep')
                        popup = page.locator('.documentFolderPopup')
                        await expect(popup).to_be_visible()
                        await expect(popup.get_by_role('option')).to_have_count(2)
                        await expect(popup.locator('[data-folder-path=""]')).to_have_count(1)
                        await expect(popup.locator('[data-folder-path="specs/nested/deep"]')).to_have_count(1)
                        short_geometry = await check_popup_geometry(popup, indicators=False, scrollable=False)
                        await field.press('Escape')
                        await field.fill('specs')
                        await field.press('Tab')
                        await expect(field).to_have_attribute('aria-expanded', 'false')
                        await expect(field).to_have_value('specs')
                        assert not api.operations and not errors, (api.operations, errors)
                        assert not api.unexpected and not api.failures, (api.unexpected, api.failures)
                        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        results.append({'browser': browser_name, 'language': language, 'theme': theme,
                                        'width': width, 'height': height, 'passed': True,
                                        'shortGeometry': short_geometry, 'longGeometry': long_geometry,
                                        'scrolledGeometry': scrolled_geometry})
                        await context.close()
            await browser.close()
    (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')


def main() -> None:
    """許可された local fixture と出力先を明示して実行する。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', type=Path, default=Path('/tmp/skillmind-document-folder-input'))
    parser.add_argument('--browser', choices=('chromium', 'firefox', 'webkit', 'all'), default='all')
    args = parser.parse_args()
    asyncio.run(check(args.url, args.output, args.browser))


if __name__ == '__main__':
    main()
