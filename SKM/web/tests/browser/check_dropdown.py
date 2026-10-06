"""共有 native select の選択、form 契約、三語・双テーマ・狭幅と modal を隔離 browser で検証する。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import BrowserType, Locator, Page, Route, async_playwright, expect


VIEWPORTS = [(1440, 900), (390, 844), (390, 360)]


def fixture_url(url: str, language: str, theme: str) -> str:
    """配備先を誤操作しないよう、専用 loopback fixture 以外は拒否する。"""
    parts = urlsplit(url)
    if parts.scheme != 'http' or parts.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('Only a local HTTP fixture URL is allowed')
    if not parts.path.endswith('/tests/browser/dropdown.html') or parts.username or parts.password:
        raise ValueError('Use the isolated tests/browser/dropdown.html fixture')
    query = dict(parse_qsl(parts.query))
    query.update(lang=language, theme=theme)
    return urlunsplit(parts._replace(query=urlencode(query), fragment=''))


async def center(locator: Locator) -> None:
    """固定 bottom fixture に通常 form の click が重ならない位置へ移す。"""
    await locator.evaluate("element => element.scrollIntoView({block: 'center'})")


async def open_picker(select: Locator, page: Page) -> None:
    """JS showPicker や select_option で代用せず、実 keyboard で popup を開く。"""
    await center(select)
    await select.focus()
    await page.keyboard.press('Space')
    await page.wait_for_function('(element) => element.matches(":open")', arg=await select.element_handle())


async def closed_picker(select: Locator, page: Page) -> None:
    """Native picker の open 状態を DOM pseudo-class で確認する。"""
    await page.wait_for_function('(element) => !element.matches(":open")', arg=await select.element_handle())


async def picker_style(select: Locator) -> dict:
    """公開 computed style から picker の外観・scroll 上限を採取する。"""
    return await select.evaluate('''element => {
      const style = getComputedStyle(element, '::picker(select)');
      return Object.fromEntries(['appearance', 'width', 'height', 'maxWidth', 'maxHeight',
        'paddingTop', 'paddingBottom', 'paddingLeft', 'paddingRight', 'borderTopWidth',
        'borderBottomWidth', 'borderLeftWidth', 'borderRightWidth', 'backgroundColor',
        'borderRadius', 'boxShadow', 'overflowY'].map(key => [key, style[key]]));
    }''')


async def check_form(page: Page) -> None:
    """Native required/name/value と disabled 除外を実 submit 経由で確認する。"""
    required = page.locator('#required-select')
    assert not await required.evaluate('element => element.checkValidity()')
    submit = page.locator('#submit-form')
    await center(submit)
    await submit.click()
    await expect(page.locator('#submitted')).to_have_text('')
    await expect(required).to_be_focused()
    # 選択 UI の検証とは分け、ここでは form entry list の契約だけを観測する。
    await required.select_option('required-beta')
    await expect(required).to_have_value('required-beta')
    await center(submit)
    await submit.click()
    await expect(page.locator('#submitted')).not_to_have_text('')
    assert json.loads(await page.locator('#submitted').inner_text()) == {
        'project': await page.locator('#basic-select').input_value(),
        'requiredProject': 'required-beta',
    }
    disabled = page.locator('#disabled-select')
    await expect(disabled).to_be_disabled()
    changes = await page.locator('#change-count').inner_text()
    await center(disabled)
    box = await disabled.bounding_box()
    assert box
    await page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    await page.keyboard.press('ArrowDown')
    await expect(disabled).to_have_value('unavailable')
    await expect(page.locator('#change-count')).to_have_text(changes)
    assert not await disabled.evaluate('element => element === document.activeElement')


async def check_enhanced(page: Page, output: Path, key: str, width: int, height: int) -> dict:
    """Customizable select の操作と top-layer popup を実 input event で確認する。"""
    select = page.locator('#basic-select')
    await expect(select).to_have_value('alpha')
    await open_picker(select, page)
    await page.screenshot(path=str(output / f'{key}-open.png'))
    appearance = await picker_style(select)
    assert appearance['appearance'] == 'base-select', appearance
    assert appearance['overflowY'] in {'auto', 'scroll'}, appearance
    checked = select.locator('option:checked')
    checkmark = await checked.evaluate('''element => {
      const style = getComputedStyle(element, '::checkmark');
      return {display: style.display, visibility: style.visibility, opacity: style.opacity, content: style.content};
    }''')
    assert checkmark['display'] != 'none' and checkmark['visibility'] == 'visible', checkmark
    assert float(checkmark['opacity']) > 0, checkmark
    assert await checked.evaluate('element => getComputedStyle(element).backgroundColor') != 'rgba(0, 0, 0, 0)'
    # alpha の次には disabled option と disabled optgroup がある。両方を飛ばして beta を選ぶ。
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('Enter')
    await closed_picker(select, page)
    await expect(select).to_have_value('beta')
    await expect(page.locator('#selection')).to_have_text('beta')
    await expect(page.locator('#change-count')).to_have_text('1')
    await expect(select).to_be_focused()
    await open_picker(select, page)
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('Escape')
    await closed_picker(select, page)
    await expect(select).to_have_value('beta')
    await expect(page.locator('#change-count')).to_have_text('1')
    await expect(select).to_be_focused()
    await open_picker(select, page)
    await select.locator('option[value="gamma"]').click()
    await closed_picker(select, page)
    await expect(select).to_have_value('gamma')
    await expect(page.locator('#selection')).to_have_text('gamma')
    await expect(page.locator('#change-count')).to_have_text('2')
    # 同じ native element を保ったまま theme を切替えても controlled value を保持する。
    assert await select.evaluate('''element => {
      const theme = document.documentElement.dataset.theme;
      document.documentElement.dataset.theme = theme === 'light' ? 'dark' : 'light';
      const unchanged = document.getElementById('basic-select') === element && element.value === 'gamma';
      document.documentElement.dataset.theme = theme;
      return unchanged;
    }''')
    await check_form(page)
    await open_picker(page.locator('#size-one-select'), page)
    await page.keyboard.press('Escape')
    for element_id in ['listbox-select', 'multiple-select']:
        assert await page.locator('#' + element_id).evaluate("element => getComputedStyle(element).appearance") != 'base-select'
        assert await page.locator('#' + element_id + ' option').first.evaluate("element => getComputedStyle(element).display") != 'flex'
    long = page.locator('#long-select')
    await long.focus()
    await page.keyboard.press('Space')
    await page.wait_for_function('document.getElementById("long-select").matches(":open")')
    first = long.locator('option').first
    first_box = await first.bounding_box()
    container = await page.locator('#clipped-container').bounding_box()
    assert first_box and container
    assert first_box['x'] >= 0 and first_box['x'] + first_box['width'] <= width + 1, first_box
    assert first_box['y'] >= 0 and first_box['y'] + first_box['height'] <= height + 1, first_box
    assert first_box['y'] < container['y'], {'first': first_box, 'container': container}
    long_style = await picker_style(long)
    assert long_style['overflowY'] in {'auto', 'scroll'}, long_style
    assert await first.evaluate('element => getComputedStyle(element).whiteSpace') == 'normal'
    assert await first.evaluate('element => getComputedStyle(element).overflowWrap') == 'anywhere'
    await page.screenshot(path=str(output / f'{key}-long-open.png'))
    await page.keyboard.press('End')
    # Native End が末尾を focus し、picker 内を scroll して画面内へ露出する。
    last = long.locator('option').last
    await expect(last).to_be_focused()
    last_box = await last.bounding_box()
    assert last_box and last_box['y'] >= 0 and last_box['y'] + last_box['height'] <= height + 1, last_box
    assert last_box['x'] >= 0 and last_box['x'] + last_box['width'] <= width + 1, last_box
    moved_first = await first.bounding_box()
    assert moved_first and moved_first['y'] < first_box['y'], {'before': first_box, 'after': moved_first}
    await page.keyboard.press('Enter')
    await expect(long).to_have_value('long-35')
    await closed_picker(long, page)
    opener = page.locator('#open-modal')
    await center(opener)
    await opener.click()
    dialog = page.get_by_role('dialog')
    await expect(dialog).to_be_visible()
    modal_select = page.locator('#modal-select')
    await open_picker(modal_select, page)
    await page.keyboard.press('Escape')
    await closed_picker(modal_select, page)
    # Escape 一回目は picker だけを閉じ、二回目で ModalDialog を閉じる必要がある。
    await expect(dialog).to_be_visible()
    await expect(modal_select).to_be_focused()
    await open_picker(modal_select, page)
    await modal_select.locator('option[value="beta"]').click()
    await expect(page.locator('#modal-selection')).to_have_text('beta')
    await expect(dialog).to_be_visible()
    await page.keyboard.press('Escape')
    await expect(dialog).to_be_hidden()
    await expect(opener).to_be_focused()
    return {'picker': appearance, 'checkmark': checkmark, 'longPicker': long_style,
            'visibleOptionBounds': {'first': first_box, 'lastAfterScroll': last_box}}


async def check_navigation(page: Page, url: str, output: Path, key: str) -> None:
    """本物の緊凑導航でも第一 Escape は picker、第二 Escape は導航を閉じる。"""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query['navigation'] = '1'
    await page.goto(urlunsplit(parts._replace(query=urlencode(query))))
    toggle = page.locator('.sidebarMenuToggle')
    panel = page.locator('.navigationPanel')
    await expect(toggle).to_be_visible()
    for selector in ['.projectContext select', '.sidebarLanguage select']:
        await toggle.click()
        await expect(panel).to_be_visible()
        select = panel.locator(selector)
        value = await select.input_value()
        await open_picker(select, page)
        await page.screenshot(path=str(output / f'{key}-navigation-{ "project" if "projectContext" in selector else "language" }-open.png'))
        await page.keyboard.press('Escape')
        await closed_picker(select, page)
        await expect(panel).to_be_visible()
        await expect(toggle).to_have_attribute('aria-expanded', 'true')
        await expect(select).to_be_focused()
        await expect(select).to_have_value(value)
        await page.keyboard.press('Escape')
        await expect(panel).to_be_hidden()
        await expect(toggle).to_have_attribute('aria-expanded', 'false')
        await expect(toggle).to_be_focused()


async def check(url: str, output: Path, browser_name: str, executable: str | None, require_enhanced: bool) -> None:
    """各 scenario の失敗も保存し、fallback を enhanced 成功と誤記しない。"""
    fixture_url(url, 'en', 'light')
    output.mkdir(parents=True, exist_ok=True)
    results = []
    failures = []
    async with async_playwright() as playwright:
        browser_type: BrowserType = getattr(playwright, browser_name)
        browser = await browser_type.launch(executable_path=executable)
        try:
            for width, height in VIEWPORTS:
                for language in ['zh', 'ja', 'en']:
                    for theme in ['light', 'dark']:
                        key = f'dropdown-{width}x{height}-{language}-{theme}'
                        page = await browser.new_page(viewport={'width': width, 'height': height})
                        errors: list[str] = []
                        page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
                        target = fixture_url(url, language, theme)
                        origin = urlsplit(target)
                        blocked: list[str] = []

                        async def only_fixture(route: Route) -> None:
                            """外部 resource と API は禁止し、fixture の同一 origin 静的資源だけを許可する。"""
                            destination = urlsplit(route.request.url)
                            if (destination.scheme, destination.netloc) != (origin.scheme, origin.netloc) or '/api/' in destination.path:
                                blocked.append(route.request.url)
                                await route.abort()
                            else:
                                await route.continue_()

                        await page.route('**/*', only_fixture)
                        result = {'viewport': [width, height], 'language': language, 'theme': theme,
                                  'browser': browser_name, 'version': browser.version}
                        try:
                            await page.goto(target)
                            await expect(page.locator('#basic-select')).to_be_visible()
                            supported = await page.evaluate("CSS.supports('appearance', 'base-select') && CSS.supports('selector(select::picker(select))')")
                            appearance = await page.locator('#basic-select').evaluate('element => getComputedStyle(element).appearance')
                            result.update(supportsBaseSelect=supported, appearance=appearance)
                            if supported:
                                assert appearance == 'base-select', 'Enhancement did not apply in a supporting browser'
                                result['observations'] = await check_enhanced(page, output, key, width, height)
                                result['enhanced'] = 'passed'
                                if width <= 960:
                                    await check_navigation(page, target, output, key)
                                    result['compactNavigation'] = 'passed'
                            else:
                                assert not require_enhanced, 'This browser does not support customizable selects'
                                await page.locator('#basic-select').focus()
                                await page.keyboard.press('ArrowDown')
                                await expect(page.locator('#basic-select')).to_have_value('beta')
                                await expect(page.locator('#selection')).to_have_text('beta')
                                await check_form(page)
                                await page.screenshot(path=str(output / f'{key}-native-fallback.png'))
                                result['enhanced'] = 'not supported; native keyboard/form fallback verified'
                            assert not errors, errors
                            assert not blocked, blocked
                            result['passed'] = True
                        except Exception as error:
                            # Browser 回帰の結果境界で失敗を記録し、他言語・viewport の証拠採取を続ける。
                            result.update(passed=False, error=str(error), pageErrors=errors, blockedRequests=blocked)
                            failures.append(key)
                            await page.screenshot(path=str(output / f'{key}-failure.png'))
                        finally:
                            results.append(result)
                            (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
                            await page.close()
        finally:
            await browser.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if failures:
        raise AssertionError('Dropdown regression failed: ' + ', '.join(failures))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:5190/skillmind/tests/browser/dropdown.html')
    parser.add_argument('--output', type=Path, default=Path('/tmp/skm-dropdown-browser'))
    parser.add_argument('--browser', choices=['chromium', 'firefox', 'webkit'], default='chromium')
    parser.add_argument('--executable', help='Optional installed browser path; otherwise use the Playwright browser')
    parser.add_argument('--require-enhanced', action='store_true', help='Fail rather than accept native fallback when base-select is unsupported')
    args = parser.parse_args()
    asyncio.run(check(args.url, args.output, args.browser, args.executable, args.require_enhanced))
