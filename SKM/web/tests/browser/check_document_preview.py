"""実 App の文書 preview を全面 mock HTTP で検証し、外部通信を必ず遮断する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from check_accounts import OTHER, PASSWORD, ResponseGate, self_revoke
from check_document_management import DOCUMENT, SECOND, document
from check_projects import NEXT_PROJECT, PROJECT, ProjectsApi, messages, privacy, settle
from playwright.async_api import Browser, Page, Route, async_playwright, expect

PRIVATE = "Preview fixture internal detail must not be shown"
SECOND_TEXT = "Second document is the current preview."
MARKDOWN = "# Original Markdown\n\n**Unrendered emphasis** <img src='/preview-probe/markdown'>"
DELAYED_TIMERS = ("timeout-delayed-timer-401", "timeout-delayed-timer-403")
LATE_RESPONSES = ("close-late", "project-late", "actor-late", "timeout", *DELAYED_TIMERS)
MALICIOUS = """<!doctype html><html><head>
<base href="https://preview.invalid/base/" target="_top">
<meta http-equiv="refresh" content="0;url=https://preview.invalid/refresh">
<link rel="stylesheet" href="https://preview.invalid/style.css">
<link rel="preload" as="image" href="/preview-probe/preload">
<style>@import 'https://preview.invalid/import.css';
body { background: url('/preview-probe/css'); }</style>
</head><body onload="parent.document.body.dataset.previewMutated='yes'">
<style>.preview-grid{display:grid;grid-template-columns:1fr 2fr;gap:12px}
.preview-card{background:rgb(224, 236, 248);padding:17px;border-radius:9px}</style>
<h1>Readable preview</h1><p>Safe paragraph <strong>strong text</strong>.</p>
<div class="preview-grid"><div id="styled-card" class="preview-card">Styled card</div>
<div>Layout</div></div>
<svg id="static-diagram" viewBox="0 0 240 80" width="240" height="80">
<defs><linearGradient id="paint"><stop stop-color="blue"/>
<stop offset="1" stop-color="white"/></linearGradient>
<symbol id="shape" viewBox="0 0 20 20"><circle cx="10" cy="10" r="8" fill="blue"/></symbol></defs>
<rect width="240" height="80" fill="url(#paint)"/><use href="#shape" width="20" height="20"/>
<text x="30" y="40">Static diagram</text>
<foreignObject><p>Forbidden SVG HTML</p></foreignObject>
<animate attributeName="href" values="https://preview.invalid/animated"/>
<use href="https://preview.invalid/external.svg#shape"/>
<script>parent.postMessage('preview-attacked','*')</script></svg>
<ul><li>First item</li><li>Second item</li></ul>
<table><thead><tr><th>Field</th><th>Value</th></tr></thead>
<tbody><tr><td>State</td><td>Safe</td></tr></tbody></table>
<pre>literal &lt;script&gt; text</pre>
<a href="https://preview.invalid/navigate" target="_top">Readable link text</a>
<p style="background-image:url('/preview-probe/inline')">No uploaded style</p>
<script>parent.document.body.dataset.previewMutated='yes';
parent.postMessage('preview-attacked','*');top.location='https://preview.invalid/top';</script>
<img src="https://preview.invalid/image" srcset="/preview-probe/srcset 2x"
  onerror="parent.postMessage('preview-attacked','*')">
<iframe src="https://preview.invalid/frame" srcdoc="<script>alert(1)</script>"></iframe>
<object data="https://preview.invalid/object"></object>
<embed src="https://preview.invalid/embed">
<video poster="https://preview.invalid/poster"><source src="/preview-probe/video"></video>
<audio src="https://preview.invalid/audio"></audio>
<form action="https://preview.invalid/form" target="_top"><input autofocus>
<button formaction="https://preview.invalid/button">Active form</button></form>
<svg><image href="https://preview.invalid/svg"/></svg>
<math><mtext><img src="/preview-probe/math"></mtext></math>
<template><img src="/preview-probe/template"></template>
<unknown-active-element onclick="alert(1)">Unknown active content</unknown-active-element>
</body></html>"""


class PreviewApi(ProjectsApi):
    """既存 App fixture を共有し、文書本文と遅延応答だけを独立に所有する。"""

    def __init__(self, url: str, language: str, mode: str) -> None:
        """本番資源を含まない metadata、本文、原要求 gate を case ごとに作る。"""
        super().__init__(url, language)
        self.mode = mode
        self.content_calls: list[tuple[str, str]] = []
        self.probes: list[str] = []
        self.gate = ResponseGate()

    async def respond(self, route: Route) -> None:
        """悪性 URL は記録して遮断し、GET 以外の文書操作は一切許可しない。"""
        request = route.request
        address = urlsplit(request.url)
        if (
            f"{address.scheme}://{address.netloc}" != self.origin
            or "/preview-probe/" in address.path
        ):
            self.probes.append(request.url)
            await route.abort()
            return
        parts = address.path.removeprefix(self.prefix).split("/")
        if len(parts) < 3 or parts[0] != "projects" or parts[2] != "documents":
            await super().respond(route)
            return
        assert request.method == "GET" and parts[1] in (PROJECT, NEXT_PROJECT)
        if len(parts) == 3:
            size = 1_000_001 if self.mode == "metadata-large" else 10
            filename = "preview.md" if self.mode == "markdown" else "preview.html"
            mime = "text/markdown" if self.mode == "markdown" else "text/html"
            await route.fulfill(
                json={
                    "documents": [
                        {
                            **document(parts[1]),
                            "name": filename,
                            "mime": mime,
                            "size": size,
                        },
                        {**document(parts[1], SECOND), "name": "second.txt", "mime": "text/plain"},
                    ]
                }
            )
            return
        assert len(parts) == 5 and parts[4] == "content"
        self.content_calls.append((parts[1], parts[3]))
        assert parts[3] in (DOCUMENT, SECOND)
        if parts[3] == SECOND:
            await route.fulfill(content_type="text/plain", body=SECOND_TEXT)
            return
        if self.mode in LATE_RESPONSES:
            self.gate.received.set()
            await asyncio.wait_for(self.gate.release.wait(), 45)
        failures = {
            "denied": (403, "csrf_rejected"),
            "project-missing": (404, "project_not_found"),
            "expired": (401, "authentication_required"),
            "actor-late": (401, "authentication_required"),
            "project-late": (401, "authentication_required"),
            "close-late": (401, "authentication_required"),
            "timeout-delayed-timer-401": (401, "authentication_required"),
            "timeout-delayed-timer-403": (403, "csrf_rejected"),
            "content-missing": (409, "document_content_missing"),
            "content-invalid": (409, "document_content_invalid"),
            "storage-unavailable": (503, "document_storage_unavailable"),
        }
        if self.mode in failures:
            status, code = failures[self.mode]
            await route.fulfill(
                status=status,
                json={
                    "status": status,
                    "title": "Fixture refusal",
                    "code": code,
                    "detail": PRIVATE,
                },
            )
        elif self.mode == "invalid-success":
            await route.fulfill(status=202, content_type="text/html", body=MALICIOUS)
        elif self.mode == "markdown":
            await route.fulfill(content_type="text/markdown", body=MARKDOWN)
        else:
            await route.fulfill(content_type="text/html", body=MALICIOUS)
        self.gate.returned.set()


async def install_transport(page: Page, mode: str) -> None:
    """late case は abort 無視、byte case は実 ReadableStream で client の境界を観測する。"""
    await page.add_init_script("""(() => {
      window.previewMessages = [];
      addEventListener('message', event => window.previewMessages.push(event.data));
    })();""")
    if mode in LATE_RESPONSES:
        await page.add_init_script("""(() => {
          const send = window.fetch.bind(window);
          window.fetch = (input, init) => send(input, init ? {...init, signal: undefined} : init);
        })();""")
    if mode in DELAYED_TIMERS:
        await page.add_init_script("""(() => {
          window.previewDeadlineCallbacks = 0;
          const schedule = window.setTimeout.bind(window);
          window.setTimeout = (callback, delay, ...args) => schedule(
            typeof callback === 'function' ? () => {
              if (delay === 30000) window.previewDeadlineCallbacks += 1;
              callback(...args);
            } : callback, delay);
        })();""")
    if mode.startswith(("stream-", "headers-")):
        await page.add_init_script(
            """(mode => {
          const send = window.fetch.bind(window);
          window.previewStream = {requests: 0, pulls: 0, cancelled: 0};
          window.fetch = (input, init) => {
            if (!String(input).endsWith('/documents/"""
            + DOCUMENT
            + """/content')) {
              return send(input, init);
            }
            window.previewStream.requests += 1;
            const stream = new ReadableStream({
              pull(controller) {
                window.previewStream.pulls += 1;
                if (mode.startsWith('headers-')) return;
                controller.enqueue(new Uint8Array(400000).fill(65));
                if (window.previewStream.pulls === 20) controller.close();
              },
              cancel() { window.previewStream.cancelled += 1; }
            }, {highWaterMark: 0});
            const headers = {'Content-Type': 'text/plain'};
            if (mode === 'stream-lying') headers['Content-Length'] = '10';
            const status = mode === 'headers-expired' ? 401 : mode === 'headers-denied' ? 403 : 200;
            return Promise.resolve(new Response(stream, {status, headers}));
          };
        })("""
            + json.dumps(mode)
            + ");"
        )


async def static_html(page: Page) -> None:
    """静的本文の読める構造と frame 内外の権限・URL 除去を実 DOM で照合する。"""
    frame_element = page.locator("iframe.previewFrame")
    await expect(frame_element).to_be_visible()
    await expect(frame_element).to_have_attribute("sandbox", "")
    await expect(frame_element).to_have_attribute("referrerpolicy", "no-referrer")
    frame = page.frame_locator("iframe.previewFrame")
    await expect(frame.get_by_role("heading", name="Readable preview")).to_be_visible()
    await expect(frame.locator("strong")).to_have_text("strong text")
    await expect(frame.get_by_role("cell", name="Safe", exact=True)).to_be_visible()
    await expect(frame.locator("pre")).to_have_text("literal <script> text")
    await expect(
        frame.locator(
            "script, link, img, iframe, object, embed, video, audio, "
            "svg image, svg script, foreignObject, animate, math, form, input, button, "
            "base, template, "
            "unknown-active-element, meta[http-equiv=refresh]"
        )
    ).to_have_count(0)
    csp = frame.locator('meta[http-equiv="Content-Security-Policy"]')
    await expect(csp).to_have_count(1)
    policy = await csp.get_attribute("content")
    assert policy and "default-src 'none'" in policy and "form-action 'none'" in policy
    assert await csp.evaluate("""element => {
      for (let before = element.previousElementSibling; before;
           before = before.previousElementSibling) {
        if (before.tagName !== 'META' || !before.hasAttribute('charset')) return false;
      }
      return element.parentElement.tagName === 'HEAD';
    }""")
    await expect(frame.locator("#static-diagram text")).to_have_text("Static diagram")
    await expect(frame.locator('#static-diagram use[href="#shape"]')).to_have_count(1)
    assert await frame.locator(".preview-grid").evaluate(
        "el => getComputedStyle(el).display"
    ) == "grid"
    assert await frame.locator("#styled-card").evaluate(
        "el => getComputedStyle(el).backgroundColor"
    ) == "rgb(224, 236, 248)"
    # CSS の原文は保持し、@import/url の通信は CSP と probe 検査で拒否を実証する。
    assert await frame.locator("style").evaluate_all(
        "elements => elements.some(el => el.textContent.includes('@import'))"
    )
    assert await frame.locator(
        "body"
    ).evaluate("""element => [...element.querySelectorAll('*')]
      .every(node => [...node.attributes].every(attribute =>
        !/^(on|src|action$|formaction$|poster$|background$)/i.test(attribute.name)
        && (!/href$/i.test(attribute.name) || attribute.value.startsWith('#'))))""")
    bounds = await page.get_by_role("dialog").bounding_box()
    assert bounds and page.viewport_size
    inset = 10 if page.viewport_size["width"] <= 480 else 24
    assert abs(bounds["x"] - inset) <= 1 and abs(bounds["y"] - inset) <= 1, bounds
    assert abs(bounds["width"] - (page.viewport_size["width"] - inset * 2)) <= 1, bounds
    assert abs(bounds["height"] - (page.viewport_size["height"] - inset * 2)) <= 1, bounds
    original_url = page.url
    await frame.get_by_text("Readable link text", exact=True).click()
    await settle(page)
    assert page.url == original_url
    assert await page.evaluate("document.body.dataset.previewMutated === undefined")
    assert await page.evaluate("window.previewMessages.length === 0")


async def scenario(
    browser: Browser, url: str, mode: str, language: str, width: int, output: Path
) -> None:
    """本番 App/client/modal を通し、成功・拒否・読取上限・古い応答を検証する。"""
    api = PreviewApi(url, language, mode)
    context = await browser.new_context(viewport={"width": width, "height": 1000}, locale=language)
    await context.route("**/*", api.route)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(error.stack))
    await install_transport(page, mode)
    name = f"{mode}-{language}-{width}"
    try:
        await page.goto(f"{url}#/documents?project={PROJECT}")
        labels = (await messages(page, language))["documentsPanel"]
        panel = page.locator(".documentPanel")
        await expect(page.locator(".documentItem")).to_have_count(2)
        previews = panel.get_by_role("button", name=labels["previewButton"], exact=True)
        if mode == "metadata-large":
            await expect(previews.first).to_be_disabled()
            assert not api.content_calls
        else:
            if mode == "timeout":
                await page.clock.install()
            if mode == "same-tick":
                await previews.evaluate_all(
                    "buttons => { buttons[0].click(); buttons[1].click(); }"
                )
            else:
                await previews.first.focus()
                await page.keyboard.press("Enter")
            if mode in LATE_RESPONSES:
                await asyncio.wait_for(api.gate.received.wait(), 5)
                if mode == "close-late":
                    await page.keyboard.press("Escape")
                    await expect(page.get_by_role("dialog")).to_have_count(0)
                    await previews.nth(1).click()
                    await expect(page.locator(".previewText")).to_have_text(SECOND_TEXT)
                elif mode == "project-late":
                    await page.evaluate(
                        "target => location.hash = target", f"/documents?project={NEXT_PROJECT}"
                    )
                    await expect(page.get_by_role("dialog")).to_have_count(0)
                    await expect(page.locator(".documentItem")).to_have_count(2)
                    await previews.nth(1).click()
                    await expect(page.locator(".previewText")).to_have_text(SECOND_TEXT)
                elif mode == "actor-late":
                    await page.evaluate("location.hash = '/accounts'")
                    await expect(page.locator("[data-account-own]")).to_be_visible()
                    await self_revoke(page, api, (await messages(page, language))["account"])
                    api.actor = OTHER
                    await page.locator('input[name="email"]').fill(api.users[OTHER]["email"])
                    await page.locator('input[name="password"]').fill(PASSWORD)
                    await page.locator('button[type="submit"]').click()
                    await expect(page.locator("[data-account-own]")).to_be_visible()
                elif mode in DELAYED_TIMERS:
                    await page.evaluate("""() => {
                      const elapsed = performance.now() + 31000;
                      Object.defineProperty(performance, 'now', {
                        configurable: true, value: () => elapsed,
                      });
                    }""")
                else:
                    await page.clock.run_for(30_001)
                    await expect(page.get_by_role("dialog").get_by_role("alert")).to_be_visible()
                api.gate.release.set()
                await asyncio.wait_for(api.gate.returned.wait(), 5)
                await settle(page)
                await expect(page.locator('input[name="email"]')).to_have_count(0)
                await expect(page.locator("iframe.previewFrame")).to_have_count(0)
                if mode in ("close-late", "project-late"):
                    await expect(page.locator(".previewText")).to_have_text(SECOND_TEXT)
                    await page.keyboard.press("Escape")
                    await expect(panel.locator('input[type="file"]').first).to_be_enabled()
                elif mode == "actor-late":
                    await expect(page.locator(".sidebarUser")).to_contain_text(
                        api.users[OTHER]["email"]
                    )
                elif mode in DELAYED_TIMERS:
                    await expect(page.get_by_role("dialog").get_by_role("alert")).to_have_text(
                        labels["failures"]["loadFailed"]
                    )
                    assert await page.evaluate("window.previewDeadlineCallbacks") == 0
                    await page.keyboard.press("Escape")
                    await expect(panel.locator('input[type="file"]').first).to_be_enabled()
                    remove = panel.get_by_role("button", name=labels["remove"], exact=True).first
                    await expect(remove).to_be_enabled()
                    await remove.click()
                    await expect(page.get_by_role("dialog")).to_be_visible()
                    await expect(
                        page.get_by_role("dialog").get_by_role(
                            "button", name=labels["remove"], exact=True
                        )
                    ).to_be_enabled()
                    await page.keyboard.press("Escape")
                    assert api.content_calls == [(PROJECT, DOCUMENT)]
                else:
                    await expect(page.get_by_role("dialog").get_by_role("alert")).to_be_visible()
            elif mode == "malicious":
                await static_html(page)
                await expect(page.get_by_text(labels["previewNotice"], exact=True)).to_be_visible()
            elif mode in ("markdown", "same-tick"):
                expected = MARKDOWN if mode == "markdown" else SECOND_TEXT
                await expect(page.locator(".previewText")).to_have_text(expected)
                await expect(page.locator("iframe.previewFrame")).to_have_count(0)
                if mode == "same-tick":
                    assert api.content_calls == [(PROJECT, SECOND)]
            elif mode in ("expired", "headers-expired"):
                await expect(page.locator('input[name="email"]')).to_be_visible()
                await expect(panel).to_have_count(0)
            else:
                key = {
                    "stream-absent": "previewTooLarge",
                    "stream-lying": "previewTooLarge",
                    "invalid-success": "loadFailed",
                    "denied": "denied",
                    "headers-denied": "denied",
                    "project-missing": "denied",
                    "content-missing": "contentMissing",
                    "content-invalid": "contentInvalid",
                    "storage-unavailable": "storageUnavailable",
                }[mode]
                await expect(page.get_by_role("dialog").get_by_role("alert")).to_have_text(
                    labels["failures"][key]
                )
                await expect(page.locator("iframe.previewFrame, .previewText")).to_have_count(0)
                if mode.startswith("stream-"):
                    assert await page.evaluate("window.previewStream") == {
                        "requests": 1,
                        "pulls": 3,
                        "cancelled": 1,
                    }
                if mode in ("denied", "headers-denied", "project-missing"):
                    await page.keyboard.press("Escape")
                    await panel.get_by_role("button", name=labels["refresh"], exact=True).click()
                    await settle(page)
                    await expect(panel.locator('input[type="file"]').first).to_be_disabled()
                    for button in await panel.get_by_role(
                        "button", name=labels["remove"], exact=True
                    ).all():
                        await expect(button).to_be_disabled()
        if mode.startswith("headers-"):
            assert not api.content_calls
            assert await page.evaluate("window.previewStream") == {
                "requests": 1,
                "pulls": 0,
                "cancelled": 1,
            }
        await settle(page)
        await privacy(page)
        assert PRIVATE not in await page.locator("body").inner_text()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert len(context.pages) == 1 and not api.probes
        assert not errors and not api.unexpected and not api.failures, (
            errors,
            api.unexpected,
            api.failures,
        )
        assert not [call for call in api.calls if call[1].endswith("auth/logout")]
        await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
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
    """三語/狭幅と境界ケースを分離し、実 API や外部 resource は一切呼び出さない。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ("zh", "ja", "en"):
                for width in (390, 1440):
                    await scenario(browser, url, "malicious", language, width, output)
                for mode in (
                    "stream-absent",
                    "denied",
                    "content-missing",
                    "content-invalid",
                    "storage-unavailable",
                ):
                    await scenario(browser, url, mode, language, 390, output)
            for mode in (
                "stream-lying",
                "invalid-success",
                "metadata-large",
                "project-missing",
                "expired",
                "close-late",
                "project-late",
                "actor-late",
                "timeout",
                "markdown",
                "same-tick",
                "headers-expired",
                "headers-denied",
                *DELAYED_TIMERS,
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
