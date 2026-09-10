"""実 App の原文書削除・未知核対を全面 mock HTTP で検証する。実 storage/DB は使わない。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from check_accounts import ACTOR, CSRF, OTHER, PASSWORD, ResponseGate, self_revoke
from check_projects import (
    ARCHIVED,
    NEXT_PROJECT,
    PROJECT,
    ProjectsApi,
    menu,
    messages,
    privacy,
    settle,
)
from playwright.async_api import Browser, Page, Route, async_playwright, expect

DOCUMENT = "00000000-0000-4000-8000-000000000091"
SECOND = "00000000-0000-4000-8000-000000000092"
PRIVATE = "Private document reference must never be shown"


def document(project_id: str, document_id: str = DOCUMENT) -> dict:
    """厳格 client に一致する、実資源と無関係な metadata を用意する。"""
    return {
        "document_id": document_id,
        "project_id": project_id,
        "folder": "specs",
        "name": "overview.md" if document_id == DOCUMENT else "second.md",
        "size": 10,
        "mime": "text/markdown",
        "checksum": "sha256:" + "a" * 64,
        "uploaded_by": ACTOR,
        "created_at": "2026-09-09T00:00:00Z",
    }


class DocumentsApi(ProjectsApi):
    """現在目录と原 DELETE の応答を分離し、同名新 ID が核対に混ざらないことを確認する。"""

    def __init__(self, url: str, language: str, mode: str) -> None:
        """各 scenario に独立した原文書・失敗・gate を作る。"""
        super().__init__(url, language)
        self.mode = mode
        self.rows = {
            identity: [document(identity), document(identity, SECOND)]
            for identity in (PROJECT, NEXT_PROJECT, ARCHIVED)
        }
        self.delete_calls: list[str] = []
        self.exact_reads: list[str] = []
        self.gate = ResponseGate()
        self.read_gate = ResponseGate()

    async def problem(self, route: Route, status: int, code: str) -> None:
        """code だけを表示規則に使わせ、内部 detail の漏洩を検知する。"""
        await route.fulfill(
            status=status,
            json={"status": status, "title": "Fixture refusal", "code": code, "detail": PRIVATE},
        )

    async def respond(self, route: Route) -> None:
        """明示した文書 API だけを処理し、それ以外は既存 App fixture の厳格判定に渡す。"""
        request = route.request
        address = urlsplit(request.url)
        parts = address.path.removeprefix(self.prefix).split("/")
        if len(parts) < 3 or parts[0] != "projects" or parts[2] != "documents":
            await super().respond(route)
            return
        assert f"{address.scheme}://{address.netloc}" == self.origin and parts[1] in self.rows
        project_id = parts[1]
        if request.method == "GET":
            if len(parts) == 3:
                await route.fulfill(json={"documents": self.rows[project_id]})
            elif len(parts) == 5 and parts[4] == "content":
                await route.fulfill(content_type="text/markdown", body="# Fixture")
            elif len(parts) == 4:
                self.exact_reads.append(parts[3])
                assert parts[3] == DOCUMENT, "Read drifted from original ID"
                if self.mode in {"read-timeout-401", "read-absolute-401"}:
                    self.read_gate.received.set()
                    await asyncio.wait_for(self.read_gate.release.wait(), 45)
                    await self.problem(route, 401, "authentication_required")
                    self.read_gate.returned.set()
                elif self.mode == "read-expired":
                    await self.problem(route, 401, "authentication_required")
                elif self.mode == "read-denied" or (
                    self.mode == "recheck-denied" and len(self.exact_reads) > 1
                ):
                    await self.problem(route, 404, "project_not_found")
                elif self.mode == "unknown-absent":
                    await self.problem(route, 404, "document_not_found")
                else:
                    await route.fulfill(json=document(project_id))
            else:
                raise AssertionError("Unknown document read")
            return
        assert request.method == "DELETE" and len(parts) == 4 and request.post_data is None
        assert request.headers.get("x-csrf-token") == CSRF
        assert request.headers.get("origin") == self.origin
        self.delete_calls.append(parts[3])
        if self.mode in {"switch", "timeout", "actor-switch"}:
            self.gate.received.set()
            await asyncio.wait_for(self.gate.release.wait(), 45)
        if self.mode in {
            "unknown-present",
            "unknown-absent",
            "read-denied",
            "recheck-denied",
            "read-expired",
            "read-timeout-401",
            "read-absolute-401",
        }:
            if self.mode == "unknown-absent":
                self.rows[project_id] = [{**document(project_id, SECOND), "name": "overview.md"}]
            await self.problem(route, 500, "server_error")
        elif self.mode == "invalid-success":
            await route.fulfill(status=202, json={})
        elif self.mode in {"in-use", "references-unavailable", "denied", "expired"}:
            status, code = {
                "in-use": (409, "document_in_use"),
                "references-unavailable": (409, "document_references_unavailable"),
                "denied": (403, "csrf_rejected"),
                "expired": (401, "authentication_required"),
            }[self.mode]
            await self.problem(route, status, code)
        elif self.mode == "actor-switch":
            await self.problem(route, 401, "authentication_required")
        else:
            self.rows[project_id] = [
                item for item in self.rows[project_id] if item["document_id"] != parts[3]
            ]
            await route.fulfill(status=204)
        self.gate.returned.set()


async def delete_first(page: Page, labels: dict) -> None:
    """同 tick の別文書 click と確認の二重 click でも原 ID 一件だけを送る。"""
    await expect(page.locator(".documentItem")).to_have_count(2)
    await page.locator(".documentItem button").evaluate_all("""buttons => {
      const removal = buttons.filter(button => button.textContent === buttons.at(-1).textContent);
      removal[0].click(); removal[1].click();
    }""")
    dialog = page.get_by_role("dialog")
    await expect(dialog).to_be_visible()
    confirm = dialog.get_by_role("button", name=labels["remove"], exact=True)
    await confirm.focus()
    await page.keyboard.press("Enter")


async def scenario(
    browser: Browser, url: str, mode: str, language: str, width: int, output: Path
) -> None:
    """実 App・共有 hook・client を使い、拒否/未知/切替/狭幅を観測する。"""
    api = DocumentsApi(url, language, mode)
    context = await browser.new_context(viewport={"width": width, "height": 1000}, locale=language)
    await context.route("**/*", api.route)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(error.stack))
    await page.add_init_script("""(() => {
      const send = window.fetch.bind(window);
      window.fetch = (input, init) => send(input, init ? {...init, signal: undefined} : init);
    })();""")
    name = f"{mode}-{language}-{width}"
    try:
        await page.goto(f"{url}#/documents?project={ARCHIVED if mode == 'archived' else PROJECT}")
        labels = (await messages(page, language))["documentsPanel"]
        await expect(page.locator(".documentItem")).to_have_count(2)
        panel = page.locator(".documentPanel")
        if mode == "archived":
            for button in await panel.get_by_role(
                "button", name=labels["remove"], exact=True
            ).all():
                await expect(button).to_be_disabled()
            await expect(panel.locator('input[type="file"]').first).to_be_disabled()
            assert not api.delete_calls
        else:
            if mode in {"timeout", "read-timeout-401", "read-absolute-401"}:
                await page.clock.install()
            await delete_first(page, labels)
            if mode in {"switch", "timeout", "actor-switch"}:
                await asyncio.wait_for(api.gate.received.wait(), 5)
                if mode == "switch":
                    await menu(page)
                    await page.locator(".sideNavProject select").select_option(NEXT_PROJECT)
                    await expect(page.locator(".documentItem")).to_have_count(2)
                    api.gate.release.set()
                    await asyncio.wait_for(api.gate.returned.wait(), 5)
                    await settle(page)
                    await expect(
                        panel.get_by_text(labels["unknownTitle"], exact=True)
                    ).to_have_count(0)
                    await expect(page.locator(".documentItem")).to_have_count(2)
                elif mode == "actor-switch":
                    await page.evaluate("location.hash = '/accounts'")
                    await expect(page.locator("[data-account-own]")).to_be_visible()
                    await self_revoke(page, api, (await messages(page, language))["account"])
                    api.actor = OTHER
                    await page.locator('input[name="email"]').fill(api.users[OTHER]["email"])
                    await page.locator('input[name="password"]').fill(PASSWORD)
                    await page.locator('button[type="submit"]').click()
                    await expect(page.locator("[data-account-own]")).to_be_visible()
                    api.gate.release.set()
                    await asyncio.wait_for(api.gate.returned.wait(), 5)
                    await settle(page)
                    await expect(page.locator('input[name="email"]')).to_have_count(0)
                    await expect(page.locator(".sidebarUser")).to_contain_text(
                        api.users[OTHER]["email"]
                    )
                else:
                    await page.clock.run_for(30_001)
                    await expect(
                        panel.get_by_text(labels["unknownTitle"], exact=True)
                    ).to_be_visible()
                    api.gate.release.set()
                    await asyncio.wait_for(api.gate.returned.wait(), 5)
                    await expect(
                        panel.get_by_text(labels["unknownTitle"], exact=True)
                    ).to_be_visible()
            elif mode == "success":
                await expect(page.locator(".documentItem")).to_have_count(1)
            elif mode == "expired":
                await expect(page.locator(".documentPanel")).to_have_count(0)
            elif mode in {"in-use", "references-unavailable", "denied"}:
                key = {
                    "in-use": "inUse",
                    "references-unavailable": "referencesUnavailable",
                    "denied": "denied",
                }[mode]
                await expect(panel.get_by_role("alert")).to_have_text(labels["failures"][key])
                if mode == "denied":
                    await panel.get_by_role("button", name=labels["refresh"], exact=True).click()
                    await expect(
                        panel.get_by_role("button", name=labels["remove"], exact=True).first
                    ).to_be_disabled()
            else:
                await expect(panel.get_by_text(labels["unknownTitle"], exact=True)).to_be_visible()
                assert not api.exact_reads, "Unknown automatically queried or resent"
                await panel.get_by_role("button", name=labels["refresh"], exact=True).click()
                await settle(page)
                await expect(
                    panel.get_by_role("button", name=labels["remove"], exact=True).first
                ).to_be_disabled()
                await expect(panel.locator('input[type="file"]').first).to_be_disabled()
                await panel.get_by_role("button", name=labels["checkOriginal"], exact=True).click()
                if mode in {"read-timeout-401", "read-absolute-401"}:
                    await asyncio.wait_for(api.read_gate.received.wait(), 5)
                    if mode == "read-timeout-401":
                        await page.clock.run_for(30_001)
                    else:
                        # timer を発火させず、loader 内の早すぎる失効通知を検知する。
                        await page.evaluate("""() => {
                          const originalNow = performance.now.bind(performance);
                          performance.now = () => originalNow() + 31_000;
                        }""")
                    api.read_gate.release.set()
                    await asyncio.wait_for(api.read_gate.returned.wait(), 5)
                    await expect(
                        panel.get_by_text(labels["failures"]["loadFailed"], exact=True)
                    ).to_be_visible()
                    await expect(panel.get_by_text(labels["checking"], exact=True)).to_have_count(0)
                    await expect(
                        panel.get_by_text(labels["unknownTitle"], exact=True)
                    ).to_be_visible()
                    await expect(page.locator('input[name="email"]')).to_have_count(0)
                    await expect(
                        panel.get_by_role("button", name=labels["release"], exact=True)
                    ).to_have_count(0)
                elif mode == "read-expired":
                    await expect(panel).to_have_count(0)
                    await expect(page.locator('input[name="email"]')).to_be_visible()
                elif mode == "read-denied":
                    await expect(
                        panel.get_by_text(labels["failures"]["denied"], exact=True).first
                    ).to_be_visible()
                    await expect(
                        panel.get_by_role("button", name=labels["release"], exact=True)
                    ).to_have_count(0)
                    await expect(panel.get_by_text(labels["checking"], exact=True)).to_have_count(0)
                else:
                    fact = "absent" if mode == "unknown-absent" else "present"
                    await expect(panel.get_by_text(labels[fact], exact=True)).to_be_visible()
                    await page.screenshot(path=str(output / f"{name}-unknown.png"), full_page=True)
                    if mode in {"unknown-present", "recheck-denied"}:
                        await panel.evaluate(
                            """(element, labels) => {
                          const buttons = [...element.querySelectorAll('button')];
                          const release = buttons.find(
                            button => button.textContent === labels.release);
                          buttons.find(button => button.textContent === labels.check).click();
                          release.click();
                        }""",
                            {"check": labels["checkOriginal"], "release": labels["release"]},
                        )
                        await expect(
                            panel.get_by_text(labels["unknownTitle"], exact=True)
                        ).to_be_visible()
                    if mode == "recheck-denied":
                        await expect(
                            panel.get_by_text(labels["failures"]["denied"], exact=True).first
                        ).to_be_visible()
                        await expect(
                            panel.get_by_role("button", name=labels["release"], exact=True)
                        ).to_have_count(0)
                    else:
                        await expect(panel.get_by_text(labels[fact], exact=True)).to_be_visible()
                        await panel.get_by_role(
                            "button", name=labels["release"], exact=True
                        ).click()
                        await expect(
                            panel.get_by_text(labels["unknownTitle"], exact=True)
                        ).to_have_count(0)
                expected_reads = 2 if mode in {"unknown-present", "recheck-denied"} else 1
                assert api.exact_reads == [DOCUMENT] * expected_reads
            assert api.delete_calls == [DOCUMENT], api.delete_calls
        await settle(page)
        await privacy(page)
        assert PRIVATE not in await page.locator("body").inner_text()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not errors and not api.unexpected and not api.failures, (
            errors,
            api.unexpected,
            api.failures,
        )
        await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
        print(f"PASS {name}", flush=True)
    except Exception:
        await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        print("Fixture errors:", errors, api.unexpected, api.failures, flush=True)
        raise
    finally:
        api.gate.release.set()
        api.read_gate.release.set()
        api.release.set()
        await context.close()


async def check(url: str, output: Path) -> None:
    """三語/窄屏と失敗時系列を別 case で検証し、実環境 write は行わない。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ("zh", "ja", "en"):
                for width in (390, 1440):
                    await scenario(browser, url, "unknown-present", language, width, output)
            for mode in (
                "success",
                "unknown-absent",
                "invalid-success",
                "in-use",
                "references-unavailable",
                "denied",
                "expired",
                "read-denied",
                "recheck-denied",
                "read-expired",
                "read-timeout-401",
                "read-absolute-401",
                "switch",
                "actor-switch",
                "timeout",
                "archived",
            ):
                await scenario(browser, url, mode, "en", 390, output)
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if urlsplit(arguments.url).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Only an owned loopback mock server is allowed")
    asyncio.run(check(arguments.url, arguments.output))
