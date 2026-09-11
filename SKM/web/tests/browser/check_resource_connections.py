"""PostgreSQL/MCP の実接続 form と API wire を外部通信なしで検証する。"""

from __future__ import annotations

import argparse
import asyncio
from itertools import product
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from check_accounts import ACTOR, CSRF
from check_document_upload import UploadMockApi
from check_projects import PROJECT, messages
from playwright.async_api import Route, async_playwright, expect


class ConnectionsApi(UploadMockApi):
    """接続と SecretReference の公開 metadata だけを保持する mock。"""

    def __init__(self, url: str, language: str) -> None:
        """Project fixture に空の接続一覧を追加する。"""
        super().__init__(url, language, "resources")
        self.integrations: list[dict] = []
        self.secrets: list[dict] = []
        self.created: list[dict] = []

    async def respond(self, route: Route) -> None:
        """構造化 payload と CSRF を検査し、保存後の再読取を返す。"""
        request = route.request
        suffix = urlsplit(request.url).path.removeprefix(f"{self.prefix}projects/{PROJECT}/")
        catalogs = {
            "integrations": self.integrations,
            "secret-references": self.secrets,
            "resource-bindings": [],
            "effect-preauthorizations": [],
        }
        if suffix not in catalogs:
            await super().respond(route)
            return
        if request.method == "GET":
            await route.fulfill(json={"items": catalogs[suffix]})
            return
        assert request.method == "POST" and request.headers["x-csrf-token"] == CSRF
        body = request.post_data_json
        base = {
            "project_id": PROJECT,
            "created_by": ACTOR,
            "status": "ACTIVE",
            "created_at": "2026-09-11T00:00:00Z",
            "updated_at": "2026-09-11T00:00:00Z",
            "disabled_at": None,
        }
        if suffix == "secret-references":
            assert body["resolver"] == "MANAGED" and body["secret_value"] == "fixture-only"
            result = {
                **base,
                **{key: body[key] for key in ("name", "provider", "resolver", "key_version")},
                "secret_reference_id": str(uuid4()),
            }
            self.secrets.append(result)
        else:
            assert suffix == "integrations"
            self.created.append(body)
            result = {
                **base,
                **{key: value for key, value in body.items() if key != "config"},
                "integration_id": str(uuid4()),
                "revision": 1,
                "config_keys": sorted(body["config"]),
            }
            self.integrations.append(result)
        await route.fulfill(status=201, json=result)


async def check(url: str, output: Path) -> None:
    """三語・両 theme と PC で保存、再読取、provider 切替を確認する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language, theme, width in product(
                ("zh", "ja", "en"), ("light", "dark"), (390, 1366, 1440, 1920)
            ):
                api = ConnectionsApi(url, language)
                context = await browser.new_context(viewport={"width": width, "height": 900})
                await context.add_init_script(f"localStorage.setItem('skillmind.theme', '{theme}')")
                await context.route("**/*", api.route)
                page = await context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))
                try:
                    await page.goto(f"{url}#/resources?project={PROJECT}")
                    labels = (await messages(page, language))["resources"]
                    for provider in ("postgres", "mcp"):
                        await page.get_by_role(
                            "button", name=labels["connectTitle"], exact=True
                        ).click()
                        dialog = page.get_by_role("dialog")
                        await dialog.get_by_role(
                            "combobox", name=labels["providerLabel"], exact=True
                        ).select_option(provider)
                        await dialog.get_by_label(labels["nameLabel"], exact=True).fill(
                            f"{provider} reports"
                        )
                        await expect(dialog.get_by_role("radio")).to_have_count(0)
                        if provider == "postgres":
                            for key, value in (
                                ("databaseHost", "db.example.test"),
                                ("databaseName", "reports"),
                                ("databaseUser", "reader"),
                                ("secretValueLabel", "fixture-only"),
                            ):
                                await dialog.get_by_label(labels[key], exact=True).fill(value)
                            await dialog.locator("textarea").fill("public.reports\npublic.items")
                        else:
                            await dialog.get_by_label(labels["mcpServerUrl"], exact=True).fill(
                                "https://mcp.example.test/mcp"
                            )
                            await dialog.locator("textarea").fill("resource://reports/current")
                        await page.screenshot(
                            path=str(output / f"{provider}-{language}-{theme}-{width}.png")
                        )
                        await dialog.get_by_role(
                            "button", name=labels["connectSubmit"], exact=True
                        ).click()
                        await expect(dialog).to_have_count(0)
                        await expect(
                            page.get_by_role("tabpanel").get_by_text(
                                f"{provider} reports", exact=True
                            )
                        ).to_be_visible()
                    postgres, mcp = api.created
                    assert postgres["config"] == {
                        "host": "db.example.test",
                        "port": 5432,
                        "database": "reports",
                        "username": "reader",
                        "sslmode": "verify-full",
                    }
                    assert postgres["scope"] == {"tables": ["public.reports", "public.items"]}
                    assert postgres["secret_reference_id"] == api.secrets[0]["secret_reference_id"]
                    assert mcp["config"] == {
                        "server_url": "https://mcp.example.test/mcp",
                        "transport": "streamable_http",
                    }
                    assert mcp["scope"] == {"resource_uris": ["resource://reports/current"]}
                    assert mcp["secret_reference_id"] is None
                    assert not errors and not api.failures and not api.unexpected, (
                        errors,
                        api.failures,
                        api.unexpected,
                    )
                    print(f"PASS {language}-{theme}-{width}", flush=True)
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
