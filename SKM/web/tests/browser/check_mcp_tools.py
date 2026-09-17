"""FlaUI 接続の発見・保存・明示操作権を本番画面で隔離検証する。"""

from __future__ import annotations

import argparse
import asyncio
from itertools import product
from pathlib import Path
from urllib.parse import urlsplit

from check_accounts import CSRF, session
from check_projects import PROJECT, messages
from check_resource_editing import EditingApi
from playwright.async_api import async_playwright, expect

TOOLS = ["inspect_window", "get_step_status", "open_application", "execute_step", "cancel_step"]
CATALOG = {
    "server": {"name": "FlaUiMcp", "version": "0.3.0.0"},
    "tools": [
        {
            "name": name,
            "description": name,
            "input_schema": {"type": "object"},
            "output_schema": None,
        }
        for name in TOOLS
    ],
}


class McpApi(EditingApi):
    """秘密や遠端 I/O を含まない保存済み MCP 接続。"""

    def __init__(self, url, language):
        """既存の資源 API double を MCP の空 URI 接続へ変更する。"""
        super().__init__(url, language)
        self.secrets[0].update(provider="mcp", name="FlaUI credential")
        self.config = {"server_url": "https://mcp.example.test/mcp", "transport": "streamable_http"}
        self.integrations[0].update(
            provider="mcp",
            name="FlaUI tools",
            capabilities=["mcp.read/v1"],
            scope={"resource_uris": []},
            config_keys=list(self.config),
        )
        self.discoveries = 0

    async def respond(self, route):
        """発見が credential/URL の再送を求めないことを検査する。"""
        path = urlsplit(route.request.url).path
        if path.endswith("/auth/session"):
            await route.fulfill(json={**session(self.actor, self.role), "mcp_tools_enabled": True})
        elif path.endswith("/mcp-tools/discover"):
            assert route.request.method == "POST"
            assert route.request.headers["x-csrf-token"] == CSRF
            assert route.request.post_data_json == {"expected_revision": 1}
            self.discoveries += 1
            await route.fulfill(
                json={
                    "expected_revision": 1,
                    "catalog": CATALOG,
                    "catalog_hash": "sha256:" + "1" * 64,
                }
            )
        else:
            await super().respond(route)


async def check(url, output):
    """三語・両テーマで発見、読取+提案権、URI 空欄保存を確認する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language, theme in product(("ja", "zh", "en"), ("light", "dark")):
                api = McpApi(url, language)
                context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                await context.add_init_script(f"localStorage.setItem('skillmind.theme', '{theme}')")
                await context.route("**/*", api.route)
                page = await context.new_page()
                errors = []
                page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))
                try:
                    await page.goto(f"{url}#/resources?project={PROJECT}")
                    labels = (await messages(page, language))["resources"]
                    row = page.locator(".resourceItem").filter(has_text="FlaUI tools")
                    await row.get_by_role("button", name=labels["edit"], exact=True).click()
                    dialog = page.get_by_role("dialog")
                    await dialog.get_by_role(
                        "button", name=labels["mcpDiscover"], exact=True
                    ).click()
                    await expect(
                        dialog.get_by_label(labels["mcpEnableTools"], exact=True)
                    ).to_be_checked()
                    for name in TOOLS:
                        await expect(dialog.get_by_text(name, exact=True)).to_be_visible()
                    # 権限 select の値を選び、モデルへ write が直接露出しない構成を保存する。
                    await dialog.get_by_role(
                        "radio", name=labels["accessReadWrite"], exact=True
                    ).check()
                    await page.screenshot(path=str(output / f"mcp-{language}-{theme}.png"))
                    await dialog.get_by_role("button", name=labels["save"], exact=True).click()
                    await expect(dialog).to_have_count(0)
                    assert api.discoveries == 1
                    body = api.updates[-1]
                    assert body["scope"] == {"resource_uris": [], "tool_names": TOOLS}
                    assert body["capabilities"] == ["mcp.tools/v1", "mcp.query/v1", "mcp.call/v1"]
                    assert body["config"]["tool_catalog"] == CATALOG
                    assert not errors and not api.failures and not api.unexpected, (
                        errors,
                        api.failures,
                        api.unexpected,
                    )
                    print(f"PASS MCP tools {language} {theme}", flush=True)
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
        parser.error("Only the loopback projects.html harness is allowed")
    asyncio.run(check(args.url, args.output))
