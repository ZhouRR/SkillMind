"""描画待機・翻訳取得・機密入力の非公開を各画面の検証で共有する。"""

from __future__ import annotations

from account_fixture import CSRF, CURRENT_PASSWORD, PASSWORD
from playwright.async_api import Page


async def settle(page: Page) -> None:
    """React の描画と microtask を待ち、固定秒数の sleep を使わない。"""

    await page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )


async def privacy(page: Page) -> None:
    """password/token が URL・storage・可視文へ出ないことを各操作後に検査する。"""

    state = await page.evaluate(
        "() => JSON.stringify({url: location.href, local: {...localStorage}, "
        "session: {...sessionStorage}, text: document.body.innerText})"
    )
    for value in (PASSWORD, CURRENT_PASSWORD, CSRF):
        assert value not in state, "Sensitive fixture input escaped transient form memory"


async def messages(page: Page, language: str) -> dict:
    """実翻訳 catalog を読むので文案を test 側で二重管理しない。"""
    return await page.evaluate(
        "async language => (await import('../../src/lib/i18n/messages.ts')).MESSAGES[language]",
        language,
    )


async def menu(page: Page) -> None:
    """狭幅では実開閉 button を通し、非表示要素の force 操作をしない。"""
    toggle = page.locator(".sidebarMenuToggle")
    if await toggle.is_visible() and await toggle.get_attribute("aria-expanded") == "false":
        await toggle.click()
