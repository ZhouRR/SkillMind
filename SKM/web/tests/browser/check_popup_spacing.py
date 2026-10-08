"""短い/長い popup と一覧の左右余白、modal の見出し整列を合成 fixture で確認する。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Locator, Page, Route, async_playwright, expect


VIEWPORTS = [(1440, 900), (390, 844), (390, 360)]


def fixture_url(url: str, language: str, theme: str) -> str:
    """実配備に触れず、専用 loopback fixture だけを対象にする。"""
    parts = urlsplit(url)
    if parts.scheme != 'http' or parts.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('Only a local HTTP fixture URL is allowed')
    if not parts.path.endswith('/tests/browser/popup-spacing.html') or parts.username or parts.password:
        raise ValueError('Use the isolated tests/browser/popup-spacing.html fixture')
    query = dict(parse_qsl(parts.query))
    query.update(lang=language, theme=theme)
    return urlunsplit(parts._replace(query=urlencode(query), fragment=''))


async def geometry(container: Locator) -> dict:
    """border box から最初の行までの距離を測り、padding 値だけの誤判定を避ける。"""
    return await container.evaluate('''element => {
      const row = element.firstElementChild;
      if (!row) throw new Error('Spacing fixture has no row');
      const outer = element.getBoundingClientRect(), inner = row.getBoundingClientRect();
      const style = getComputedStyle(element);
      return {
        left: inner.left - outer.left, right: outer.right - inner.right,
        scrollHeight: element.scrollHeight, clientHeight: element.clientHeight,
        scrollWidth: element.scrollWidth, clientWidth: element.clientWidth,
        gutter: style.scrollbarGutter, paddingLeft: style.paddingLeft,
        paddingRight: style.paddingRight, overflowY: style.overflowY,
      };
    }''')


def symmetric(measured: dict) -> None:
    """実 scrollbar の幅を含めた外周余白が左右で等しく、横へ溢れないことを要求する。"""
    assert abs(measured['left'] - measured['right']) <= 1, measured
    assert measured['scrollWidth'] <= measured['clientWidth'] + 1, measured


async def check_lists(page: Page) -> dict:
    """通常のユーザー一覧は自然に伸び、有界の監査一覧だけが縦 scroll する。"""
    result = {}
    for length in ['short', 'long']:
        users = page.locator(f'#{length}-users .accountUserList')
        await users.scroll_into_view_if_needed()
        measured = await geometry(users)
        symmetric(measured)
        assert measured['gutter'] == 'auto', measured
        assert measured['paddingLeft'] == measured['paddingRight'] == '0px', measured
        assert measured['scrollHeight'] <= measured['clientHeight'] + 1, measured
        result[f'{length}Users'] = measured
        events = page.locator(f'#{length}-events .accountEventList')
        await events.scroll_into_view_if_needed()
        measured = await geometry(events)
        symmetric(measured)
        assert measured['gutter'] == 'stable both-edges', measured
        assert measured['paddingLeft'] == measured['paddingRight'] == '4px', measured
        assert (measured['scrollHeight'] > measured['clientHeight'] + 1) == (length == 'long'), measured
        if length == 'long':
            await events.focus()
            await page.keyboard.press('End')
            await page.wait_for_function('element => element.scrollTop > 0', arg=await events.element_handle())
            symmetric(await geometry(events))
        result[f'{length}Events'] = measured
    assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
    return result


async def check_modals(page: Page) -> dict:
    """短い modal はタイトルと左端が揃い、長い form は本文内で最後まで到達できる。"""
    result = {}
    for length in ['short', 'long', 'short']:
        trigger = page.locator(f'#open-{length}-modal')
        await trigger.click()
        dialog = page.get_by_role('dialog')
        await expect(dialog).to_be_visible()
        body = dialog.locator('.modalBody')
        measured = await geometry(body)
        assert measured['gutter'] == 'auto', measured
        assert measured['scrollWidth'] <= measured['clientWidth'] + 1, measured
        assert (measured['scrollHeight'] > measured['clientHeight'] + 1) == (length == 'long'), measured
        left_difference = await dialog.evaluate('''element => Math.abs(
          element.querySelector('.modalHeader strong').getBoundingClientRect().left -
          element.querySelector('.modalBody > label').getBoundingClientRect().left)''')
        assert left_difference <= 1, left_difference
        if length == 'short':
            symmetric(measured)
        else:
            await body.locator('input').last.focus()
            await expect(body.locator('input').last).to_be_in_viewport()
            assert await body.evaluate('element => element.scrollTop > 0')
        bounds = await dialog.bounding_box()
        assert bounds and page.viewport_size
        assert bounds['x'] >= 0 and bounds['y'] >= 0, bounds
        assert bounds['x'] + bounds['width'] <= page.viewport_size['width'] + 1, bounds
        assert bounds['y'] + bounds['height'] <= page.viewport_size['height'] + 1, bounds
        await page.keyboard.press('Escape')
        await expect(dialog).not_to_be_visible()
        await expect(trigger).to_be_focused()
        result[length] = {**measured, 'headerLeftDifference': left_difference}
    return result


async def check_menus(page: Page, output: Path, key: str) -> dict:
    """長い menu の実 scroll と折返しを保ち、短い menu も同じ左右余白になる。"""
    result = {}
    for length in ['short', 'long']:
        trigger = page.locator(f'#{length}-menu')
        await trigger.click()
        menu = page.get_by_role('menu')
        await expect(menu).to_be_visible()
        measured = await geometry(menu)
        symmetric(measured)
        assert measured['gutter'] == 'stable both-edges', measured
        assert (measured['scrollHeight'] > measured['clientHeight'] + 1) == (length == 'long'), measured
        assert await menu.evaluate('''element => [...element.querySelectorAll('[role="menuitem"]')]
          .every(item => item.scrollWidth <= item.clientWidth + 1)''')
        bounds = await menu.bounding_box()
        assert bounds and page.viewport_size
        assert bounds['x'] >= 0 and bounds['y'] >= 0, bounds
        assert bounds['x'] + bounds['width'] <= page.viewport_size['width'] + 1, bounds
        assert bounds['y'] + bounds['height'] <= page.viewport_size['height'] + 1, bounds
        await page.keyboard.press('End')
        await expect(menu.get_by_role('menuitem').last).to_be_focused()
        await expect(menu.get_by_role('menuitem').last).to_be_in_viewport()
        if length == 'long':
            assert await menu.evaluate('element => element.scrollTop > 0')
            symmetric(await geometry(menu))
        await page.screenshot(path=str(output / f'{key}-{length}-menu.png'))
        await page.keyboard.press('Escape')
        await expect(menu).to_have_count(0)
        await expect(trigger).to_be_focused()
        await trigger.click()
        await page.keyboard.press('End')
        await page.keyboard.press('Enter')
        await expect(menu).to_have_count(0)
        await expect(page.locator('#selected-action')).to_have_text('close' if length == 'short' else 'item-23')
        await expect(trigger).to_be_focused()
        result[length] = measured
    return result


async def check(url: str, output: Path, browser_names: list[str], executable: str | None) -> None:
    """browser 起動不可も失敗として保存し、三語・双テーマ・PC/狭幅の結果を区別する。"""
    fixture_url(url, 'en', 'light')
    output.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    failures: list[str] = []

    def save_results() -> None:
        """途中で終了しても確認済み case の結果を残す。"""
        (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')

    async with async_playwright() as playwright:
        for browser_name in browser_names:
            try:
                browser = await getattr(playwright, browser_name).launch(executable_path=executable)
            except Exception as error:
                # 起動境界で環境不足を記録する。他 engine の確認を成功扱いで省略しない。
                results.append({'browser': browser_name, 'passed': False, 'status': 'browser-launch-failed', 'error': str(error)})
                failures.append(f'{browser_name}:launch')
                save_results()
                continue
            try:
                for width, height in VIEWPORTS:
                    for language in ['zh', 'ja', 'en']:
                        for theme in ['light', 'dark']:
                            key = f'{browser_name}-{width}x{height}-{language}-{theme}'
                            page = await browser.new_page(viewport={'width': width, 'height': height})
                            errors: list[str] = []
                            blocked: list[str] = []
                            page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
                            target = fixture_url(url, language, theme)
                            origin = urlsplit(target)

                            async def only_fixture(route: Route) -> None:
                                """同一 origin の静的資源だけを許可し、API と外部通信は拒否する。"""
                                destination = urlsplit(route.request.url)
                                if (destination.scheme, destination.netloc) != (origin.scheme, origin.netloc) or '/api/' in destination.path:
                                    blocked.append(route.request.url)
                                    await route.abort()
                                else:
                                    await route.continue_()

                            await page.route('**/*', only_fixture)
                            result = {'browser': browser_name, 'version': browser.version,
                                      'viewport': [width, height], 'language': language, 'theme': theme}
                            try:
                                await page.goto(target)
                                await expect(page.locator('#open-short-modal')).to_be_visible()
                                await expect(page.locator('html')).to_have_attribute('lang', language)
                                await expect(page.locator('html')).to_have_attribute('data-theme', theme)
                                await page.evaluate('document.fonts.ready')
                                result['lists'] = await check_lists(page)
                                result['modals'] = await check_modals(page)
                                result['menus'] = await check_menus(page, output, key)
                                assert not errors, errors
                                assert not blocked, blocked
                                result['passed'] = True
                            except Exception as error:
                                # 回帰の結果境界で診断を保存し、残りの言語・画面を確認する。
                                result.update(passed=False, error=str(error), pageErrors=errors, blockedRequests=blocked)
                                failures.append(key)
                                await page.screenshot(path=str(output / f'{key}-failure.png'))
                            finally:
                                results.append(result)
                                save_results()
                                await page.close()
            finally:
                await browser.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if failures:
        raise AssertionError('Popup spacing regression failed: ' + ', '.join(failures))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:5190/skillmind/tests/browser/popup-spacing.html')
    parser.add_argument('--output', type=Path, default=Path('/tmp/skm-popup-spacing-browser'))
    parser.add_argument('--browser', choices=['chromium', 'firefox', 'webkit', 'all'], default='all')
    parser.add_argument('--executable', help='Installed browser path for one --browser')
    args = parser.parse_args()
    if args.executable and args.browser == 'all':
        parser.error('--executable requires a single --browser')
    selected_browsers = ['chromium', 'firefox', 'webkit'] if args.browser == 'all' else [args.browser]
    asyncio.run(check(args.url, args.output, selected_browsers, args.executable))
