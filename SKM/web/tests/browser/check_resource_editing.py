"""資源の編集・削除を実画面と mock HTTP で検証する。"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from check_accounts import ACTOR, CSRF
from check_projects import PROJECT, messages
from check_resource_connections import ConnectionsApi
from playwright.async_api import Route, async_playwright, expect


class EditingApi(ConnectionsApi):
    """参照保護と version を持つ編集・削除 API double。"""

    def __init__(self, url: str, language: str) -> None:
        """一つの接続と、その認証情報を登録して開始する。"""
        super().__init__(url, language)
        base = {"project_id": PROJECT, "created_by": ACTOR, "status": "ACTIVE",
                "created_at": "2026-09-11T00:00:00Z", "updated_at": "2026-09-11T00:00:00Z",
                "disabled_at": None}
        self.secrets = [{**base, "secret_reference_id": str(uuid4()), "name": "Review password",
                         "provider": "postgres", "resolver": "MANAGED", "key_version": "v1"}]
        self.config = {"host": "db.example.test", "port": 15432, "database": "reviews",
                       "username": "reviewer", "sslmode": "require"}
        self.integrations = [{**base, "integration_id": str(uuid4()), "name": "Review DB",
                              "kind": "other", "provider": "postgres", "revision": 1,
                              "capabilities": ["database.read/v1"],
                              "scope": {"tables": ["public.reports"]},
                              "config_keys": sorted(self.config),
                              "secret_reference_id": self.secrets[0]["secret_reference_id"]}]
        self.secret_updates: list[dict] = []
        self.updates: list[dict] = []
        self.deletes: list[str] = []
        self.refuse_delete = True

    async def respond(self, route: Route) -> None:
        """原 version、CSRF と省略値の意味を検証して結果を返す。"""
        request = route.request
        parsed = urlsplit(request.url)
        suffix = parsed.path.removeprefix(f"{self.prefix}projects/{PROJECT}/")
        parts = suffix.split("/")
        if len(parts) != 2 or parts[0] not in {"integrations", "secret-references"}:
            await super().respond(route)
            return
        is_secret = parts[0] == "secret-references"
        rows = self.secrets if is_secret else self.integrations
        key = "secret_reference_id" if is_secret else "integration_id"
        item = next(row for row in rows if row[key] == parts[1])
        if request.method == "GET":
            assert not is_secret
            await route.fulfill(json={"integration": item, "config": self.config})
            return
        assert request.headers["x-csrf-token"] == CSRF
        if request.method == "DELETE":
            params = parse_qs(parsed.query)
            assert params["expected_updated_at" if is_secret else "expected_revision"] == [
                item["updated_at"] if is_secret else str(item["revision"])]
            if self.refuse_delete:
                await route.fulfill(status=409, json={"title": "Resource is referenced",
                    "status": 409, "detail": "Integration is referenced; disable it instead",
                    "code": "integration_resource_in_use"})
                return
            self.deletes.append(parts[0])
            rows.remove(item)
            await route.fulfill(status=204)
            return
        body = request.post_data_json
        if is_secret:
            assert request.method == "PATCH"
            assert body["expected_updated_at"] == item["updated_at"]
            self.secret_updates.append(body)
            item.update(name=body["name"], key_version=body["key_version"],
                        updated_at="2026-09-12T00:00:00Z")
        else:
            assert request.method == "PUT"
            assert body["expected_revision"] == item["revision"]
            self.updates.append(body)
            item.update({key: body[key] for key in
                         ("name", "scope", "capabilities", "secret_reference_id")})
            item["revision"] += 1
            item["updated_at"] = "2026-09-12T00:00:00Z"
            self.config = body["config"]
        await route.fulfill(json=item)


async def check(url: str, output: Path) -> None:
    """PC の三語で設定維持・資格情報更新・参照拒否・削除を確認する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ("ja", "zh", "en"):
                api = EditingApi(url, language)
                context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                await context.route("**/*", api.route)
                page = await context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))
                try:
                    await page.goto(f"{url}#/resources?project={PROJECT}")
                    labels = (await messages(page, language))["resources"]
                    panel = page.get_by_role("region", name=labels["integrationListTitle"])
                    await panel.get_by_role("button", name=labels["edit"], exact=True).click()
                    dialog = page.get_by_role("dialog")
                    await expect(dialog.get_by_label(labels["databasePort"], exact=True)).to_have_value("15432")
                    await expect(dialog.get_by_role("combobox", name=labels["databaseTls"], exact=True)).to_have_value("require")
                    password = dialog.get_by_label(labels["credentialValueLabels"]["password"], exact=True)
                    await expect(password).to_have_value("")
                    await expect(password).not_to_have_attribute("required", "")
                    await dialog.get_by_label(labels["databaseHost"], exact=True).fill("db-new.example.test")
                    await dialog.get_by_role("button", name=labels["save"], exact=True).click()
                    await expect(dialog).to_have_count(0)
                    assert api.config["host"] == "db-new.example.test" and len(api.updates) == 1
                    assert not api.secret_updates
                    assert api.integrations[0]["scope"] == {"tables": ["public.reports"]}
                    await panel.get_by_role("button", name=labels["edit"], exact=True).click()
                    await password.fill("  replacement-fixture  ")
                    await dialog.get_by_role("button", name=labels["save"], exact=True).click()
                    await expect(dialog).to_have_count(0)
                    assert len(api.secret_updates) == 1
                    assert api.secret_updates[0]["secret_value"] == "  replacement-fixture  "
                    assert len(api.updates) == 2
                    await panel.get_by_role("button", name=labels["delete"], exact=True).click()
                    await dialog.get_by_role("button", name=labels["delete"], exact=True).click()
                    await expect(dialog.get_by_role("alert")).to_contain_text(labels["resourceInUse"])
                    # 削除の失敗は確認中の dialog だけへ表示し、背面へ重複しない。
                    await expect(page.locator(".resourceAdminError")).to_have_count(0)
                    await expect(page.get_by_text(labels["resourceInUse"], exact=True)).to_have_count(1)
                    assert not api.deletes
                    await page.screenshot(path=str(output / f"resource-edit-{language}.png"))
                    await dialog.get_by_role("button", name=labels["cancel"], exact=True).click()
                    api.refuse_delete = False
                    await panel.get_by_role("button", name=labels["delete"], exact=True).click()
                    await dialog.get_by_role("button", name=labels["delete"], exact=True).click()
                    await expect(dialog).to_have_count(0)
                    await expect(panel.get_by_text("Review DB", exact=True)).to_have_count(0)
                    advanced = page.locator("details.resourceAdvanced")
                    await advanced.locator("summary").click()
                    row = advanced.locator(".resourceItem").filter(has_text="Review password")
                    await row.get_by_role("button", name=labels["edit"], exact=True).click()
                    await dialog.get_by_label(labels["nameLabel"], exact=True).fill("Renamed password")
                    await dialog.get_by_role("button", name=labels["save"], exact=True).click()
                    await expect(dialog).to_have_count(0)
                    assert "secret_value" not in api.secret_updates[1]
                    row = advanced.locator(".resourceItem").filter(has_text="Renamed password")
                    await row.get_by_role("button", name=labels["delete"], exact=True).click()
                    await dialog.get_by_role("button", name=labels["delete"], exact=True).click()
                    await expect(dialog).to_have_count(0)
                    assert api.deletes == ["integrations", "secret-references"]
                    assert not errors and not api.failures and not api.unexpected, (
                        errors, api.failures, api.unexpected)
                    print(f"PASS resource editing {language}", flush=True)
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
    if (target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost"}
            or not target.path.endswith("/tests/browser/projects.html")):
        parser.error("Only the loopback projects.html mock harness is allowed")
    asyncio.run(check(args.url, args.output))
