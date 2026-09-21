"""共有 action menu の keyboard、portal、native download と元 focus を実 browser で確認する。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def check(url: str, output: Path) -> None:
    """Local fixture だけを表示し、外部通信や業務操作を行わない。"""
    output.mkdir(parents=True, exist_ok=True)
    results = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for width, height in [(1440, 900), (390, 360), (900, 600)]:
                for theme in ['light', 'dark']:
                    page = await browser.new_page(viewport={'width': width, 'height': height})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(url)
                    await page.locator('html').evaluate('(element, theme) => element.dataset.theme = theme', theme)
                    trigger = page.get_by_role('button', name='仕様書.md の操作 / 文档操作 / Document actions', exact=True)
                    menu = page.get_by_role('menu')
                    await trigger.focus()
                    await page.keyboard.press('Enter')
                    await expect(menu).to_be_visible()
                    await expect(page.get_by_role('menuitem', name='Download', exact=True)).to_be_focused()
                    assert await menu.evaluate('element => element.parentElement === document.body')
                    assert await page.locator('details').evaluate('element => element.open')
                    bounds = await menu.bounding_box()
                    assert bounds and bounds['x'] >= 0 and bounds['y'] >= 0
                    assert bounds['x'] + bounds['width'] <= width and bounds['y'] + bounds['height'] <= height
                    await page.keyboard.press('ArrowDown')
                    await expect(page.get_by_role('menuitem', name='Rename', exact=True)).to_be_focused()
                    await page.keyboard.press('End')
                    await expect(page.get_by_role('menuitem', name='Recycle', exact=True)).to_be_focused()
                    await page.keyboard.press('Home')
                    await expect(page.get_by_role('menuitem', name='Download', exact=True)).to_be_focused()
                    await page.keyboard.press('ArrowUp')
                    await expect(page.get_by_role('menuitem', name='Recycle', exact=True)).to_be_focused()
                    await page.keyboard.press('Escape')
                    await expect(menu).to_have_count(0)
                    await expect(trigger).to_be_focused()
                    await page.keyboard.press('Space')
                    await expect(menu).to_be_visible()
                    await page.keyboard.press('Tab')
                    await expect(menu).to_have_count(0)
                    await expect(page.locator('#after')).to_be_focused()
                    await trigger.click()
                    await page.get_by_role('menuitem', name='Rename', exact=True).click()
                    await expect(page.get_by_role('dialog')).to_be_visible()
                    assert await page.get_by_role('dialog').evaluate('element => element.contains(document.activeElement)')
                    await expect(page.locator('#selection')).to_have_text(await trigger.get_attribute('aria-label'))
                    await page.keyboard.press('Escape')
                    await expect(trigger).to_be_focused()
                    await trigger.click()
                    async with page.expect_download() as download_info:
                        await page.get_by_role('menuitem', name='Download', exact=True).press('Space')
                    download = await download_info.value
                    assert download.suggested_filename == 'original.txt'
                    downloaded = output / 'original.txt'
                    await download.save_as(downloaded)
                    assert downloaded.read_bytes() == b'original bytes\n'
                    await trigger.click()
                    await page.locator('#outside').click()
                    await expect(menu).to_have_count(0)
                    await expect(page.locator('#outside')).to_be_focused()
                    for change in ['owner', 'disable']:
                        await trigger.click()
                        await expect(menu).to_be_visible()
                        await page.evaluate('(name) => window.dispatchEvent(new Event(name))', 'fixture-' + change)
                        await expect(menu).to_have_count(0)
                    await expect(trigger).to_be_disabled()
                    await page.evaluate('window.dispatchEvent(new Event("fixture-disable"))')
                    await trigger.click()
                    await expect(menu).to_be_visible()
                    await page.screenshot(path=str(output / f'menu-{width}-{theme}.png'))
                    await page.evaluate('window.dispatchEvent(new Event("fixture-unmount"))')
                    await expect(menu).to_have_count(0)
                    assert not errors, errors
                    results.append({'width': width, 'height': height, 'theme': theme, 'passed': True})
                    await page.close()
        finally:
            await browser.close()
    (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(results))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:5192/skillmind/tests/browser/action-menu.html')
    parser.add_argument('--output', type=Path, default=Path('/tmp/skm-action-menu-browser'))
    args = parser.parse_args()
    asyncio.run(check(args.url, args.output))
