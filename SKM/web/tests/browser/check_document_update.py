"""同名更新の入口・原条件・直列 batch を mock API と実 Component で検証する。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from check_document_management import row_menu
from check_document_upload import UploadBrowserAudit, UploadMockApi, upload_request
from check_projects import PROJECT, messages
from playwright.async_api import async_playwright, expect


class UpdateApi(UploadMockApi):
    """同名の新 ID だけを一覧へ公開し、DELETE や範囲拡張を fixture 側でも拒否する。"""

    def __init__(self, url: str, language: str) -> None:
        """現行文書と原更新要求を scenario ごとに独立して持つ。"""
        super().__init__(url, language, "update")
        self.updates: list[dict] = []

    async def respond(self, route) -> None:
        """実 multipart の更新先/hash を検証し、原パスに新 ID の metadata を返す。"""
        request = route.request
        if request.method != "POST" or urlsplit(request.url).path != f"{self.prefix}projects/{PROJECT}/documents":
            await super().respond(route)
            return
        update = upload_request(route, self.origin, replacement=True)
        old = next(row for row in self.rows[PROJECT] if row["document_id"] == update["replaces_document_id"])
        assert update["expected_checksum"] == old["checksum"]
        assert update["name"] == old["name"] and update["folder"].decode() == old["folder"]
        self.updates.append(update)
        new = {**old, "document_id": str(uuid4()), "size": len(update["body"]),
               "checksum": "sha256:" + hashlib.sha256(update["body"]).hexdigest()}
        self.rows[PROJECT] = [new if row is old else row for row in self.rows[PROJECT]]
        await route.fulfill(status=201, json=new)


async def check(url: str, output: Path) -> None:
    """三語・双テーマ・PC/狭幅で更新動作を確認し、実業務への通信を禁止する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ("ja", "zh", "en"):
                for theme, width in (("light", 1440), ("dark", 390)):
                    api = UpdateApi(url, language)
                    context = await browser.new_context(viewport={"width": width, "height": 900})
                    await context.add_init_script(f"localStorage.setItem('skillmind.theme', '{theme}')")
                    await context.route("**/*", api.route)
                    page = await context.new_page()
                    audit = UploadBrowserAudit(page, api)
                    try:
                        await page.goto(f"{url}#/documents?project={PROJECT}")
                        labels = (await messages(page, language))["documentsPanel"]
                        await expect(page.locator(".documentItem")).to_have_count(2)
                        menu = await row_menu(page, page.locator(".documentItem").first)
                        async with page.expect_file_chooser() as chooser:
                            await menu.get_by_role("menuitem", name=labels["updateFile"], exact=True).click()
                        await (await chooser.value).set_files({"name": "local-copy.md", "mimeType": "text/markdown", "buffer": b"updated"})
                        await expect(page.get_by_label(labels["uploadFilesAria"], exact=True)).to_be_enabled()
                        assert len(api.updates) == 1 and api.updates[0]["name"] == "overview.md"
                        await page.get_by_role("checkbox", name=labels["replaceSameName"], exact=True).check()
                        await page.get_by_role("combobox", name=labels["targetFolder"], exact=True).fill("specs")
                        await page.get_by_label(labels["uploadFilesAria"], exact=True).set_input_files([
                            {"name": "overview.md", "mimeType": "text/markdown", "buffer": b"new first"},
                            {"name": "second.md", "mimeType": "text/markdown", "buffer": b"new second"},
                        ])
                        await expect(page.get_by_label(labels["uploadFilesAria"], exact=True)).to_be_enabled()
                        assert len(api.updates) == 3 and len({item["key"] for item in api.updates}) == 3
                        assert len(api.rows[PROJECT]) == 2 and not api.delete_calls
                        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                        audit.verify()
                        await page.screenshot(path=str(output / f"update-{language}-{theme}-{width}.png"))
                    finally:
                        await context.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    target = urlsplit(args.url)
    if target.scheme != "http" or target.hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("Only a loopback mock Vite URL is allowed")
    if not target.path.endswith("/tests/browser/projects.html") or target.query or target.fragment:
        parser.error("Use the dedicated projects.html fixture")
    asyncio.run(check(args.url, args.output))
