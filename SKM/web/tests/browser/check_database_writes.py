"""DB 専用 switch と可写列/操作の実 App 提交を mock HTTP で検証する。"""

from __future__ import annotations

import argparse
import asyncio
from itertools import product
from pathlib import Path
from urllib.parse import urlsplit

from check_accounts import session
from check_projects import PROJECT, messages
from check_resource_connections import ConnectionsApi
from playwright.async_api import Route, async_playwright, expect


class DatabaseConnectionsApi(ConnectionsApi):
    """DB 書込みのみを有効にし、無関係な policy 依存も失敗として記録する。"""

    def __init__(self, url: str, language: str) -> None:
        """配備 flags は reload 時の原会話応答だけから更新する。"""
        super().__init__(url, language)
        self.database = True
        self.deferred = False

    async def respond(self, route: Route) -> None:
        """通常の接続 mock を再利用し、秘密を持たない会話の配備上限だけを上書きする。"""
        path = urlsplit(route.request.url).path
        if path == self.prefix + "auth/session":
            await route.fulfill(
                json={
                    **session(),
                    "database_writes_enabled": self.database,
                    "deferred_features_enabled": self.deferred,
                }
            )
            return
        if path.endswith("/effect-preauthorizations") and not self.deferred:
            raise AssertionError("Database switch enabled an unrelated policy dependency")
        await super().respond(route)


async def check(url: str, output: Path) -> None:
    """三語/両 theme/大小画面で DB の明示範囲と独立した後置 gate を確認する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language, theme, width in product(
                ("zh", "ja", "en"), ("light", "dark"), (390, 1440)
            ):
                api = DatabaseConnectionsApi(url, language)
                context = await browser.new_context(viewport={"width": width, "height": 1000})
                await context.add_init_script(f"localStorage.setItem('skillmind.theme','{theme}')")
                await context.route("**/*", api.route)
                page = await context.new_page()
                errors = []
                page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))
                try:
                    await page.goto(f"{url}#/resources?project={PROJECT}")
                    labels = (await messages(page, language))["resources"]
                    await expect(
                        page.get_by_role("tab", name=labels["tabPolicy"], exact=True)
                    ).to_have_count(0)
                    await page.get_by_role(
                        "button", name=labels["connectTitle"], exact=True
                    ).click()
                    dialog = page.get_by_role("dialog")
                    await dialog.get_by_role(
                        "combobox", name=labels["providerLabel"], exact=True
                    ).select_option("postgres")
                    await dialog.get_by_role(
                        "radio", name=labels["accessReadWrite"], exact=True
                    ).check()
                    for key, value in [
                        ("nameLabel", "Database write example"),
                        ("databaseHost", "db.example.test"),
                        ("databaseName", "reports"),
                        ("databaseUser", "writer"),
                        ("secretValueLabel", "fixture-only"),
                    ]:
                        await dialog.get_by_label(labels[key], exact=True).fill(value)
                    await dialog.locator("textarea").nth(0).fill("public.reports")
                    await (
                        dialog.locator("textarea")
                        .nth(1)
                        .fill("public.reports.id\npublic.reports.status")
                    )
                    await dialog.get_by_role("checkbox", name="UPDATE", exact=True).uncheck()
                    await dialog.locator("textarea").nth(1).scroll_into_view_if_needed()
                    await page.screenshot(
                        path=str(output / f"database-{language}-{theme}-{width}.png")
                    )
                    await dialog.get_by_role(
                        "button", name=labels["connectSubmit"], exact=True
                    ).click()
                    await expect(dialog).to_have_count(0)
                    assert len(api.created) == 1
                    created = api.created[0]
                    assert created["capabilities"] == ["database.read/v1", "database.write/v1"]
                    assert created["scope"] == {
                        "tables": ["public.reports"],
                        "write_columns": ["public.reports.id", "public.reports.status"],
                        "operations": ["INSERT"],
                    }
                    await page.get_by_role(
                        "button", name=labels["connectTitle"], exact=True
                    ).click()
                    await dialog.get_by_role(
                        "combobox", name=labels["providerLabel"], exact=True
                    ).select_option("redmine")
                    await expect(dialog.locator('input[name="connect-access"]')).to_have_count(0)
                    # 逆の組合せも実会話の再取得で確認する。後置 switch は DB を開かない。
                    api.database = False
                    api.deferred = True
                    await page.reload()
                    await page.get_by_role(
                        "button", name=labels["connectTitle"], exact=True
                    ).click()
                    await dialog.get_by_role(
                        "combobox", name=labels["providerLabel"], exact=True
                    ).select_option("postgres")
                    await expect(dialog.locator('input[name="connect-access"]')).to_have_count(0)
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    assert not errors and not api.failures and not api.unexpected, (
                        errors,
                        api.failures,
                        api.unexpected,
                    )
                    print(f"PASS database {language}-{theme}-{width}", flush=True)
                finally:
                    await context.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    target = urlsplit(args.url)
    if (
        target.scheme != "http"
        or target.hostname not in {"127.0.0.1", "localhost"}
        or not target.path.endswith("/tests/browser/projects.html")
    ):
        parser.error("Only the loopback projects.html mock harness is allowed")
    asyncio.run(check(args.url, args.output))
