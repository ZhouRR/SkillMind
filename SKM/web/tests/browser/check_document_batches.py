"""文書の複数選択、直列削除と指定 directory upload を mock API で検証する。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from check_document_management import DOCUMENT, SECOND, document
from check_document_upload import UploadMockApi, upload_request
from check_projects import NEXT_PROJECT, PROJECT, messages, settle
from playwright.async_api import Browser, Route, async_playwright, expect

THIRD = "00000000-0000-4000-8000-000000000093"


class BatchApi(UploadMockApi):
    """原 ID と upload destination を記録し、二件目の失敗を注入する。"""

    def __init__(self, url: str, language: str, mode: str) -> None:
        """全 case を独立した三文書から開始する。"""
        super().__init__(url, language, mode)
        self.rows[PROJECT].append({**document(PROJECT, THIRD), "name": "third.md"})
        self.uploads: list[dict] = []

    async def respond(self, route: Route) -> None:
        """DELETE は一件ずつ応答し、POST は multipart の実 byte を確認する。"""
        request = route.request
        path = urlsplit(request.url).path
        prefix = f"{self.prefix}projects/{PROJECT}/documents"
        if request.method == "POST" and path == prefix:
            upload = upload_request(route, self.origin)
            self.uploads.append(upload)
            row = {
                **document(PROJECT, THIRD),
                "name": upload["name"],
                "folder": upload["folder"].decode(),
                "size": len(upload["body"]),
            }
            await route.fulfill(status=201, json=row)
        elif request.method == "DELETE" and path.startswith(prefix + "/"):
            identity = path.rsplit("/", 1)[-1]
            self.delete_calls.append(identity)
            if self.mode == "switch":
                self.gate.received.set()
                await asyncio.wait_for(self.gate.release.wait(), 30)
            if identity == SECOND and self.mode in {"refused", "unknown"}:
                await self.problem(
                    route,
                    409 if self.mode == "refused" else 500,
                    "document_in_use" if self.mode == "refused" else "server_error",
                )
            else:
                self.rows[PROJECT] = [
                    row for row in self.rows[PROJECT] if row["document_id"] != identity
                ]
                await route.fulfill(status=204)
            self.gate.returned.set()
        else:
            await super().respond(route)


async def scenario(
    browser: Browser, url: str, output: Path, language: str, width: int, theme: str, mode: str
) -> None:
    """実 Component で選択、確認、防重、途中停止と旧 owner の隔離を確認する。"""
    api = BatchApi(url, language, mode)
    context = await browser.new_context(viewport={"width": width, "height": 900})
    await context.add_init_script(f"localStorage.setItem('skillmind.theme', '{theme}')")
    if mode == "switch":
        await context.add_init_script("""const send = window.fetch.bind(window);
        window.fetch = (input, init) => send(input, init ? {...init, signal: undefined} : init);""")
    await context.route("**/*", api.route)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    name = f"{mode}-{language}-{width}-{theme}"
    try:
        await page.goto(f"{url}#/documents?project={PROJECT}")
        labels = (await messages(page, language))["documentsPanel"]
        await expect(page.locator(".documentItem")).to_have_count(3)
        if mode == "upload":
            await page.get_by_role("button", name=labels["uploadHere"], exact=True).click()
            destination = page.get_by_role("combobox", name=labels["targetFolder"], exact=True)
            await expect(destination).to_have_value("specs")
            await destination.fill("specs/new/nested")
            await page.get_by_label(labels["uploadFilesAria"], exact=True).set_input_files(
                [
                    {"name": "first.md", "mimeType": "text/markdown", "buffer": b"first"},
                    {"name": "second.md", "mimeType": "text/markdown", "buffer": b"second"},
                ]
            )
            await expect(destination).to_be_enabled()
            assert [item["folder"] for item in api.uploads] == [b"specs/new/nested"] * 2
            assert [item["body"] for item in api.uploads] == [b"first", b"second"]
            assert len({item["key"] for item in api.uploads}) == 2
        else:
            await page.get_by_role("checkbox", name=labels["selectAll"], exact=True).check()
            await expect(page.locator(".documentItem input:checked")).to_have_count(3)
            await page.get_by_role("button", name=labels["clearSelection"], exact=True).click()
            await expect(page.locator(".documentItem input:checked")).to_have_count(0)
            await page.get_by_role("checkbox", name=labels["selectAll"], exact=True).check()
            action = page.get_by_role("button", name=labels["deleteSelected"], exact=True)
            await action.evaluate("button => { button.click(); button.click(); }")
            await expect(page.get_by_label(labels["uploadFilesAria"], exact=True)).to_be_disabled()
            confirm = page.get_by_role("dialog").get_by_role(
                "button", name=labels["remove"], exact=True
            )
            await confirm.focus()
            await page.keyboard.press("Enter")
            if mode == "switch":
                await asyncio.wait_for(api.gate.received.wait(), 5)
                await page.evaluate(
                    "id => location.hash = '/documents?project=' + id", NEXT_PROJECT
                )
                await expect(page.locator(".documentItem")).to_have_count(2)
                api.gate.release.set()
                await asyncio.wait_for(api.gate.returned.wait(), 5)
                await settle(page)
                assert api.delete_calls == [DOCUMENT]
                await expect(page.locator(".documentItem input:checked")).to_have_count(0)
            elif mode == "success":
                await expect(page.locator(".documentItem")).to_have_count(0)
                assert api.delete_calls == [DOCUMENT, SECOND, THIRD]
            else:
                await expect(page.get_by_text(labels["batchStopped"], exact=False)).to_be_visible()
                await expect(page.locator(".documentItem input:checked")).to_have_count(2)
                for filename in ("second.md", "third.md"):
                    await expect(
                        page.locator(".documentItem").get_by_text(filename, exact=True)
                    ).to_be_visible()
                for checkbox in await page.locator('.documentItem input[type="checkbox"]').all():
                    bounds = await checkbox.bounding_box()
                    assert bounds and bounds["width"] == 16 and bounds["height"] == 16
                assert api.delete_calls == [DOCUMENT, SECOND]
                if mode == "unknown":
                    await expect(action).to_be_disabled()
                    await expect(
                        page.get_by_role("heading", name=labels["unknownTitle"], exact=True)
                    ).to_be_visible()
                else:
                    await expect(action).to_be_enabled()
        assert not errors and not api.unexpected and not api.failures, (
            errors,
            api.unexpected,
            api.failures,
        )
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
        print(f"PASS {name}", flush=True)
    except Exception:
        await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        print(errors, api.unexpected, api.failures, flush=True)
        raise
    finally:
        api.gate.release.set()
        await context.close()


async def check(url: str, output: Path) -> None:
    """三語と PC・狭幅の両 theme、成功と失敗・切替を分けて実行する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ("zh", "ja", "en"):
                for width in (390, 1366, 1440, 1920):
                    for theme in ("light", "dark"):
                        await scenario(browser, url, output, language, width, theme, "refused")
            for mode in ("success", "unknown", "switch", "upload"):
                await scenario(browser, url, output, "zh", 1440, "dark", mode)
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
        parser.error("Only a loopback projects.html mock harness is allowed")
    asyncio.run(check(args.url, args.output))
