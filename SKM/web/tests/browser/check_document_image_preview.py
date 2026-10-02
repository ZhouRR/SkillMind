"""実 App の raster preview・解放・拒否を全面 mock HTTP で検証し、外部通信を遮断する。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import struct
import zlib
from pathlib import Path
from urllib.parse import urlsplit

from check_document_management import DOCUMENT, SECOND, assert_document_writes, document, open_ancestor_folders
from check_document_preview import PRIVATE, PreviewApi
from check_projects import NEXT_PROJECT, PROJECT, messages, privacy, settle
from playwright.async_api import Browser, Page, Route, async_playwright, expect


def png_fixture(width: int = 960, height: int = 640) -> bytes:
    """狭幅より大きい合成 PNG を標準 library のみで作り、実 decoder と縮小を検証する。"""
    def chunk(kind: bytes, body: bytes) -> bytes:
        """PNG chunk の長さと checksum を同じ合成 body から計算する。"""
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    pixels = (b"\x00" + b"\x10\x70\xbe" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b"")
    )


# JPEG/GIF は 24x18 の単色合成画像。外部 file・画像生成 library を実行時依存にしない。
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwg"
    "JC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAASABgDASIAAhEB"
    "AxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQR"
    "BRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpT"
    "VFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLD"
    "xMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAEC"
    "AwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAV"
    "YnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaH"
    "iImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3"
    "+Pn6/9oADAMBAAIRAxEAPwDm6KKK+xPjwooooAKKKKACiiigD//Z"
)
GIF = base64.b64decode(
    "R0lGODdhGAASAIEAABBwvgAAAAAAAAAAACwAAAAAGAASAEAIIwABCBxIsKDBgwgTKlzIsKHDhxAjSpxIsaLFixgzatzIUWNAADs="
)
PNG = png_fixture()
IMAGES = {
    "png": (PNG, "image/png", [960, 640]),
    "png-wide": (png_fixture(2400, 320), "image/png", [2400, 320]),
    "png-tall": (png_fixture(320, 2400), "image/png", [320, 2400]),
    "jpg": (JPEG, "image/jpeg", [24, 18]),
    "jpeg": (JPEG, "image/jpeg", [24, 18]),
    "gif": (GIF, "image/gif", [24, 18]),
}
ATTACHMENT_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
LATE_MODES = (
    "close-late", "file-late", "project-late",
    "close-late-401", "file-late-403", "project-late-401",
)
PROBE_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://preview.invalid/image"/></svg>'


class ImagePreviewApi(PreviewApi):
    """既存の認証・文書 fixture を共有し、画像 byte と遅延 response のみを所有する。"""

    def __init__(self, url: str, language: str, mode: str) -> None:
        """case ごとの原 filename と実画像形式を固定する。"""
        super().__init__(url, language, mode)
        self.image_kind = mode if mode in IMAGES else "png"
        self.extension = self.image_kind.split("-")[0]
        self.filename = "preview.svg" if mode == "unsupported" else f"preview.{self.extension.upper()}"

    async def respond(self, route: Route) -> None:
        """文書 GET は合成 byte で返し、実 API や未知 URL は親の拒否境界に渡す。"""
        request = route.request
        address = urlsplit(request.url)
        parts = address.path.removeprefix(self.prefix).split("/")
        if (
            f"{address.scheme}://{address.netloc}" != self.origin
            or not address.path.startswith(self.prefix)
            or len(parts) < 3 or parts[0] != "projects" or parts[2] != "documents"
        ):
            await super().respond(route)
            return
        assert request.method == "GET" and parts[1] in (PROJECT, NEXT_PROJECT)
        body, mime, _ = IMAGES[self.image_kind]
        if len(parts) == 3:
            await route.fulfill(json={"documents": [
                {**document(parts[1]), "name": self.filename,
                 "mime": "text/html" if self.mode == "metadata-mime" else mime,
                 "size": 1_000_001 if self.mode == "metadata-large" else len(body)},
                {**document(parts[1], SECOND), "name": "second.gif", "mime": "image/gif", "size": len(GIF)},
            ]})
            return
        assert len(parts) == 5 and parts[4] == "content" and parts[3] in (DOCUMENT, SECOND)
        self.content_calls.append((parts[1], parts[3]))
        filename = self.filename
        if parts[3] == SECOND:
            body, mime, _ = IMAGES["gif"]
            filename = "second.gif"
        else:
            if self.mode in (*LATE_MODES, "loading"):
                self.gate.received.set()
                await asyncio.wait_for(self.gate.release.wait(), 45)
            failures = {
                "expired": (401, "authentication_required"),
                "denied": (403, "csrf_rejected"),
                "project-missing": (404, "project_not_found"),
                "content-missing": (409, "document_content_missing"),
                "storage-unavailable": (503, "document_storage_unavailable"),
                "close-late-401": (401, "authentication_required"),
                "file-late-403": (403, "csrf_rejected"),
                "project-late-401": (401, "authentication_required"),
            }
            if self.mode in failures:
                status, code = failures[self.mode]
                await route.fulfill(status=status, json={
                    "status": status, "title": "Fixture refusal", "code": code, "detail": PRIVATE,
                })
                self.gate.returned.set()
                return
            if self.mode == "fetch-failed":
                await route.abort("failed")
                return
            if self.mode == "decode-invalid":
                # 正しい PNG signature の後は不正 byte。API の形式拒否と img の decode 拒否を区別する。
                body = PNG[:8] + PROBE_SVG
            elif self.mode in ("mime-invalid", "signature-invalid"):
                body = PROBE_SVG
                if self.mode == "mime-invalid":
                    mime = "image/svg+xml"
        headers = {**ATTACHMENT_HEADERS, "Content-Disposition": f'attachment; filename="{filename}"'}
        await route.fulfill(content_type=mime, body=body, headers=headers)
        self.gate.returned.set()


async def install_image_transport(page: Page, mode: str) -> None:
    """実 fetch の資格/上限と URL の寿命を観測し、遅延 case だけ abort 無視を模擬する。"""
    await page.add_init_script("""(options => {
      window.imagePreviewAudit = {created: [], revoked: [], fetches: [], pulls: 0, cancelled: 0};
      const audit = window.imagePreviewAudit;
      const create = URL.createObjectURL.bind(URL);
      const revoke = URL.revokeObjectURL.bind(URL);
      URL.createObjectURL = blob => {
        const url = create(blob);
        audit.created.push({url, type: blob.type, size: blob.size});
        return url;
      };
      URL.revokeObjectURL = url => { audit.revoked.push(url); revoke(url); };
      const send = window.fetch.bind(window);
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
        if (!/\\/documents\\/[^/]+\\/content$/.test(url)) return send(input, init);
        audit.fetches.push({url, credentials: init?.credentials, cache: init?.cache, redirect: init?.redirect});
        if (options.mode.startsWith('stream-') || options.mode === 'headers-large') {
          const stream = new ReadableStream({
            pull(controller) {
              audit.pulls += 1;
              controller.enqueue(new Uint8Array(400000).fill(65));
              if (audit.pulls === 20) controller.close();
            },
            cancel() { audit.cancelled += 1; }
          }, {highWaterMark: 0});
          const headers = {...options.headers, 'Content-Type': 'image/png',
            'Content-Disposition': 'attachment; filename="preview.PNG"'};
          if (options.mode === 'stream-lying') headers['Content-Length'] = '10';
          if (options.mode === 'headers-large') headers['Content-Length'] = '1000001';
          return Promise.resolve(new Response(stream, {headers}));
        }
        return send(input, options.late ? {...init, signal: undefined} : init);
      };
    })(""" + json.dumps({"mode": mode, "late": mode in LATE_MODES, "headers": ATTACHMENT_HEADERS}) + ");")


async def ready_image(page: Page, filename: str, extension: str) -> str:
    """blob URL と実 decode・狭幅 layout を照合し、本文や API URL の直接埋込みを拒否する。"""
    image = page.locator("img.previewImage")
    await expect(image).to_be_visible()
    await expect(image).to_have_attribute("alt", filename)
    await expect(image).to_have_attribute("referrerpolicy", "no-referrer")
    await expect(page.locator(".imagePreview")).to_have_attribute("aria-busy", "false")
    await expect(page.locator(".previewText, iframe.previewFrame")).to_have_count(0)
    assert await image.evaluate("el => [el.naturalWidth, el.naturalHeight]") == IMAGES[extension][2]
    source = await image.get_attribute("src")
    assert source and source.startswith("blob:")
    assert await image.evaluate("""el => {
      const image = el.getBoundingClientRect(), body = el.closest('.modalBody').getBoundingClientRect();
      return image.width > 0 && image.height > 0 && image.left >= body.left
        && image.right <= body.right + 1 && image.top >= body.top && image.bottom <= body.bottom + 1;
    }""")
    audit = await page.evaluate("window.imagePreviewAudit")
    assert any(item["url"] == source and item["type"] == IMAGES[extension][1] for item in audit["created"])
    assert source not in audit["revoked"]
    return source


async def assert_urls_released(page: Page) -> None:
    """StrictMode による再 mount も含め、作成した全 URL が閉鎖後に失効することを確認する。"""
    await expect(page.get_by_role("dialog")).to_have_count(0)
    await page.wait_for_function("""() => window.imagePreviewAudit.created.every(
      item => window.imagePreviewAudit.revoked.includes(item.url))""")


async def scenario(
    browser: Browser, url: str, mode: str, language: str, width: int, output: Path, theme: str = "light",
) -> None:
    """本番 App を通じて成功・失敗・対象切替を独立 context で検証する。"""
    api = ImagePreviewApi(url, language, mode)
    context = await browser.new_context(viewport={"width": width, "height": 1000}, locale=language)
    await context.route("**/*", api.route)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(error.stack))
    await page.add_init_script(f"localStorage.setItem('skillmind.theme', {json.dumps(theme)})")
    await install_image_transport(page, mode)
    name = f"image-{mode}-{language}-{width}-{theme}"
    try:
        await page.goto(f"{url}#/documents?project={PROJECT}")
        catalog = await messages(page, language)
        labels = catalog["documentsPanel"]
        panel = page.locator(".documentPanel")
        await expect(page.locator(".documentItem")).to_have_count(2)
        await open_ancestor_folders(page.locator(".documentItem").first)
        previews = panel.locator("button.documentName")
        if mode in ("metadata-large", "unsupported"):
            link = panel.get_by_role("link", name=f'{labels["download"]}: {api.filename}', exact=True)
            await expect(link).to_have_attribute("download", api.filename)
            await expect(link).to_have_attribute("href", f"{api.prefix}projects/{PROJECT}/documents/{DOCUMENT}/content")
            await expect(panel.get_by_role("button", name=f'{labels["previewButton"]}: {api.filename}', exact=True)).to_have_count(0)
            if mode == "metadata-large":
                await expect(link).to_have_attribute("title", labels["oversizedTitle"])
            assert not api.content_calls
        else:
            await previews.first.focus()
            await page.keyboard.press("Enter")
            dialog = page.get_by_role("dialog")
            if mode != "expired":
                await expect(dialog).to_be_visible()
                download = dialog.get_by_role("link", name=labels["download"], exact=True)
                await expect(download).to_have_attribute("download", api.filename)
                await expect(download).to_have_attribute("href", f"{api.prefix}projects/{PROJECT}/documents/{DOCUMENT}/content")
                await expect(dialog.get_by_role("button", name=catalog["elements"]["close"], exact=True)).to_be_enabled()
            if mode in (*LATE_MODES, "loading"):
                await asyncio.wait_for(api.gate.received.wait(), 5)
                await expect(dialog.get_by_role("status", name=labels["loadingPreview"], exact=True)).to_be_visible()
                await expect(page.locator("img.previewImage")).to_have_count(0)
                if mode.startswith("close-"):
                    await page.keyboard.press("Escape")
                    await assert_urls_released(page)
                elif mode.startswith("file-"):
                    # Modal が背面 pointer を遮るため、実 row の handler を直接起動して owner 切替を試す。
                    await previews.nth(1).evaluate("element => element.click()")
                    await ready_image(page, "second.gif", "gif")
                elif mode.startswith("project-"):
                    await page.evaluate("target => location.hash = target", f"/documents?project={NEXT_PROJECT}")
                    await expect(dialog).to_have_count(0)
                    await expect(page.locator(".documentItem")).to_have_count(2)
                    await open_ancestor_folders(page.locator(".documentItem").nth(1))
                    await previews.nth(1).click()
                    await ready_image(page, "second.gif", "gif")
                api.gate.release.set()
                await asyncio.wait_for(api.gate.returned.wait(), 5)
                await settle(page)
                await expect(page.locator('input[name="email"]')).to_have_count(0)
                if mode == "loading":
                    await ready_image(page, api.filename, "png")
                elif mode.startswith("close-"):
                    await expect(dialog).to_have_count(0)
                    assert not (await page.evaluate("window.imagePreviewAudit"))["created"]
                    await previews.nth(1).click()
                    await ready_image(page, "second.gif", "gif")
                else:
                    await ready_image(page, "second.gif", "gif")
                    assert all(item["type"] == "image/gif" for item in (await page.evaluate("window.imagePreviewAudit"))["created"])
            elif mode in (*IMAGES, "metadata-mime", "file-ready", "project-ready"):
                original = await ready_image(page, api.filename, api.image_kind)
                if mode == "png" and language == "en" and width == 390 and theme == "light":
                    async with page.expect_download() as saved:
                        await dialog.get_by_role("link", name=labels["download"], exact=True).click()
                    download = await saved.value
                    assert download.suggested_filename == api.filename
                    assert await download.failure() is None
                    download_path = await download.path()
                    assert download_path and Path(download_path).read_bytes() == PNG
                if mode in ("file-ready", "project-ready"):
                    if mode == "file-ready":
                        await previews.nth(1).evaluate("element => element.click()")
                    else:
                        await page.evaluate("target => location.hash = target", f"/documents?project={NEXT_PROJECT}")
                        await assert_urls_released(page)
                        await expect(page.locator(".documentItem")).to_have_count(2)
                        await open_ancestor_folders(page.locator(".documentItem").nth(1))
                        await previews.nth(1).click()
                    current = await ready_image(page, "second.gif", "gif")
                    assert current != original
                    assert original in (await page.evaluate("window.imagePreviewAudit"))["revoked"]
            elif mode == "expired":
                await expect(page.locator('input[name="email"]')).to_be_visible()
                await expect(panel).to_have_count(0)
            else:
                key = {
                    "decode-invalid": "loadFailed", "fetch-failed": "loadFailed", "mime-invalid": "loadFailed",
                    "signature-invalid": "contentInvalid", "denied": "denied", "project-missing": "denied",
                    "content-missing": "contentMissing", "storage-unavailable": "storageUnavailable",
                    "stream-absent": "previewTooLarge", "stream-lying": "previewTooLarge", "headers-large": "previewTooLarge",
                }[mode]
                await expect(dialog.get_by_role("alert")).to_have_text(labels["failures"][key])
                await expect(page.locator("img.previewImage, .previewText, iframe.previewFrame")).to_have_count(0)
                created = (await page.evaluate("window.imagePreviewAudit"))["created"]
                assert bool(created) == (mode == "decode-invalid")
            await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
            if mode != "expired":
                await dialog.get_by_role("button", name=catalog["elements"]["close"], exact=True).click()
                await assert_urls_released(page)
                if mode in LATE_MODES:
                    await expect(panel.locator('input[type="file"]').first).to_be_enabled()
                if mode in ("denied", "project-missing"):
                    await panel.get_by_role("button", name=labels["refresh"], exact=True).click()
                    await settle(page)
                    await assert_document_writes(page, disabled=True)
            else:
                await assert_urls_released(page)
        await settle(page)
        await privacy(page)
        audit = await page.evaluate("window.imagePreviewAudit")
        assert all(call["credentials"] == "same-origin" and call["cache"] == "no-store"
                   and call["redirect"] == "error" for call in audit["fetches"])
        if mode in ("metadata-large", "unsupported"):
            assert not audit["fetches"] and not audit["created"]
        elif mode.startswith("stream-") or mode == "headers-large":
            assert not api.content_calls and len(audit["fetches"]) == 1
            assert audit["pulls"] == (0 if mode == "headers-large" else 3) and audit["cancelled"] == 1
        else:
            switched = mode in (*LATE_MODES, "file-ready", "project-ready")
            assert len(audit["fetches"]) == (2 if switched else 1)
            expected_calls = [(PROJECT, DOCUMENT)]
            if switched:
                expected_calls.append((NEXT_PROJECT if mode.startswith("project-") else PROJECT, SECOND))
            elif mode == "png" and language == "en" and width == 390 and theme == "light":
                expected_calls.append((PROJECT, DOCUMENT))
            assert api.content_calls == expected_calls
        assert PRIVATE not in await page.locator("body").inner_text()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert len(context.pages) == 1 and not api.probes
        assert not errors and not api.unexpected and not api.failures, (errors, api.unexpected, api.failures)
        assert not [call for call in api.calls if call[1].endswith("auth/logout")]
        print(f"PASS {name}", flush=True)
    except Exception:
        await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        print("Fixture errors:", errors, api.probes, api.unexpected, api.failures, flush=True)
        raise
    finally:
        api.gate.release.set()
        api.release.set()
        await context.close()


async def check(url: str, output: Path) -> None:
    """双テーマ・三語・狭幅と各安全境界を loopback fixture のみで検証する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ("zh", "ja", "en"):
                for theme in ("light", "dark"):
                    for width in (390, 1440):
                        await scenario(browser, url, "png", language, width, output, theme)
                for mode in ("decode-invalid", "fetch-failed", "denied"):
                    await scenario(browser, url, mode, language, 390, output)
            for mode in ("png-wide", "png-tall"):
                for width in (390, 1440):
                    await scenario(browser, url, mode, "zh", width, output)
            for mode in (
                "jpg", "jpeg", "gif", "loading", "metadata-mime", "metadata-large", "unsupported",
                "mime-invalid", "signature-invalid", "expired", "project-missing", "content-missing",
                "storage-unavailable", "stream-absent", "stream-lying", "headers-large",
                "file-ready", "project-ready", *LATE_MODES,
            ):
                await scenario(browser, url, mode, "zh", 390, output)
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    target = urlsplit(arguments.url)
    if target.scheme != "http" or target.hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("Only an explicit loopback mock Vite URL is allowed")
    if not target.path.endswith("/tests/browser/projects.html") or target.query or target.fragment:
        parser.error("Use the dedicated projects.html fixture without query or fragment")
    asyncio.run(check(arguments.url, arguments.output))
