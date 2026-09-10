"""実 App の単一 upload 拒否と三語表示を全面 mock HTTP で検証する。"""

from __future__ import annotations

import argparse
import asyncio
import re
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from check_accounts import CSRF
from check_document_management import DocumentsApi, document
from check_projects import PROJECT, messages, privacy, settle
from playwright.async_api import (
    Browser,
    ConsoleMessage,
    Page,
    Response,
    Route,
    async_playwright,
    expect,
)

PRIVATE = "Private multipart parser detail must never be displayed"
CONTENT = b"# Fixture upload\n"


class UploadMockApi(DocumentsApi):
    """業務 HTTP は必ず mock に閉じ、Vite の source 読込だけを通過させる。"""

    async def route(self, route: Route) -> None:
        """別 origin と非 API の fetch を継続せず、誤った接続も失敗として記録する。"""
        request = route.request
        address = urlsplit(request.url)
        if f"{address.scheme}://{address.netloc}" != self.origin or (
            request.resource_type in {"fetch", "xhr", "eventsource"}
            and not address.path.startswith(self.prefix)
        ):
            self.unexpected.append(f"Non-mock request: {request.method} {request.url}")
            await route.abort()
            return
        await super().route(route)


class UploadBrowserAudit:
    """全 console error と page error を捕捉し、既知の mock HTTP 拒否のみ区別する。"""

    def __init__(self, page: Page, api: UploadMockApi) -> None:
        """HTTP 応答と console の順序に依存せず、終了時に対応を照合する。"""
        self.api = api
        self.errors: list[str] = []
        self.console_errors: list[tuple[str, str]] = []
        self.mock_refusals: set[tuple[str, int]] = set()
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on("console", self.on_console)
        page.on("response", self.on_response)

    def on_console(self, message: ConsoleMessage) -> None:
        """React warning を特定文字列だけで拾わず、error 全件を保存する。"""
        if message.type == "error":
            self.console_errors.append((message.text, message.location.get("url", "")))

    def on_response(self, response: Response) -> None:
        """route が管理する API の 4xx/5xx だけを browser の既知 resource log に対応させる。"""
        address = urlsplit(response.url)
        if (
            400 <= response.status <= 599
            and f"{address.scheme}://{address.netloc}" == self.api.origin
            and address.path.startswith(self.api.prefix)
        ):
            self.mock_refusals.add((response.url, response.status))

    def verify(self) -> None:
        """対応する mock 拒否のない console error と、すべての未処理例外を拒否する。"""
        unexpected = []
        for message, url in self.console_errors:
            refusal = re.fullmatch(
                r"Failed to load resource: the server responded with a status of "
                r"([45][0-9]{2}) \([^\n]*\)",
                message,
            )
            if refusal is None or (url, int(refusal[1])) not in self.mock_refusals:
                unexpected.append((message, url))
        assert not self.errors and not unexpected, (self.errors, unexpected)
        assert not self.api.failures and not self.api.unexpected, (
            self.api.failures,
            self.api.unexpected,
        )


def upload_request(route: Route, origin: str) -> dict:
    """原 key と実 multipart を共通検証し、合成 file の完全な内容だけを返す。"""
    request = route.request
    assert request.headers.get("origin") == origin
    assert request.headers.get("x-csrf-token") == CSRF
    key = request.headers.get("idempotency-key", "")
    assert str(UUID(key)) == key.lower() and UUID(key).int != 0
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    envelope = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii")
    body = BytesParser(policy=default).parsebytes(envelope + (request.post_data_buffer or b""))
    assert body.is_multipart()
    parts = list(body.iter_parts())
    assert [part.get_param("name", header="content-disposition") for part in parts] == [
        "file",
        "folder",
    ]
    return {
        "key": key,
        "name": parts[0].get_filename(),
        "mime": parts[0].get_content_type(),
        "body": parts[0].get_payload(decode=True),
        "folder": parts[1].get_payload(decode=True),
    }


class UploadApi(UploadMockApi):
    """既存の厳格な一覧 fixture を共有し、POST の request と拒否だけを追加する。"""

    def __init__(self, url: str, language: str, mode: str) -> None:
        """実 blob・credential と無関係な一件の upload を観測する。"""
        super().__init__(url, language, mode)
        self.uploads = 0

    async def respond(self, route: Route) -> None:
        """Browser が組み立てた multipart を検査し、本文を保存せず固定拒否を返す。"""
        request = route.request
        address = urlsplit(request.url)
        if address.path != f"{self.prefix}projects/{PROJECT}/documents" or request.method != "POST":
            await super().respond(route)
            return
        assert f"{address.scheme}://{address.netloc}" == self.origin
        content = upload_request(route, self.origin)
        assert content["name"] == "upload.md" and content["mime"] == "text/markdown"
        assert content["body"] == CONTENT and content["folder"] == b""
        self.uploads += 1
        if self.mode == "invalid-success":
            await route.fulfill(status=202, json=document(PROJECT))
            return
        status, code = {
            "oversize": (413, "document_upload_too_large"),
            "invalid": (422, "invalid_document_upload"),
            "unknown-413": (413, "unrecognized_gateway_response"),
            "expired": (401, "authentication_required"),
            "denied": (403, "csrf_rejected"),
            "project-missing": (404, "project_not_found"),
            "archived": (409, "project_archived"),
        }[self.mode]
        await route.fulfill(
            status=status,
            json={
                "status": status,
                "code": code,
                "title": "Fixture rejection",
                "detail": PRIVATE,
            },
        )


async def scenario(
    browser: Browser, url: str, mode: str, language: str, width: int, output: Path
) -> None:
    """実 file input・API client・共有文案を使い、確定拒否と未知を混同しない。"""
    api = UploadApi(url, language, mode)
    context = await browser.new_context(
        viewport={"width": width, "height": 1000},
        locale=language,
        service_workers="block",
    )
    await context.route("**/*", api.route)
    page = await context.new_page()
    audit = UploadBrowserAudit(page, api)
    name = f"{mode}-{language}-{width}"
    try:
        await page.goto(f"{url}#/documents?project={PROJECT}")
        labels = (await messages(page, language))["documentsPanel"]
        panel = page.locator(".documentPanel")
        await expect(panel.locator(".documentItem")).to_have_count(2)
        await panel.locator('input[type="file"]').first.set_input_files(
            {
                "name": "upload.md",
                "mimeType": "text/markdown",
                "buffer": CONTENT,
            }
        )
        if mode == "expired":
            await expect(page.locator('input[name="email"]')).to_be_visible()
            await expect(panel).to_have_count(0)
        else:
            key = {
                "oversize": "uploadTooLarge",
                "invalid": "invalid",
                "unknown-413": "uploadUnknown",
                "invalid-success": "uploadUnknown",
                "denied": "denied",
                "project-missing": "denied",
                "archived": "archived",
            }[mode]
            await expect(
                panel.locator(".documentUploadItems").get_by_text(
                    labels["failures"][key], exact=True
                )
            ).to_be_visible()
            await expect(panel.locator(".documentItem")).to_have_count(2)
            await expect(panel.get_by_text(labels["unknownTitle"], exact=True)).to_have_count(0)
            await panel.get_by_role("button", name=labels["refresh"], exact=True).click()
            await settle(page)
            if mode in ("denied", "project-missing", "archived"):
                await expect(panel.locator('input[type="file"]').first).to_be_disabled()
            elif mode in ("oversize", "invalid"):
                await expect(panel.locator('input[type="file"]').first).to_be_enabled()
        await settle(page)
        assert api.uploads == 1 and not api.delete_calls
        audit.verify()
        await privacy(page)
        assert PRIVATE not in await page.locator("body").inner_text()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
        print(f"PASS {name}", flush=True)
    except Exception:
        await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        print(
            "Fixture errors:",
            api.failures,
            api.unexpected,
            audit.errors,
            audit.console_errors,
            flush=True,
        )
        raise
    finally:
        await context.close()


async def check(url: str, output: Path) -> None:
    """限定した upload response 表示を検証し、永続意図や完全な未知復旧は証明しない。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ("zh", "ja", "en"):
                for mode in ("oversize", "invalid", "unknown-413"):
                    await scenario(browser, url, mode, language, 390, output)
            for mode in (
                "oversize",
                "invalid-success",
                "expired",
                "denied",
                "project-missing",
                "archived",
            ):
                await scenario(
                    browser,
                    url,
                    mode,
                    "zh",
                    1440 if mode == "oversize" else 390,
                    output,
                )
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    target = urlsplit(args.url)
    if target.scheme != "http" or target.hostname not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        parser.error("Only an explicit loopback mock Vite URL is allowed")
    if not target.path.endswith("/tests/browser/projects.html") or target.query or target.fragment:
        parser.error("Use the dedicated projects.html fixture without query or fragment")
    asyncio.run(check(args.url, args.output))
