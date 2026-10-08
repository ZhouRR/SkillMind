"""共有 Select の DOM popup、実 input、form、modal と実 HomePage を三 browser で隔離検証する。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from popup_geometry import check_popup_geometry
from playwright.async_api import BrowserType, Locator, Page, Route, async_playwright, expect


VIEWPORTS = [(1440, 900), (390, 844), (390, 360)]
LANGUAGES = ['zh', 'ja', 'en']
HOME_LABELS = {'zh': '概览', 'ja': '概要', 'en': 'Overview'}
LANGUAGE_LABELS = {'zh': '中文', 'ja': '日本語', 'en': 'English'}
POPUP = '[data-select-popup]'


def fixture_url(url: str, language: str, theme: str, *, navigation: bool = False) -> str:
    """配備先を誤操作しないよう、専用 loopback fixture 以外は拒否する。"""
    parts = urlsplit(url)
    if parts.scheme != 'http' or parts.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('Only a local HTTP fixture URL is allowed')
    if not parts.path.endswith('/tests/browser/dropdown.html') or parts.username or parts.password:
        raise ValueError('Use the isolated tests/browser/dropdown.html fixture')
    query = dict(parse_qsl(parts.query))
    query.update(lang=language, theme=theme)
    query.pop('navigation', None)
    if navigation:
        query['navigation'] = '1'
    return urlunsplit(parts._replace(query=urlencode(query), fragment=''))


async def center(locator: Locator) -> None:
    """固定 bottom fixture に通常 form の click が重ならない位置へ移す。"""
    await locator.evaluate("element => element.scrollIntoView({block: 'center'})")


async def open_picker(trigger: Locator, page: Page, key: str = 'Space') -> Locator:
    """native select_option/showPicker で代用せず、実 keyboard で DOM popup を開く。"""
    await center(trigger)
    await expect(trigger).to_have_attribute('role', 'combobox')
    await trigger.focus()
    await page.keyboard.press(key)
    await expect(trigger).to_have_attribute('aria-expanded', 'true')
    popup = page.locator(f'{POPUP}:visible')
    await expect(popup).to_have_count(1)
    await expect(popup).to_have_attribute('role', 'listbox')
    await expect(popup.get_by_role('option').first).to_be_visible()
    return popup


async def closed_picker(trigger: Locator, page: Page) -> None:
    """DOM popup が閉じ、trigger の読み上げ状態も同期したことを確認する。"""
    await expect(trigger).to_have_attribute('aria-expanded', 'false')
    await expect(page.locator(f'{POPUP}:visible')).to_have_count(0)


async def choose(trigger: Locator, page: Page, value: str) -> None:
    """実 pointer で option を選び、閉鎖を待つ。"""
    popup = await open_picker(trigger, page)
    await popup.locator(f'[role="option"][data-value="{value}"]').click()
    await closed_picker(trigger, page)


async def form_entries(form: Locator) -> dict[str, str]:
    """hidden control の存在ではなく browser の実 form entry list を検査する。"""
    return await form.evaluate('element => Object.fromEntries(new FormData(element))')


async def popup_style(popup: Locator) -> dict:
    """三 browser 共通の DOM computed style を証拠として採取する。"""
    return await popup.evaluate('''element => {
      const style = getComputedStyle(element);
      return Object.fromEntries(['backgroundColor', 'borderRadius', 'borderTopWidth',
        'boxShadow', 'overflowY', 'maxHeight', 'zIndex'].map(key => [key, style[key]]));
    }''')


async def check_short_popup(page: Page) -> dict:
    """二候補の非 scroll popup でも両側余白と選択有無の本文幅を揃える。"""
    trigger = page.locator('#numeric-select')
    popup = await open_picker(trigger, page)
    geometry = await check_popup_geometry(popup, indicators=True, scrollable=False)
    await page.keyboard.press('Escape')
    await closed_picker(trigger, page)
    return geometry


async def check_form(page: Page) -> None:
    """required/name/numeric/disabled、controlled と uncontrolled reset を実 form で確認する。"""
    form = page.locator('#dropdown-form')
    required = page.locator('#required-select')
    assert not await form.evaluate('element => element.checkValidity()')
    submit = page.locator('#submit-form')
    await center(submit)
    await submit.click()
    await expect(page.locator('#submitted')).to_have_text('')
    await expect(required).to_be_focused()
    await choose(required, page, 'required-beta')
    assert await form.evaluate('element => element.checkValidity()')
    await choose(page.locator('#numeric-select'), page, '2')
    await expect(page.locator('#numeric-selection')).to_have_text('2')
    await center(submit)
    await submit.click()
    await expect(page.locator('#submitted')).not_to_have_text('')
    assert json.loads(await page.locator('#submitted').inner_text()) == {
        'project': 'gamma', 'requiredProject': 'required-beta', 'numericProject': '2',
    }
    disabled = page.locator('#disabled-select')
    await expect(disabled).to_be_disabled()
    changes = await page.locator('#change-log').text_content()
    await center(disabled)
    box = await disabled.bounding_box()
    assert box
    await page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    await page.keyboard.press('ArrowDown')
    await expect(disabled).to_have_attribute('data-value', 'unavailable')
    await expect(page.locator('#change-log')).to_have_text(changes or '')
    await expect(page.locator(f'{POPUP}:visible')).to_have_count(0)
    assert not await disabled.evaluate('element => element === document.activeElement')
    reset = page.locator('#reset-form')
    await center(reset)
    await reset.click()
    await expect(page.locator('#basic-select')).to_have_attribute('data-value', 'alpha')
    await expect(required).to_have_attribute('data-value', '')
    await expect(page.locator('#numeric-selection')).to_have_text('1')
    assert not await form.evaluate('element => element.checkValidity()')
    assert await form_entries(form) == {'project': 'alpha', 'requiredProject': '', 'numericProject': '1'}
    uncontrolled = page.locator('#uncontrolled-select')
    await expect(uncontrolled).to_have_attribute('data-value', 'beta')
    await choose(uncontrolled, page, 'gamma')
    assert await form_entries(page.locator('#uncontrolled-form')) == {'uncontrolledProject': 'gamma'}
    await center(page.locator('#reset-uncontrolled'))
    await page.locator('#reset-uncontrolled').click()
    await expect(uncontrolled).to_have_attribute('data-value', 'beta')
    assert await form_entries(page.locator('#uncontrolled-form')) == {'uncontrolledProject': 'beta'}


async def check_keyboard_pointer(page: Page, output: Path, key: str) -> dict:
    """全 browser で同じ popup、無効候補の拒否、typeahead と一度だけの通知を検証する。"""
    trigger = page.locator('#basic-select')
    await expect(trigger).to_have_accessible_name(await page.locator('label[for="basic-select"]').inner_text())
    await expect(trigger).to_have_attribute('data-value', 'alpha')
    popup = await open_picker(trigger, page)
    await page.screenshot(path=str(output / f'{key}-open.png'))
    appearance = await popup_style(popup)
    appearance['geometry'] = await check_popup_geometry(popup, indicators=True)
    assert appearance['backgroundColor'] not in {'transparent', 'rgba(0, 0, 0, 0)'}, appearance
    assert float(appearance['borderTopWidth'].removesuffix('px')) > 0, appearance
    assert float(appearance['borderRadius'].split()[0].removesuffix('px')) > 0, appearance
    assert appearance['boxShadow'] != 'none', appearance
    selected = popup.get_by_role('option', selected=True)
    await expect(selected).to_have_attribute('data-value', 'alpha')
    await expect(selected.locator('.selectItemIndicator svg')).to_be_visible()
    assert await selected.evaluate('element => getComputedStyle(element).backgroundColor') != 'rgba(0, 0, 0, 0)'
    for value in ['blocked', 'archived-a', 'archived-b']:
        await expect(popup.locator(f'[role="option"][data-value="{value}"]')).to_have_attribute('aria-disabled', 'true')
    await expect(popup.get_by_role('group')).to_have_count(1)
    # Base UI は無効候補も発見できるよう焦点を許すが、Enter では選択しない。
    for value in ['blocked', 'archived-a', 'archived-b']:
        await page.keyboard.press('ArrowDown')
        await expect(popup.locator(f'[role="option"][data-value="{value}"]')).to_be_focused()
        await page.keyboard.press('Enter')
        await expect(trigger).to_have_attribute('aria-expanded', 'true')
        await expect(page.locator('#selection')).to_have_text('alpha')
        await expect(page.locator('#change-count')).to_have_text('0')
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('Enter')
    await closed_picker(trigger, page)
    await expect(page.locator('#selection')).to_have_text('beta')
    await expect(page.locator('#change-count')).to_have_text('1')
    await expect(trigger).to_be_focused()
    popup = await open_picker(trigger, page, 'Enter')
    changed_geometry = await check_popup_geometry(popup, indicators=True)
    assert abs(changed_geometry['items'][0]['textWidth'] - appearance['geometry']['items'][0]['textWidth']) <= 1, changed_geometry
    appearance['changedGeometry'] = changed_geometry
    await page.keyboard.press('ArrowUp')
    await page.keyboard.press('Escape')
    await closed_picker(trigger, page)
    await expect(page.locator('#selection')).to_have_text('beta')
    await expect(page.locator('#change-count')).to_have_text('1')
    await expect(trigger).to_be_focused()
    for keypress, expected_value in [('Home', 'alpha'), ('End', 'gamma'), ('b', 'beta')]:
        await open_picker(trigger, page)
        await page.keyboard.press(keypress)
        await page.keyboard.press('Enter')
        await closed_picker(trigger, page)
        await expect(page.locator('#selection')).to_have_text(expected_value)
    await choose(trigger, page, 'gamma')
    await expect(page.locator('#selection')).to_have_text('gamma')
    # 同じ値を再選択しても callback を重複させない。
    await choose(trigger, page, 'gamma')
    assert json.loads(await page.locator('#change-log').text_content() or 'null') == ['beta', 'alpha', 'gamma', 'beta', 'gamma']
    await open_picker(trigger, page)
    await page.keyboard.press('Tab')
    await closed_picker(trigger, page)
    await expect(page.locator('#after-basic')).to_be_focused()
    await open_picker(trigger, page)
    await page.locator('h1').click()
    await closed_picker(trigger, page)
    await expect(page.locator('#selection')).to_have_text('gamma')
    # theme の変更は同じ trigger と controlled value を維持する。
    assert await trigger.evaluate('''element => {
      const theme = document.documentElement.dataset.theme;
      document.documentElement.dataset.theme = theme === 'light' ? 'dark' : 'light';
      const same = document.getElementById('basic-select') === element && element.dataset.value === 'gamma';
      document.documentElement.dataset.theme = theme;
      return same;
    }''')
    await check_form(page)
    for element_id in ['listbox-select', 'multiple-select']:
        native = page.locator('#' + element_id)
        assert await native.evaluate("element => element.tagName === 'SELECT' && getComputedStyle(element).appearance !== 'base-select'")
        assert await native.locator('option').first.evaluate("element => getComputedStyle(element).display !== 'flex'")
    return appearance


async def check_long_popup(page: Page, output: Path, key: str, width: int, height: int) -> dict:
    """Portal が overflow 親を抜け、長文候補を viewport 内で折返し・scroll できることを確認する。"""
    trigger = page.locator('#long-select')
    popup = await open_picker(trigger, page)
    box = await popup.bounding_box()
    container = await page.locator('#clipped-container').bounding_box()
    assert box and container
    assert box['x'] >= -1 and box['x'] + box['width'] <= width + 1, box
    assert box['y'] >= -1 and box['y'] + box['height'] <= height + 1, box
    assert box['y'] < container['y'], {'popup': box, 'container': container}
    assert not await popup.evaluate('element => document.getElementById("clipped-container").contains(element)')
    appearance = await popup_style(popup)
    assert appearance['overflowY'] in {'auto', 'scroll'}, appearance
    geometry = await check_popup_geometry(popup, indicators=True, scrollable=True)
    first = popup.get_by_role('option').first
    first_box = await first.bounding_box()
    assert first_box
    assert await first.evaluate('element => getComputedStyle(element).whiteSpace') == 'normal'
    assert await first.evaluate('element => getComputedStyle(element).overflowWrap') == 'anywhere'
    assert await popup.evaluate('element => element.scrollWidth <= element.clientWidth + 1')
    await page.screenshot(path=str(output / f'{key}-long-open.png'))
    await page.keyboard.press('End')
    last = popup.get_by_role('option').last
    await expect(last).to_be_focused()
    last_box = await last.bounding_box()
    assert last_box
    assert last_box['y'] >= -1 and last_box['y'] + last_box['height'] <= height + 1, last_box
    assert last_box['x'] >= -1 and last_box['x'] + last_box['width'] <= width + 1, last_box
    assert await popup.evaluate('element => element.scrollTop > 0')
    scrolled_geometry = await check_popup_geometry(popup, indicators=True, scrollable=True)
    # 下敷きや clipping に隠されていないことを hit test でも確認する。
    assert await last.evaluate('''element => {
      const rect = element.getBoundingClientRect();
      return element.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
    }''')
    await page.keyboard.press('Enter')
    await closed_picker(trigger, page)
    await expect(page.locator('#long-selection')).to_have_text('long-35')
    return {'style': appearance, 'popupBounds': box, 'firstBounds': first_box, 'lastBounds': last_box,
            'geometry': geometry, 'scrolledGeometry': scrolled_geometry}


async def check_modal(page: Page, output: Path, key: str) -> None:
    """Modal 内 popup の Tab/ShiftTab、pointer と二段階 Escape の焦点を確認する。"""
    opener = page.locator('#open-modal')
    await center(opener)
    await opener.click()
    dialog = page.get_by_role('dialog')
    await expect(dialog).to_be_visible()
    trigger = page.locator('#modal-select')
    await open_picker(trigger, page)
    await page.screenshot(path=str(output / f'{key}-modal-open.png'))
    for keypress, target in [('Tab', '#modal-after'), ('Shift+Tab', '#modal-before')]:
        if await trigger.get_attribute('aria-expanded') != 'true':
            await open_picker(trigger, page)
        await page.keyboard.press(keypress)
        await closed_picker(trigger, page)
        await expect(page.locator(target)).to_be_focused()
        await expect(dialog).to_be_visible()
    await open_picker(trigger, page)
    await page.keyboard.press('Escape')
    await closed_picker(trigger, page)
    await expect(dialog).to_be_visible()
    await expect(trigger).to_be_focused()
    await choose(trigger, page, 'beta')
    await expect(page.locator('#modal-selection')).to_have_text('beta')
    await expect(dialog).to_be_visible()
    await page.locator('#modal-after').focus()
    await page.keyboard.press('Tab')
    await expect(dialog.get_by_role('button').first).to_be_focused()
    await page.keyboard.press('Shift+Tab')
    await expect(page.locator('#modal-after')).to_be_focused()
    await page.keyboard.press('Escape')
    await expect(dialog).to_be_hidden()
    await expect(opener).to_be_focused()


async def check_navigation(page: Page, url: str, output: Path, key: str, language: str, theme: str, width: int) -> None:
    """本物の HomePage で言語名、見出し、導航の切替と compact Escape を検証する。"""
    await page.goto(fixture_url(url, language, theme, navigation=True))
    await expect(page.locator('html')).to_have_attribute('data-theme', theme)
    toggle = page.locator('.sidebarMenuToggle')
    panel = page.locator('.navigationPanel')
    await expect(page.locator('.homePage h1')).to_have_text(HOME_LABELS[language])
    if width <= 960:
        await expect(toggle).to_be_visible()
        for selector in ['.projectContext [role="combobox"]', '.sidebarLanguage [role="combobox"]']:
            await toggle.click()
            await expect(panel).to_be_visible()
            trigger = panel.locator(selector)
            original = await trigger.get_attribute('data-value')
            await open_picker(trigger, page)
            await page.keyboard.press('Escape')
            await closed_picker(trigger, page)
            await expect(panel).to_be_visible()
            await expect(toggle).to_have_attribute('aria-expanded', 'true')
            await expect(trigger).to_be_focused()
            await expect(trigger).to_have_attribute('data-value', original or '')
            await page.keyboard.press('Escape')
            await expect(panel).to_be_hidden()
            await expect(toggle).to_have_attribute('aria-expanded', 'false')
            await expect(toggle).to_be_focused()
        await toggle.click()
    trigger = panel.locator('.sidebarLanguage [role="combobox"]')
    await expect(trigger).to_contain_text(LANGUAGE_LABELS[language])
    target = LANGUAGES[(LANGUAGES.index(language) + 1) % len(LANGUAGES)]
    popup = await open_picker(trigger, page)
    await expect(popup).to_have_attribute('data-density', 'compact')
    geometry = await check_popup_geometry(popup, indicators=True, scrollable=False)
    assert geometry['reservedInlineSpace'] <= 1, geometry
    bounds = await popup.bounding_box()
    anchor = await trigger.bounding_box()
    assert bounds and anchor
    rem = await page.evaluate('parseFloat(getComputedStyle(document.documentElement).fontSize)')
    assert abs(bounds['width'] - max(anchor['width'], 9 * rem)) <= 1, (bounds, anchor)
    assert bounds['height'] <= 146, bounds
    await page.screenshot(path=str(output / f'{key}-home-language-open.png'))
    await popup.get_by_role('option', name=LANGUAGE_LABELS[target], exact=True).click()
    await closed_picker(trigger, page)
    await expect(trigger).to_have_attribute('data-value', target)
    await expect(trigger).to_contain_text(LANGUAGE_LABELS[target])
    await expect(page.locator('#navigation-language')).to_have_text(target)
    await expect(page.locator('#navigation-language-changes')).to_have_text('1')
    await expect(page.locator('.homePage h1')).to_have_text(HOME_LABELS[target])
    await expect(panel.locator('.sideNav a[aria-current="page"]')).to_have_text(HOME_LABELS[target])
    await expect(page.locator('html')).to_have_attribute('lang', target)
    await page.screenshot(path=str(output / f'{key}-home-language-changed.png'))
    project = panel.locator('.projectContext [role="combobox"]')
    await choose(project, page, '00000000-0000-4000-8000-000000000011')
    await expect(page.locator('#navigation-project')).to_have_text('00000000-0000-4000-8000-000000000011')
    if width <= 960:
        await expect(panel).to_be_hidden()
        await expect(toggle).to_be_focused()


async def check(url: str, output: Path, browser_names: list[str], executable: str | None) -> None:
    """機能不足を native fallback 成功へ降級せず、全 scenario と browser 起動失敗を保存する。"""
    fixture_url(url, 'en', 'light')
    output.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    failures: list[str] = []

    def save_results() -> None:
        """中断時にも完了済み scenario と起動不可の証拠を残す。"""
        (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')

    async with async_playwright() as playwright:
        for browser_name in browser_names:
            browser_type: BrowserType = getattr(playwright, browser_name)
            try:
                browser = await browser_type.launch(executable_path=executable)
            except Exception as error:
                # Runner の起動境界では欠落 browser と権限制限を結果へ残し、別 engine も確認する。
                results.append({'browser': browser_name, 'passed': False, 'status': 'browser-launch-failed', 'error': str(error)})
                failures.append(browser_name + ':launch')
                save_results()
                continue
            try:
                for width, height in VIEWPORTS:
                    for language in LANGUAGES:
                        for theme in ['light', 'dark']:
                            key = f'{browser_name}-{width}x{height}-{language}-{theme}'
                            page = await browser.new_page(viewport={'width': width, 'height': height})
                            errors: list[str] = []
                            blocked: list[str] = []
                            page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
                            target = fixture_url(url, language, theme)
                            origin = urlsplit(target)

                            async def only_fixture(route: Route) -> None:
                                """外部資源と全 API を禁止し、同一 origin の静的 fixture 資源だけを許可する。"""
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
                                await expect(page.locator('html')).to_have_attribute('data-theme', theme)
                                await expect(page.locator('#basic-select')).to_be_visible()
                                result['shortPopup'] = await check_short_popup(page)
                                result['popup'] = await check_keyboard_pointer(page, output, key)
                                result['longPopup'] = await check_long_popup(page, output, key, width, height)
                                await check_modal(page, output, key)
                                result['modal'] = 'passed'
                                await check_navigation(page, url, output, key, language, theme, width)
                                result['homeNavigation'] = 'passed'
                                assert not errors, errors
                                assert not blocked, blocked
                                result['passed'] = True
                            except Exception as error:
                                # Browser 回帰の結果境界で失敗を記録し、他言語・viewport の採取を続ける。
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
        raise AssertionError('Dropdown regression failed: ' + ', '.join(failures))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:5190/skillmind/tests/browser/dropdown.html')
    parser.add_argument('--output', type=Path, default=Path('/tmp/skm-dropdown-browser'))
    parser.add_argument('--browser', choices=['chromium', 'firefox', 'webkit', 'all'], default='all')
    parser.add_argument('--executable', help='Installed browser path for one --browser; otherwise use the Playwright browser')
    args = parser.parse_args()
    if args.executable and args.browser == 'all':
        parser.error('--executable requires a single --browser')
    selected_browsers = ['chromium', 'firefox', 'webkit'] if args.browser == 'all' else [args.browser]
    asyncio.run(check(args.url, args.output, selected_browsers, args.executable))
