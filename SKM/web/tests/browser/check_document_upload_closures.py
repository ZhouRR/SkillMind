"""実 App で公開停止・原 batch の照合・独立読取を全面 mock HTTP で検証する。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from check_accounts import CSRF, OTHER, PASSWORD, ResponseGate, self_revoke
from check_document_management import PRIVATE
from check_document_upload import UploadBrowserAudit
from check_document_upload_receipts import (
    KEY,
    NEXT_KEY,
    UploadReceiptsApi,
    choose,
    inject_selection,
    new_write_closed,
    original_batch,
    recover_key,
    summary,
)
from check_projects import ARCHIVED, NEXT_PROJECT, PROJECT, menu, messages, privacy, settle
from playwright.async_api import Browser, Page, Route, async_playwright, expect


class ClosureApi(UploadReceiptsApi):
    """文書公開と停止受付記録を別々に観測し、storage の完了は模倣しない。"""

    def __init__(self, url: str, language: str, mode: str, late: str) -> None:
        """既存の厳格な HTTP fixture に新 endpoint だけを追加する。"""
        super().__init__(url, language, "sequence-unknown", late)
        self.closure_mode = mode
        self.closure_posts: list[tuple[str, str]] = []
        self.closure_reads: list[tuple[str, str]] = []
        self.closure_read_result = "closed"
        self.closure_gates: dict[tuple[str, str], ResponseGate] = {}

    async def respond(self, route: Route) -> None:
        """停止 POST は原 key と固定確認だけを送り、GET は一切書き込まない。"""
        request = route.request
        address = urlsplit(request.url)
        parts = address.path.removeprefix(self.prefix).split("/")
        if not (
            len(parts) == 5
            and parts[0] == "projects"
            and parts[2] == "document-uploads"
            and parts[4] == "closure"
        ):
            await super().respond(route)
            return
        project_id, key = parts[1], parts[3]
        assert project_id in self.rows and UUID(key).int != 0
        assert not address.query and request.method in ("GET", "POST")
        if request.method == "POST":
            assert project_id != ARCHIVED
            assert request.post_data_json == {"confirmation": "STOP_PUBLICATION"}
            assert request.headers.get("x-csrf-token") == CSRF
            assert request.headers.get("origin") == self.origin
            assert request.headers.get("content-type") == "application/json"
            assert (project_id, key) not in self.closure_posts, "Automatic closure POST retry"
            assert (
                key in [item["key"] for item in self.uploads]
                or (project_id, key, self.actor) in self.reads
            ), "Unsent or unchecked key was closed"
            self.closure_posts.append((project_id, key))
            result = {
                "already-published": "already-published",
                "post-not-found": "upload-not-found",
                "post-invalid": "invalid",
                "post-expired": "expired",
                "post-denied": "denied",
                "post-project-missing": "project-missing",
            }.get(self.closure_mode, "closed")
            if self.closure_mode in ("unknown", "recover-unknown") or self.closure_mode.startswith(
                "read-"
            ):
                result = "unavailable"
        else:
            assert request.post_data is None
            self.closure_reads.append((project_id, key))
            result = self.closure_read_result
        metadata = dict(self.records[key])
        gate = self.closure_gates.get((request.method, key))
        if gate:
            gate.received.set()
            await asyncio.wait_for(gate.release.wait(), 45)
            if gate.failure:
                await self.problem(route, *gate.failure)
                gate.returned.set()
                return
        refusal = {
            "already-published": (409, "document_upload_already_published"),
            "upload-not-found": (404, "document_upload_not_found"),
            "not-found": (404, "document_upload_closure_not_found"),
            "unavailable": (503, "document_upload_unavailable"),
            "expired": (401, "authentication_required"),
            "denied": (403, "csrf_rejected"),
            "project-missing": (404, "project_not_found"),
        }.get(result)
        if refusal:
            await self.problem(route, *refusal)
        else:
            receipt = {
                "upload_key": key,
                "project_id": project_id,
                "document_id": metadata["document_id"],
                "closed_at": "2026-09-10T00:00:00Z",
                "publication_state": "CLOSED",
            }
            if result == "invalid":
                receipt["upload_key"] = NEXT_KEY
            await route.fulfill(status=201 if request.method == "POST" else 200, json=receipt)
        if gate:
            gate.returned.set()


async def lookup(page: Page, labels: dict, key: str) -> None:
    """実入力と同 tick の二重 click で、独立読取の防重を検査する。"""
    help_panel = page.locator(".documentHelp")
    if await help_panel.get_attribute("open") is None:
        await help_panel.locator(":scope > summary").click()
    form = page.locator(".documentUploadClosureRecovery")
    if await form.get_attribute("open") is None:
        await form.locator("summary").click()
    await form.get_by_label(labels["upload"]["recoveryKey"], exact=True).fill(key)
    await form.get_by_role("button", name=labels["closure"]["recover"], exact=True).evaluate(
        "button => { button.click(); button.click(); }"
    )


async def close_lookup(page: Page, labels: dict) -> None:
    """独立読取だけを keyboard で終了し、原 batch には触れない。"""
    result = page.locator(".documentUploadClosureRecoveryResult")
    button = result.get_by_role("button", name=labels["closure"]["closeRecovery"], exact=True)
    await button.focus()
    await page.keyboard.press("Enter")
    await expect(result).to_have_count(0)


async def prepare(page: Page, labels: dict) -> None:
    """確認を開くだけでは POST されず、原 key がコピー可能なことを検証する。"""
    panel = page.locator(".documentUploadClosure")
    await panel.get_by_role("button", name=labels["closure"]["prepare"], exact=True).evaluate(
        "button => { button.click(); button.click(); }"
    )
    await expect(
        panel.get_by_text(labels["closure"]["phase"]["confirming"], exact=True)
    ).to_be_visible()


async def submit(page: Page, labels: dict) -> None:
    """固定確認を二重 click しても同じ原停止は一度だけ送信する。"""
    await (
        page.locator(".documentUploadClosureIntent")
        .get_by_role("button", name=labels["closure"]["confirm"], exact=True)
        .evaluate("button => { button.click(); button.click(); }")
    )


async def check_original(page: Page, labels: dict) -> None:
    """元 batch 自身の停止 GET にだけ確認権を与える。"""
    await (
        page.locator(".documentUploadClosure")
        .get_by_role("button", name=labels["closure"]["check"], exact=True)
        .evaluate("button => { button.click(); button.click(); }")
    )


async def continue_original(page: Page, labels: dict, api: ClosureApi, before: list[dict]) -> None:
    """公開停止を成功数に混ぜず、人工続行後も原 UUID/bytes をそのまま送る。"""
    await summary(page, ["published", "closed", "queued"], labels)
    assert len(api.uploads) == 2
    await new_write_closed(page, labels)
    await page.get_by_role("button", name=labels["upload"]["continueRemaining"], exact=True).click()
    await summary(page, ["published", "closed", "published"], labels)
    assert [item["key"] for item in api.uploads] == [item["key"] for item in before]
    assert [item["body"] for item in api.uploads] == [b"first.md", b"second.md", b"third.md"]
    assert len(api.closure_posts) == 1


async def switch_owner(page: Page, api: ClosureApi, language: str, actor: bool) -> None:
    """現在の App 選択または有効 session を実操作で置換する。"""
    if not actor:
        await menu(page)
        await page.locator(".sideNavProject select").select_option(NEXT_PROJECT)
        await expect(page.locator(".documentUploadItems > li")).to_have_count(0)
        return
    await page.evaluate("location.hash='/accounts'")
    await expect(page.locator("[data-account-own]")).to_be_visible()
    await self_revoke(page, api, (await messages(page, language))["account"])
    api.actor = OTHER
    await page.locator('input[name="email"]').fill(api.users[OTHER]["email"])
    await page.locator('input[name="password"]').fill(PASSWORD)
    await page.locator('button[type="submit"]').click()
    await expect(page.locator("[data-account-own]")).to_be_visible()


async def scenario(
    browser: Browser,
    url: str,
    mode: str,
    language: str,
    width: int,
    output: Path,
    late: str = "closed",
) -> None:
    """各 case は独立 context を持ち、外部通信・console・遅延要求を全件監査する。"""
    api = ClosureApi(url, language, mode, late)
    context = await browser.new_context(
        viewport={"width": width, "height": 1000}, locale=language, service_workers="block"
    )
    await context.route("**/*", api.route)
    page = await context.new_page()
    audit = UploadBrowserAudit(page, api)
    await page.add_init_script("""(() => {
      const send = window.fetch.bind(window);
      window.fetch = (input, init) => send(input, init ? {...init, signal: undefined} : init);
      window.closureDeadlineCallbacks = 0;
      const schedule = window.setTimeout.bind(window);
      window.setTimeout = (fn, delay, ...args) => schedule(() => {
        if (delay === 30000) window.closureDeadlineCallbacks++;
        fn(...args);
      }, delay);
    })();""")
    name = f"{mode}-{late}-{language}-{width}"
    try:
        project_id = ARCHIVED if mode == "manual-archived" else PROJECT
        await page.goto(f"{url}#/documents?project={project_id}")
        labels = (await messages(page, language))["documentsPanel"]
        texts = labels["closure"]
        panel = page.locator(".documentPanel")
        closure = page.locator(".documentUploadClosure")
        intent = page.locator(".documentUploadClosureIntent")
        await expect(panel.locator(".documentItem")).to_have_count(2)
        if "timeout" in mode:
            await page.clock.install()
        if mode.startswith("recover-"):
            await page.reload()
            await expect(panel.locator(".documentItem")).to_have_count(2)
            await recover_key(page, KEY, labels)
            await expect(
                page.locator(".documentUploadRecoveryResult").get_by_text(
                    labels["upload"]["recoveryPending"], exact=True
                )
            ).to_be_visible()
            await closure.get_by_role("button", name=texts["prepareRecovered"], exact=True).click()
            await expect(intent.get_by_label(labels["upload"]["key"], exact=True)).to_have_value(
                KEY
            )
            assert not api.closure_posts and not api.uploads
            await submit(page, labels)
            if mode == "recover-unknown":
                await expect(
                    intent.get_by_text(texts["phase"]["unknown"], exact=True)
                ).to_be_visible()
                await new_write_closed(page, labels)
                await lookup(page, labels, KEY)
                await expect(
                    page.locator(".documentUploadClosureRecoveryResult").get_by_text(
                        texts["phase"]["closed"], exact=True
                    )
                ).to_be_visible()
                await close_lookup(page, labels)
                await new_write_closed(page, labels)
                api.closure_read_result = "not-found"
                await check_original(page, labels)
                await expect(
                    intent.get_by_text(texts["failures"]["notFound"], exact=True)
                ).to_be_visible()
                await new_write_closed(page, labels)
                api.closure_read_result = "closed"
                await check_original(page, labels)
            await expect(intent.get_by_text(texts["phase"]["closed"], exact=True)).to_be_visible()
            await summary(page, [], labels)
            assert not api.uploads and api.closure_posts == [(PROJECT, KEY)]
            await new_write_closed(page, labels)
            await intent.get_by_role("button", name=texts["finishRecovery"], exact=True).click()
            await expect(intent).to_have_count(0)
            await expect(panel.locator('input[type="file"]').first).to_be_enabled()
        elif mode.startswith("manual-"):
            gate = ResponseGate()
            if mode != "manual-archived":
                api.closure_gates["GET", KEY] = gate
                if late == "401":
                    gate.failure = (401, "authentication_required")
            await lookup(page, labels, KEY)
            result = page.locator(".documentUploadClosureRecoveryResult")
            if mode == "manual-archived":
                await expect(
                    result.get_by_text(texts["phase"]["closed"], exact=True)
                ).to_be_visible()
                await expect(panel.locator('input[type="file"]').first).to_be_disabled()
                await expect(
                    closure.get_by_role("button", name=texts["prepare"], exact=True)
                ).to_have_count(0)
                await page.reload()
                await expect(panel.locator(".documentItem")).to_have_count(2)
                await expect(result).to_have_count(0)
                await lookup(page, labels, KEY)
                await expect(
                    result.get_by_text(texts["phase"]["closed"], exact=True)
                ).to_be_visible()
            else:
                await asyncio.wait_for(gate.received.wait(), 5)
                if mode == "manual-replace":
                    await lookup(page, labels, NEXT_KEY)
                    await expect(
                        result.get_by_label(labels["upload"]["key"], exact=True)
                    ).to_have_value(NEXT_KEY)
                elif mode == "manual-switch":
                    await switch_owner(page, api, language, False)
                elif mode == "manual-start":
                    await choose(page, "first.md")
                    await summary(page, ["published"], labels)
                    await expect(result).to_have_count(0)
                else:
                    await close_lookup(page, labels)
                gate.release.set()
                await asyncio.wait_for(gate.returned.wait(), 5)
                await settle(page)
                await expect(page.locator('input[name="email"]')).to_have_count(0)
                assert not api.closure_posts
                assert len(api.uploads) == (1 if mode == "manual-start" else 0)
        else:
            await choose(page, "first.md", "second.md", "third.md")
            await summary(page, ["published", "unknown", "queued"], labels)
            before = await original_batch(page)
            key = before[1]["key"]
            await new_write_closed(page, labels)
            assert len(api.uploads) == 2
            gate = ResponseGate()
            gated = mode in {
                f"{method}-{action}"
                for method in ("post", "read")
                for action in ("cancel", "timeout", "deadline", "switch", "actor")
            }
            if gated:
                api.closure_gates["POST" if mode.startswith("post-") else "GET", key] = gate
                if late == "401":
                    gate.failure = (401, "authentication_required")
            await prepare(page, labels)
            assert not api.closure_posts
            if mode == "success":
                await intent.get_by_role(
                    "button", name=texts["cancelPreparation"], exact=True
                ).click()
                await summary(page, ["published", "unknown", "queued"], labels)
                await prepare(page, labels)
            await inject_selection(page, "must-not-send.md")
            await submit(page, labels)
            if mode.startswith("read-"):
                await expect(
                    intent.get_by_text(texts["phase"]["unknown"], exact=True)
                ).to_be_visible()
                api.closure_read_result = {
                    "read-expired": "expired",
                    "read-denied": "denied",
                    "read-project-missing": "project-missing",
                }.get(mode, "closed")
                await check_original(page, labels)
            if gated:
                await asyncio.wait_for(gate.received.wait(), 5)
                if mode.endswith("cancel"):
                    await intent.get_by_role(
                        "button",
                        name=texts["cancelCheck" if mode.startswith("read-") else "cancelWait"],
                        exact=True,
                    ).click()
                elif mode.endswith("timeout"):
                    await page.clock.run_for(30_001)
                elif mode.endswith("deadline"):
                    await page.evaluate(
                        "() => { const late = performance.now()+31000; performance.now=()=>late; }"
                    )
                else:
                    await switch_owner(page, api, language, mode.endswith("actor"))
                gate.release.set()
                await asyncio.wait_for(gate.returned.wait(), 5)
                await settle(page)
                await expect(page.locator('input[name="email"]')).to_have_count(0)
                if not mode.endswith(("switch", "actor")):
                    await summary(page, ["published", "unknown", "queued"], labels)
                    await new_write_closed(page, labels)
                if mode.endswith("deadline"):
                    assert await page.evaluate("window.closureDeadlineCallbacks") == 0
            elif mode.endswith("expired"):
                await expect(page.locator('input[name="email"]')).to_be_visible()
            elif mode.endswith(("denied", "project-missing")):
                await expect(
                    panel.get_by_text(labels["failures"]["denied"], exact=True)
                ).to_be_visible()
                await new_write_closed(page, labels)
            elif mode == "unknown":
                await expect(
                    intent.get_by_text(texts["phase"]["unknown"], exact=True)
                ).to_be_visible()
                await lookup(page, labels, key)
                await expect(
                    page.locator(".documentUploadClosureRecoveryResult").get_by_text(
                        texts["phase"]["closed"], exact=True
                    )
                ).to_be_visible()
                await close_lookup(page, labels)
                await summary(page, ["published", "unknown", "queued"], labels)
                await new_write_closed(page, labels)
                for result, failure in (("not-found", "notFound"), ("unavailable", "unavailable")):
                    api.closure_read_result = result
                    await check_original(page, labels)
                    await expect(
                        intent.get_by_text(texts["failures"][failure], exact=True)
                    ).to_be_visible()
                    await summary(page, ["published", "unknown", "queued"], labels)
                    assert len(api.closure_posts) == 1 and len(api.uploads) == 2
                api.closure_read_result = "closed"
                await check_original(page, labels)
                await continue_original(page, labels, api, before)
            elif mode in ("already-published", "post-not-found"):
                failure = "alreadyPublished" if mode == "already-published" else "uploadNotFound"
                await expect(
                    intent.get_by_text(texts["failures"][failure], exact=True)
                ).to_be_visible()
                await summary(page, ["published", "unknown", "queued"], labels)
                assert len(api.uploads) == 2 and not api.delete_calls
                if mode == "already-published":
                    api.read_result = "PUBLISHED"
                    await (
                        page.locator(".documentUploadStatus")
                        .get_by_role("button", name=labels["upload"]["checkOriginal"], exact=True)
                        .click()
                    )
                    await summary(page, ["published", "published", "queued"], labels)
                    assert len(api.uploads) == 2
            elif mode == "post-invalid":
                await expect(
                    intent.get_by_text(texts["phase"]["unknown"], exact=True)
                ).to_be_visible()
                await new_write_closed(page, labels)
            else:
                await expect(
                    intent.get_by_text(texts["phase"]["closed"], exact=True)
                ).to_be_visible()
                await continue_original(page, labels, api, before)
        await settle(page)
        audit.verify()
        for selector in (".documentUploadClosureIntent", ".documentUploadClosureRecoveryResult"):
            result = page.locator(selector)
            if await result.count():
                content = await result.inner_text()
                if texts["closedAt"] in content:
                    assert texts["documentId"] in content
                    assert labels["upload"]["documentId"] not in content, (labels["upload"]["documentId"], content)
        await privacy(page)
        assert PRIVATE not in await page.locator("body").inner_text()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
        if await closure.is_visible():
            await closure.scroll_into_view_if_needed()
            await page.screenshot(path=str(output / f"viewport-{name}.png"))
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
        for gate in api.closure_gates.values():
            gate.release.set()
        await context.close()


async def check(url: str, output: Path) -> None:
    """全 case を新 context で実行し、自分の browser を必ず終了する。"""
    output.mkdir(parents=True, exist_ok=True)
    cases = [
        (mode, language, width, "closed")
        for language in ("zh", "ja", "en")
        for width in (390, 1440)
        for mode in ("success", "unknown", "manual-archived", "recover-success", "recover-unknown")
    ]
    cases += [
        (mode, "zh", 390, "closed")
        for mode in (
            "already-published",
            "post-not-found",
            "post-invalid",
            "post-expired",
            "post-denied",
            "post-project-missing",
            "read-expired",
            "read-denied",
            "read-project-missing",
        )
    ]
    cases += [
        (f"{method}-{action}", "zh", 390, late)
        for method in ("post", "read")
        for action in ("cancel", "timeout", "deadline", "switch", "actor")
        for late in ("401", "closed")
    ]
    cases += [
        (f"manual-{action}", "zh", 390, late)
        for action in ("close", "replace", "switch", "start")
        for late in ("401", "closed")
    ]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for mode, language, width, late in cases:
                await scenario(browser, url, mode, language, width, output, late)
        finally:
            await browser.close()
    print(f"PASS {len(cases)} closure browser cases", flush=True)


def main() -> None:
    """明示された loopback harness のみを許可し、実 API へ接続しない。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    address = urlsplit(args.url)
    if (
        address.scheme != "http"
        or address.hostname not in ("127.0.0.1", "localhost", "::1")
        or not address.path.endswith("/tests/browser/projects.html")
        or address.query
        or address.fragment
        or address.username
        or address.password
    ):
        parser.error("Use the explicitly started loopback mock projects harness")
    asyncio.run(check(args.url, args.output))


if __name__ == "__main__":
    main()
