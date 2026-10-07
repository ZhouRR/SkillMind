"""共有 Select の popup を実操作し、候補の検査と値の選択を共用する。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import Locator, expect


@asynccontextmanager
async def select_options(control: Locator) -> AsyncIterator[Locator]:
    """対象 trigger の popup だけを開き、検査後は Escape で元の画面へ戻す。"""
    if await control.evaluate("element => element.tagName === 'SELECT'"):
        yield control.locator("option")
        return

    await expect(control).to_have_attribute("role", "combobox")
    if await control.get_attribute("aria-expanded") != "true":
        await control.click()
    await expect(control).to_have_attribute("aria-expanded", "true")
    popup_id = await control.get_attribute("aria-controls")
    assert popup_id, "Select must identify its open popup with aria-controls"
    popup = control.page.locator(f"[id={json.dumps(popup_id)}]")
    await expect(popup).to_be_visible()
    try:
        yield popup.get_by_role("option")
    finally:
        if await control.count() and await control.get_attribute("aria-expanded") == "true":
            await control.page.keyboard.press("Escape")
            await expect(popup).not_to_be_visible()


async def select_option(control: Locator, value: str) -> None:
    """状態注入を使わず、利用者と同じ trigger と候補の click で値を選ぶ。"""
    if await control.evaluate("element => element.tagName === 'SELECT'"):
        # 専用 harness に意図して残す native select だけは Playwright の標準 API を使う。
        await control.select_option(value)
        return
    async with select_options(control) as options:
        option = options.and_(control.page.locator(f"[data-value={json.dumps(value)}]"))
        await expect(option).to_have_count(1)
        await option.click()


async def count_select_options(control: Locator) -> int:
    """閉じた popup の DOM 構造に依存せず、利用可能な候補数を取得する。"""
    async with select_options(control) as options:
        return await options.count()
