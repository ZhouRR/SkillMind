"""短い field と長い field の幅、popup、keyboard を隔離 fixture の実矩形で確認する。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Page, Route, async_playwright, expect


def fixture_url(url: str, language: str, theme: str) -> str:
    """実 API/配備先への誤実行を拒否する。"""
    parts = urlsplit(url)
    if parts.scheme != 'http' or parts.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('Only a local HTTP fixture URL is allowed')
    if not parts.path.endswith('/tests/browser/short-controls.html') or parts.username or parts.password:
        raise ValueError('Use tests/browser/short-controls.html')
    query = dict(parse_qsl(parts.query))
    query.update(lang=language, theme=theme)
    return urlunsplit(parts._replace(query=urlencode(query), fragment=''))


async def check_fields(page: Page, narrow: bool) -> list[dict]:
    """狭幅では親幅へ戻し、既知の短い欄だけを desktop で制限する。"""
    result = await page.locator('.shortControl, .shortNumberControl').evaluate_all('''elements => elements.map(element => {
      const box = element.getBoundingClientRect(), parent = element.parentElement.getBoundingClientRect();
      return { id: element.id || element.name || element.dataset.fieldName, width: box.width,
        parentWidth: parent.width, height: box.height, rem: parseFloat(getComputedStyle(document.documentElement).fontSize),
        limit: element.classList.contains('shortNumberControl') ? 10 : element.classList.contains('shortControlNarrow') ? 9 : 12,
        overflow: element.scrollWidth > element.clientWidth + 1 };
    })''')
    assert len(result) >= 10, result
    for field in result:
        assert field['height'] >= 42, field
        assert not field['overflow'], field
        if narrow:
            assert abs(field['width'] - field['parentWidth']) <= 1, field
        else:
            assert field['width'] <= field['limit'] * field['rem'] + 1, field
    if not narrow:
        for scope in ['.skillLibraryFilters', '.taskFilters']:
            search = await page.locator(f'{scope} input').bounding_box()
            status = await page.locator(f'{scope} .shortControl').bounding_box()
            assert search and status and search['width'] > status['width'], (search, status)
        score, verdict = await page.locator('#score').bounding_box(), await page.locator('#verdict').bounding_box()
        assert score and verdict and verdict['width'] > score['width'], (score, verdict)
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    return result


async def check_popups(page: Page, touch: bool) -> list[dict]:
    """全短候補の実 popup に到達し、折返し、touch 高、check slot と Escape を確認する。"""
    result = []
    triggers = page.locator('.shortControl')
    for index in range(await triggers.count()):
        trigger = triggers.nth(index)
        await trigger.scroll_into_view_if_needed()
        await trigger.focus()
        await page.keyboard.press('ArrowDown')
        popup = page.locator('.selectPopup')
        await expect(popup).to_be_visible()
        await expect(popup).to_have_attribute('data-density', 'compact')
        await page.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        measured = await popup.evaluate('''element => {
          const box = element.getBoundingClientRect(), style = getComputedStyle(element);
          return { width: box.width, x: box.x, y: box.y, height: box.height,
            viewportWidth: innerWidth, viewportHeight: innerHeight, gutter: style.scrollbarGutter,
            overflow: element.scrollWidth > element.clientWidth + 1,
            scrolls: element.scrollHeight > element.clientHeight + 1,
            items: [...element.querySelectorAll('.selectItem')].map(item => ({
              height: item.getBoundingClientRect().height, overflow: item.scrollWidth > item.clientWidth + 1,
              checkWidth: item.querySelector('.selectItemIndicator').getBoundingClientRect().width,
            })) };
        }''')
        assert not measured['overflow'], measured
        assert measured['x'] >= 0 and measured['y'] >= 0, measured
        assert measured['x'] + measured['width'] <= measured['viewportWidth'] + 1, measured
        assert measured['y'] + measured['height'] <= measured['viewportHeight'] + 1, measured
        assert measured['gutter'] == ('stable both-edges' if measured['scrolls'] else 'auto'), measured
        for item in measured['items']:
            assert item['height'] >= (44 if touch else 36), item
            assert abs(item['checkWidth'] - 16) <= 1 and not item['overflow'], item
        await page.keyboard.press('End')
        await page.keyboard.press('Escape')
        await expect(popup).to_have_count(0)
        await expect(trigger).to_be_focused()
        result.append(measured)
    trigger = page.locator('#task-status')
    await trigger.focus()
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('End')
    await page.keyboard.press('Enter')
    await expect(trigger).to_have_attribute('data-value', 'ARCHIVED')
    await expect(trigger).to_be_focused()
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('Tab')
    await expect(page.locator('.selectPopup')).to_have_count(0)
    await expect(trigger).not_to_be_focused()
    return result


async def check(url: str, output: Path, browsers: list[str], executable: str | None) -> None:
    """三語/双テーマ/通常・狭い・短い画面と touch を別結果として残す。"""
    fixture_url(url, 'en', 'light')
    output.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    async with async_playwright() as playwright:
        for name in browsers:
            try:
                browser = await getattr(playwright, name).launch(executable_path=executable)
            except Exception as error:
                # 起動失敗は環境境界の失敗であり、検証済みとは扱わない。
                results.append({'browser': name, 'passed': False, 'error': str(error)})
                continue
            try:
                for width, height, touch in [(1440, 900, False), (390, 844, False), (390, 360, False), (390, 844, True)]:
                    for language in ['zh', 'ja', 'en']:
                        for theme in ['light', 'dark']:
                            key = f'{name}-{width}x{height}-{touch}-{language}-{theme}'
                            page = await browser.new_page(viewport={'width': width, 'height': height}, has_touch=touch)
                            errors: list[str] = []
                            blocked: list[str] = []
                            page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
                            target = fixture_url(url, language, theme)
                            origin = urlsplit(target)

                            async def only_fixture(route: Route) -> None:
                                """同じ origin の静的資源だけを許し、API と外部通信を拒否する。"""
                                destination = urlsplit(route.request.url)
                                if (destination.scheme, destination.netloc) != (origin.scheme, origin.netloc) or '/api/' in destination.path:
                                    blocked.append(route.request.url)
                                    await route.abort()
                                else:
                                    await route.continue_()

                            await page.route('**/*', only_fixture)
                            result = {'browser': name, 'version': browser.version, 'viewport': [width, height],
                                      'touch': touch, 'language': language, 'theme': theme}
                            try:
                                await page.goto(target)
                                await expect(page.locator('#sort')).to_be_visible()
                                await expect(page.locator('html')).to_have_attribute('lang', language)
                                await expect(page.locator('html')).to_have_attribute('data-theme', theme)
                                await page.evaluate('document.fonts.ready')
                                assert await page.evaluate("matchMedia('(pointer: coarse)').matches") == touch
                                result['fields'] = await check_fields(page, width <= 600)
                                result['popups'] = await check_popups(page, touch)
                                assert not errors and not blocked, (errors, blocked)
                                result['passed'] = True
                                await page.screenshot(path=str(output / f'{key}.png'), full_page=True)
                            except Exception as error:
                                # 各 case の診断を保持し、未実施の残りを成功に数えない。
                                result.update(passed=False, error=str(error), pageErrors=errors, blockedRequests=blocked)
                                await page.screenshot(path=str(output / f'{key}-failure.png'), full_page=True)
                            finally:
                                results.append(result)
                                (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
                                await page.close()
            finally:
                await browser.close()
    (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    if any(not result['passed'] for result in results):
        raise AssertionError('Short control checks failed; inspect results.json')


def main() -> None:
    """Browser/出力先を明示し、生成物を既存 fixture と分離する。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--browser', choices=['chromium', 'firefox', 'webkit', 'all'], default='chromium')
    parser.add_argument('--executable')
    args = parser.parse_args()
    browsers = ['chromium', 'firefox', 'webkit'] if args.browser == 'all' else [args.browser]
    if args.executable and len(browsers) > 1:
        parser.error('--executable requires one browser')
    asyncio.run(check(args.url, args.output, browsers, args.executable))


if __name__ == '__main__':
    main()
