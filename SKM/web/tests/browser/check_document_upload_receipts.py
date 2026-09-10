"""実 App の原 upload・順次 batch・人工照合を全面 mock HTTP で検証する。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from check_accounts import OTHER, PASSWORD, ResponseGate, self_revoke
from check_document_management import PRIVATE, delete_first, document
from check_document_upload import UploadBrowserAudit, UploadMockApi, upload_request
from check_projects import (
    ARCHIVED,
    NEXT_PROJECT,
    PROJECT,
    menu,
    messages,
    privacy,
    settle,
)
from playwright.async_api import Browser, Page, Route, async_playwright, expect

KEY = "00000000-0000-4000-8000-000000000080"
NEXT_KEY = "00000000-0000-4000-8000-000000000081"
POST_GATES = (
    "post-cancel",
    "post-timeout",
    "post-deadline",
    "post-switch",
    "post-actor",
)
READ_GATES = ("read-cancel", "read-timeout", "read-deadline", "read-switch")


class UploadReceiptsApi(UploadMockApi):
    """現在一覧と原公開記録を別々に保持し、受理/応答喪失の時刻を制御する。"""

    def __init__(self, url: str, language: str, mode: str, late: str) -> None:
        """fixture は合成 file 以外の storage や session に接続しない。"""
        super().__init__(url, language, "unknown-present" if mode == "delete-first" else mode)
        self.scenario = mode
        self.late = late
        self.uploads: list[dict] = []
        self.reads: list[tuple[str, str, str]] = []
        self.records = {
            key: {**document(PROJECT, key), "name": f"manual-{index}.md"}
            for index, key in enumerate((KEY, NEXT_KEY), 1)
        }
        self.read_result = "PENDING"
        self.manual_results: dict[tuple[str, str], str] = {}
        self.manual_gates: dict[tuple[str, str], ResponseGate] = {}

    async def respond(self, route: Route) -> None:
        """POST は一 key 一回、GET は元 key のみを返し、同名一覧による確認を許さない。"""
        request = route.request
        address = urlsplit(request.url)
        parts = address.path.removeprefix(self.prefix).split("/")
        if len(parts) >= 3 and parts[0] == "projects" and parts[2] == "document-uploads":
            assert request.method == "GET" and len(parts) == 4
            project_id, key = parts[1], parts[3]
            assert project_id in self.rows and UUID(key).int != 0
            assert not address.query and request.post_data is None
            self.reads.append((project_id, key, self.actor))
            # 応答を受付時に固定し、別 query の fixture 設定を遅れた応答へ混ぜない。
            result = self.manual_results.get((project_id, key), self.read_result)
            metadata = {**self.records[key], "project_id": project_id}
            gate = self.manual_gates.get((project_id, key))
            if self.scenario in READ_GATES:
                gate = self.gate
            if gate:
                gate.received.set()
                await asyncio.wait_for(gate.release.wait(), 45)
                if gate.failure or (self.scenario in READ_GATES and self.late == "401"):
                    status, code = gate.failure or (401, "authentication_required")
                    await self.problem(route, status, code)
                    gate.returned.set()
                    return
            refusal = {
                "not-found": (404, "document_upload_not_found"),
                "unavailable": (503, "document_upload_unavailable"),
                "denied": (403, "csrf_rejected"),
                "expired": (401, "authentication_required"),
                "project-missing": (404, "project_not_found"),
            }.get(result)
            if refusal:
                await self.problem(route, *refusal)
            else:
                state = "PUBLISHED" if result == "wrong-size" else result
                if self.scenario == "wrong-actor":
                    metadata["uploaded_by"] = OTHER
                if result == "wrong-size":
                    metadata["size"] += 1
                await route.fulfill(
                    json={
                        "upload_key": key,
                        "project_id": project_id,
                        "state": state,
                        "created_at": metadata["created_at"],
                        "document": metadata if state == "PUBLISHED" else None,
                    }
                )
            if gate:
                gate.returned.set()
            return
        if (
            len(parts) != 3
            or parts[0] != "projects"
            or parts[2] != "documents"
            or request.method != "POST"
        ):
            await super().respond(route)
            return
        assert f"{address.scheme}://{address.netloc}" == self.origin
        uploaded = upload_request(route, self.origin)
        assert uploaded["key"] not in [item["key"] for item in self.uploads], (
            "Original POST was resent"
        )
        assert uploaded["body"] == uploaded["name"].encode(), "Original bytes changed"
        assert uploaded["mime"] == "text/markdown" and uploaded["folder"] == b""
        self.uploads.append(uploaded)
        metadata = {
            **document(parts[1], str(UUID(int=1000 + len(self.uploads)))),
            "name": uploaded["name"],
            "size": len(uploaded["body"]),
            "folder": "",
            "uploaded_by": self.actor,
        }
        self.records[uploaded["key"]] = metadata
        number = len(self.uploads)
        if self.scenario in POST_GATES or self.scenario == "same-tick":
            self.gate.received.set()
            await asyncio.wait_for(self.gate.release.wait(), 45)
            if self.late == "401":
                await self.problem(route, 401, "authentication_required")
            else:
                await route.fulfill(status=201, json=metadata)
            self.gate.returned.set()
            return
        if self.scenario == "manual-kept-published" or (
            self.scenario == "manual-unknown" and number > 1
        ):
            await route.fulfill(status=201, json=metadata)
        elif self.scenario == "manual-kept-refused" or (
            self.scenario == "sequence-known" and number == 2
        ):
            await self.problem(route, 422, "invalid_document_upload")
        elif (
            self.scenario in ("sequence-known", "sequence-unknown", "pending", "key-conflict")
            and number != 2
        ):
            await route.fulfill(status=201, json=metadata)
        elif self.scenario in ("pending", "key-conflict"):
            await self.problem(
                route,
                409,
                "document_upload_pending"
                if self.scenario == "pending"
                else "document_upload_key_conflict",
            )
        else:
            await self.problem(route, 503, "document_upload_unavailable")


async def choose(page: Page, *names: str) -> None:
    """Browser の実 File input を使い、各 file の bytes と表示名を対応させる。"""
    await page.locator('.documentPanel input[type="file"]').first.set_input_files(
        [{"name": name, "mimeType": "text/markdown", "buffer": name.encode()} for name in names]
    )


async def summary(page: Page, phases: list[str], labels: dict) -> None:
    """成功数の代用に done を使わず、順序を保った各原結果を検証する。"""
    items = page.locator(".documentUploadItems > li")
    await expect(items).to_have_count(len(phases))
    for index, phase in enumerate(phases):
        await expect(
            items.nth(index).get_by_text(labels["upload"]["phase"][phase], exact=True)
        ).to_be_visible()


async def new_write_closed(page: Page, labels: dict) -> None:
    """upload 未知は DELETE と新 file 選択の両方を閉じる。"""
    panel = page.locator(".documentPanel")
    await expect(panel.locator('input[type="file"]').first).to_be_disabled()
    for button in await panel.get_by_role("button", name=labels["remove"], exact=True).all():
        await expect(button).to_be_disabled()


async def inject_selection(page: Page, name: str) -> None:
    """disabled の見た目だけでなく、合成イベントにも同期 gate が効くことを確認する。"""
    await page.locator('.documentPanel input[type="file"]').first.evaluate(
        """(input, name) => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([name], name, {type: 'text/markdown'}));
      input.files = transfer.files; input.dispatchEvent(new Event('change', {bubbles:true}));
    }""",
        name,
    )


async def recover_key(page: Page, key: str, labels: dict) -> None:
    """実際の入力と二重 click を通し、同 tick に一つの GET だけを開始する。"""
    help_panel = page.locator(".documentHelp")
    if await help_panel.get_attribute("open") is None:
        await help_panel.locator(":scope > summary").click()
    form = page.locator(".uploadRecovery")
    if await form.get_attribute("open") is None:
        await form.locator("summary").click()
    await form.get_by_label(labels["upload"]["recoveryKey"], exact=True).fill(key)
    await form.get_by_role("button", name=labels["upload"]["recover"], exact=True).evaluate(
        "button => { button.click(); button.click(); }"
    )


async def close_recovery(page: Page, labels: dict) -> None:
    """実際の keyboard 操作で読取だけを明示終了し、結果欄が消えることを確認する。"""
    result = page.locator(".documentUploadRecoveryResult")
    button = result.get_by_role("button", name=labels["upload"]["closeRecovery"], exact=True)
    await button.focus()
    await page.keyboard.press("Enter")
    await expect(result).to_have_count(0)


async def original_batch(page: Page) -> list[dict]:
    """原 file の表示・UUID・順序を保存し、人工 query による batch 置換を検出する。"""
    return await page.locator(".documentUploadItems > li").evaluate_all("""
    items => items.map(item => ({
      label: item.querySelector('strong').textContent,
      key: item.querySelector('input').value,
      phase: item.querySelector('p').textContent
    }))""")


async def manual_scenario(
    browser: Browser,
    url: str,
    mode: str,
    language: str,
    width: int,
    output: Path,
    late: str = "published",
) -> None:
    """人工 GET と送信済み POST を別の状態として、実 App の切替・保留を検証する。"""
    api = UploadReceiptsApi(url, language, mode, late)
    context = await browser.new_context(
        viewport={"width": width, "height": 1000},
        locale=language,
        service_workers="block",
    )
    await context.route("**/*", api.route)
    page = await context.new_page()
    audit = UploadBrowserAudit(page, api)
    # 通信取消を無視する応答も UI の所有権チェックで無害化することを検証する。
    await page.add_init_script("""(() => {
      const send = window.fetch.bind(window);
      window.fetch = (input, init) => String(input).includes('/document-uploads/')
        ? send(input, init ? {...init, signal: undefined} : init) : send(input, init);
    })();""")
    name = f"{mode}-{late}-{language}-{width}"
    project_id = ARCHIVED if mode == "manual-archived" else PROJECT
    try:
        await page.goto(f"{url}#/documents?project={project_id}")
        labels = (await messages(page, language))["documentsPanel"]
        texts = labels["upload"]
        panel = page.locator(".documentPanel")
        result = page.locator(".documentUploadRecoveryResult")
        await expect(panel.locator(".documentItem")).to_have_count(2)
        if mode.startswith("manual-kept-") or mode == "manual-unknown":
            await choose(
                page,
                *(
                    ("first.md", "second.md", "third.md")
                    if mode == "manual-unknown"
                    else ("first.md",)
                ),
            )
            phases = {
                "manual-kept-published": ["published"],
                "manual-kept-refused": ["refused"],
                "manual-unknown": ["unknown", "queued", "queued"],
            }[mode]
            await summary(page, phases, labels)
            before = await original_batch(page)
            assert len(api.uploads) == 1
            await expect(
                panel.get_by_role("button", name=texts["closeRecovery"], exact=True)
            ).to_have_count(0)
            key = api.uploads[0]["key"] if mode == "manual-unknown" else KEY
            for outcome, expected in (
                ("not-found", labels["failures"]["uploadNotFound"]),
                ("PENDING", texts["recoveryPending"]),
                ("unavailable", labels["failures"]["uploadUnavailable"]),
            ):
                api.manual_results[project_id, key] = outcome
                await recover_key(page, key, labels)
                await expect(result.get_by_text(expected, exact=True)).to_be_visible()
                assert PRIVATE not in await page.locator("body").inner_text()
                assert await original_batch(page) == before and len(api.uploads) == 1
                await close_recovery(page, labels)
                assert await original_batch(page) == before
            gate = ResponseGate()
            gate.failure = (401, "authentication_required")
            api.manual_gates[project_id, key] = gate
            await recover_key(page, key, labels)
            await asyncio.wait_for(gate.received.wait(), 5)
            await result.get_by_role("button", name=texts["cancelCheck"], exact=True).click()
            await expect(result.get_by_text(texts["recoveryCancelled"], exact=True)).to_be_visible()
            await close_recovery(page, labels)
            assert await original_batch(page) == before and len(api.uploads) == 1
            gate.release.set()
            await asyncio.wait_for(gate.returned.wait(), 5)
            await settle(page)
            await expect(page.locator('input[name="email"]')).to_have_count(0)
            del api.manual_gates[project_id, key]
            api.manual_results[project_id, key] = "PUBLISHED"
            await recover_key(page, key, labels)
            await expect(
                result.get_by_text(texts["phase"]["published"], exact=True)
            ).to_be_visible()
            assert await original_batch(page) == before
            await close_recovery(page, labels)
            assert await original_batch(page) == before and len(api.uploads) == 1
            if mode == "manual-unknown":
                await new_write_closed(page, labels)
                await expect(
                    panel.get_by_role("button", name=texts["closeRecovery"], exact=True)
                ).to_have_count(0)
                # 同じ key の人工 GET 成功では継続できず、原 batch の照合が別に必要。
                await panel.get_by_role("button", name=texts["checkOriginal"], exact=True).click()
                await summary(page, ["published", "queued", "queued"], labels)
                assert len(api.uploads) == 1
                await panel.get_by_role(
                    "button", name=texts["continueRemaining"], exact=True
                ).click()
                await summary(page, ["published", "published", "published"], labels)
                assert [item["name"] for item in api.uploads] == [
                    "first.md",
                    "second.md",
                    "third.md",
                ]
                assert [item["key"] for item in api.uploads] == [item["key"] for item in before]
                assert [item["body"] for item in api.uploads] == [
                    b"first.md",
                    b"second.md",
                    b"third.md",
                ]
            else:
                await expect(panel.locator('input[type="file"]').first).to_be_enabled()
        elif mode in ("manual-replace", "manual-switch"):
            gate = ResponseGate()
            if late == "401":
                gate.failure = (401, "authentication_required")
            api.manual_gates[project_id, KEY] = gate
            api.read_result = "PUBLISHED"
            await recover_key(page, KEY, labels)
            await asyncio.wait_for(gate.received.wait(), 5)
            assert len(api.reads) == 1
            if mode == "manual-switch":
                await menu(page)
                await page.locator(".sideNavProject select").select_option(NEXT_PROJECT)
                await expect(result).to_have_count(0)
                project_id = NEXT_PROJECT
                await expect(panel.locator(".documentItem")).to_have_count(2)
            await recover_key(page, NEXT_KEY, labels)
            await expect(result.get_by_label(texts["key"], exact=True)).to_have_value(NEXT_KEY)
            await expect(
                result.get_by_text(texts["phase"]["published"], exact=True)
            ).to_be_visible()
            gate.release.set()
            await asyncio.wait_for(gate.returned.wait(), 5)
            await settle(page)
            await expect(page.locator('input[name="email"]')).to_have_count(0)
            await expect(result.get_by_label(texts["key"], exact=True)).to_have_value(NEXT_KEY)
            await expect(result).to_contain_text("manual-2.md")
            await expect(result).not_to_contain_text("manual-1.md")
            assert api.reads == [
                (PROJECT, KEY, api.actor),
                (project_id, NEXT_KEY, api.actor),
            ]
            await summary(page, [], labels)
            await close_recovery(page, labels)
            await expect(panel.locator('input[type="file"]').first).to_be_enabled()
        else:
            for index, outcome in enumerate(("not-found", "PENDING", "unavailable")):
                key = KEY if index % 2 == 0 else NEXT_KEY
                api.manual_results[project_id, key] = outcome
                await recover_key(page, key, labels)
                expected = (
                    texts["recoveryPending"]
                    if outcome == "PENDING"
                    else labels["failures"][
                        "uploadNotFound" if outcome == "not-found" else "uploadUnavailable"
                    ]
                )
                await expect(result.get_by_text(expected, exact=True)).to_be_visible()
                await expect(result.get_by_label(texts["key"], exact=True)).to_have_value(key)
                await summary(page, [], labels)
                assert len(api.reads) == index + 1 and not api.uploads
                if outcome == "PENDING":
                    await page.screenshot(path=str(output / f"{name}-pending.png"), full_page=True)
                await close_recovery(page, labels)
            gate = ResponseGate()
            gate.failure = (401, "authentication_required")
            api.manual_gates[project_id, KEY] = gate
            api.manual_results[project_id, KEY] = "PUBLISHED"
            await recover_key(page, KEY, labels)
            await asyncio.wait_for(gate.received.wait(), 5)
            await panel.get_by_role("button", name=texts["cancelCheck"], exact=True).click()
            await expect(result.get_by_text(texts["recoveryCancelled"], exact=True)).to_be_visible()
            await close_recovery(page, labels)
            api.manual_results[project_id, NEXT_KEY] = "PUBLISHED"
            await recover_key(page, NEXT_KEY, labels)
            await expect(
                result.get_by_text(texts["phase"]["published"], exact=True)
            ).to_be_visible()
            gate.release.set()
            await asyncio.wait_for(gate.returned.wait(), 5)
            await settle(page)
            await expect(page.locator('input[name="email"]')).to_have_count(0)
            await expect(result.get_by_label(texts["key"], exact=True)).to_have_value(NEXT_KEY)
            await page.screenshot(path=str(output / f"{name}-published.png"), full_page=True)
            await close_recovery(page, labels)
            await summary(page, [], labels)
            assert len(api.reads) == 5 and not api.uploads
            for control in [
                panel.locator('input[type="file"]').first,
                *await panel.get_by_role(
                    "button",
                    name=labels["remove"],
                    exact=True,
                ).all(),
            ]:
                if mode == "manual-archived":
                    await expect(control).to_be_disabled()
                else:
                    await expect(control).to_be_enabled()
        await settle(page)
        if not mode.startswith("manual-kept-") and mode != "manual-unknown":
            assert not api.uploads
        assert not api.delete_calls and not api.exact_reads
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
        for gate in api.manual_gates.values():
            gate.release.set()
        await context.close()


async def scenario(
    browser: Browser,
    url: str,
    mode: str,
    language: str,
    width: int,
    output: Path,
    late: str = "401",
) -> None:
    """実 App を通し、独立した frame/HTTP ごとに未知と所有者の門禁を検査する。"""
    api = UploadReceiptsApi(url, language, mode, late)
    context = await browser.new_context(
        viewport={"width": width, "height": 1000},
        locale=language,
        service_workers="block",
    )
    await context.route("**/*", api.route)
    page = await context.new_page()
    audit = UploadBrowserAudit(page, api)
    await page.add_init_script("""(() => {
      const send = window.fetch.bind(window);
      window.fetch = (input, init) => send(input, init ? {...init, signal: undefined} : init);
      window.uploadDeadlineCallbacks = 0;
      const schedule = window.setTimeout.bind(window);
      window.setTimeout = (fn, delay, ...args) => schedule(() => {
        if (delay === 30000) window.uploadDeadlineCallbacks++;
        fn(...args);
      }, delay);
    })();""")
    name = f"{mode}-{late}-{language}-{width}"
    try:
        await page.goto(
            f"{url}#/documents?project={ARCHIVED if mode == 'archived-recovery' else PROJECT}"
        )
        labels = (await messages(page, language))["documentsPanel"]
        texts = labels["upload"]
        panel = page.locator(".documentPanel")
        state = page.locator(".documentUploadStatus")
        await expect(panel.locator(".documentItem")).to_have_count(2)
        if "timeout" in mode:
            await page.clock.install()
        if mode in (
            "recover-published",
            "archived-recovery",
            "bad-uuid",
            "wrong-actor",
        ):
            api.read_result = "PUBLISHED"
            await page.reload()
            await expect(panel.locator(".documentItem")).to_have_count(2)
            assert not api.reads and not api.uploads
            await recover_key(page, "00000000-0000-0000-0000-000000000000" if mode == "bad-uuid" else KEY, labels)
            if mode == "bad-uuid":
                await expect(
                    state.get_by_text(labels["failures"]["uploadInvalidKey"], exact=True)
                ).to_be_visible()
                assert not api.reads
                await expect(panel.locator('input[type="file"]').first).to_be_enabled()
            elif mode == "wrong-actor":
                await expect(
                    state.get_by_text(labels["failures"]["loadFailed"], exact=True)
                ).to_be_visible()
                await summary(page, [], labels)
                await expect(state.locator(".documentUploadRecoveryResult")).to_be_visible()
                await state.get_by_role("button", name=texts["closeRecovery"], exact=True).click()
                await expect(panel.locator('input[type="file"]').first).to_be_enabled()
            else:
                await summary(page, [], labels)
                result = state.locator(".documentUploadRecoveryResult")
                await expect(
                    result.get_by_text(texts["phase"]["published"], exact=True)
                ).to_be_visible()
                assert not api.exact_reads and len(api.reads) == 1
                await result.get_by_role("button", name=texts["closeRecovery"], exact=True).click()
                if mode == "archived-recovery":
                    await expect(panel.locator('input[type="file"]').first).to_be_disabled()
                else:
                    await expect(panel.locator('input[type="file"]').first).to_be_enabled()
            assert not api.uploads
        elif mode == "delete-first":
            await delete_first(page, labels)
            await expect(panel.get_by_text(labels["unknownTitle"], exact=True)).to_be_visible()
            await inject_selection(page, "must-not-send.md")
            await expect(state.locator(".documentUploadItems > li")).to_have_count(0)
            assert not api.uploads and len(api.delete_calls) == 1
        else:
            if mode == "same-tick":
                await page.evaluate("""() => {
                  const input = document.querySelector('.documentPanel input[type=file]');
                  for (const name of ['first.md', 'must-not-send.md']) {
                    const transfer = new DataTransfer();
                    transfer.items.add(new File([name], name, {type:'text/markdown'}));
                    input.files = transfer.files;
                    input.dispatchEvent(new Event('change', {bubbles:true}));
                  }
                  document.querySelector('.documentItem button:last-child').click();
                }""")
            else:
                await choose(
                    page,
                    *(
                        ("first.md", "second.md", "third.md")
                        if mode.startswith("sequence-") or mode in ("pending", "key-conflict")
                        else ("first.md",)
                    ),
                )
            if mode == "sequence-known":
                await summary(page, ["published", "refused", "published"], labels)
                assert len(api.uploads) == 3
                await expect(panel.locator('input[type="file"]').first).to_be_enabled()
            elif mode in ("sequence-unknown", "pending", "key-conflict"):
                await summary(page, ["published", "unknown", "queued"], labels)
                await expect(
                    state.get_by_role("button", name=texts["closeRecovery"], exact=True)
                ).to_have_count(0)
                await new_write_closed(page, labels)
                await page.screenshot(path=str(output / f"{name}-paused.png"), full_page=True)
                before = [item["key"] for item in api.uploads]
                await inject_selection(page, "must-not-replace.md")
                await panel.get_by_role("button", name=labels["refresh"], exact=True).click()
                await settle(page)
                assert len(api.uploads) == 2 and not api.reads
                for result in (
                    "not-found",
                    "PENDING",
                    "unavailable",
                    "wrong-size",
                    "PUBLISHED",
                ):
                    api.read_result = result
                    await state.get_by_role(
                        "button", name=texts["checkOriginal"], exact=True
                    ).click()
                    if result == "PUBLISHED":
                        await summary(page, ["published", "published", "queued"], labels)
                    else:
                        expected = (
                            texts["pending"]
                            if result == "PENDING"
                            else labels["failures"][
                                {
                                    "not-found": "uploadNotFound",
                                    "unavailable": "uploadUnavailable",
                                    "wrong-size": "loadFailed",
                                }[result]
                            ]
                        )
                        await expect(state.get_by_text(expected, exact=True)).to_be_visible()
                        await summary(page, ["published", "unknown", "queued"], labels)
                    await new_write_closed(page, labels)
                    await expect(
                        state.get_by_role("button", name=texts["closeRecovery"], exact=True)
                    ).to_have_count(0)
                    assert [item["key"] for item in api.uploads] == before
                assert all(read[1] == before[1] for read in api.reads) and not api.exact_reads
                await page.screenshot(path=str(output / f"{name}-confirmed.png"), full_page=True)
                await state.get_by_role(
                    "button", name=texts["continueRemaining"], exact=True
                ).evaluate("e => {e.click();e.click()}")
                await summary(page, ["published", "published", "published"], labels)
                assert len(api.uploads) == 3 and api.uploads[2]["name"] == "third.md"
            elif mode in POST_GATES or mode == "same-tick":
                await asyncio.wait_for(api.gate.received.wait(), 5)
                if mode in ("post-cancel", "same-tick"):
                    await state.get_by_role(
                        "button", name=texts["cancelUpload"], exact=True
                    ).click()
                elif mode == "post-timeout":
                    await page.clock.run_for(30_001)
                elif mode == "post-deadline":
                    await page.evaluate(
                        "() => { const late = performance.now()+31000; performance.now=()=>late; }"
                    )
                elif mode == "post-switch":
                    await menu(page)
                    await page.locator(".sideNavProject select").select_option(NEXT_PROJECT)
                    await expect(state.locator(".documentUploadItems > li")).to_have_count(0)
                else:
                    await page.evaluate("location.hash='/accounts'")
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
                if mode not in ("post-switch", "post-actor"):
                    await summary(page, ["unknown"], labels)
                    await new_write_closed(page, labels)
                    await expect(
                        state.get_by_role("button", name=texts["closeRecovery"], exact=True)
                    ).to_have_count(0)
                if mode == "post-deadline":
                    assert await page.evaluate("window.uploadDeadlineCallbacks") == 0
                assert len(api.uploads) == 1 and not api.delete_calls
            else:
                await summary(page, ["unknown"], labels)
                original_key = api.uploads[0]["key"]
                if mode == "recover-reload":
                    await page.reload()
                    await expect(state.locator(".documentUploadItems > li")).to_have_count(0)
                    assert not api.reads
                    api.read_result = "PUBLISHED"
                    await recover_key(page, original_key, labels)
                    await summary(page, [], labels)
                    await expect(
                        state.locator(".documentUploadRecoveryResult").get_by_text(
                            texts["phase"]["published"],
                            exact=True,
                        )
                    ).to_be_visible()
                    assert len(api.uploads) == len(api.reads) == 1
                else:
                    api.read_result = {
                        "read-denied": "denied",
                        "read-expired": "expired",
                        "read-project-missing": "project-missing",
                    }.get(mode, "PUBLISHED")
                    await state.get_by_role(
                        "button", name=texts["checkOriginal"], exact=True
                    ).click()
                    if mode in READ_GATES:
                        await asyncio.wait_for(api.gate.received.wait(), 5)
                        if mode == "read-cancel":
                            await state.get_by_role(
                                "button", name=texts["cancelCheck"], exact=True
                            ).click()
                        elif mode == "read-timeout":
                            await page.clock.run_for(30_001)
                        elif mode == "read-deadline":
                            await page.evaluate(
                                "() => {const late=performance.now()+31000;"
                                "performance.now=()=>late}"
                            )
                        else:
                            await menu(page)
                            await page.locator(".sideNavProject select").select_option(NEXT_PROJECT)
                            await expect(state.locator(".documentUploadItems > li")).to_have_count(
                                0
                            )
                        api.gate.release.set()
                        await asyncio.wait_for(api.gate.returned.wait(), 5)
                        await settle(page)
                        await expect(page.locator('input[name="email"]')).to_have_count(0)
                        if mode != "read-switch":
                            await summary(page, ["unknown"], labels)
                            await new_write_closed(page, labels)
                        if mode in ("read-timeout", "read-deadline"):
                            await expect(
                                state.get_by_text(labels["failures"]["loadFailed"], exact=True)
                            ).to_be_visible()
                        if mode == "read-deadline":
                            assert await page.evaluate("window.uploadDeadlineCallbacks") == 0
                    elif mode == "read-expired":
                        await expect(page.locator('input[name="email"]')).to_be_visible()
                    else:
                        await expect(
                            panel.get_by_text(labels["failures"]["denied"], exact=True)
                        ).to_be_visible()
                        await panel.get_by_role(
                            "button", name=labels["refresh"], exact=True
                        ).click()
                        await settle(page)
                        await new_write_closed(page, labels)
                    assert len(api.uploads) == 1
        await settle(page)
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
        api.gate.release.set()
        await context.close()


async def check(url: str, output: Path) -> None:
    """全 case を独立 context で確認し、自分の browser だけを終了する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ("zh", "ja", "en"):
                for mode in ("sequence-known", "sequence-unknown", "recover-published"):
                    await scenario(browser, url, mode, language, 390, output)
            await scenario(browser, url, "sequence-unknown", "en", 1440, output)
            for mode in (*POST_GATES, *READ_GATES):
                for late in ("401", "published"):
                    await scenario(browser, url, mode, "zh", 390, output, late)
            for mode in (
                "pending",
                "key-conflict",
                "read-denied",
                "read-expired",
                "read-project-missing",
                "same-tick",
                "delete-first",
                "archived-recovery",
                "bad-uuid",
                "wrong-actor",
                "recover-reload",
            ):
                await scenario(browser, url, mode, "zh", 390, output)
            for language in ("zh", "ja", "en"):
                for width in (390, 1440):
                    for mode in ("manual-outcomes", "manual-archived"):
                        await manual_scenario(browser, url, mode, language, width, output)
            for mode in ("manual-replace", "manual-switch"):
                for late in ("401", "published"):
                    await manual_scenario(browser, url, mode, "zh", 390, output, late)
            for mode in (
                "manual-kept-published",
                "manual-kept-refused",
                "manual-unknown",
            ):
                await manual_scenario(browser, url, mode, "zh", 390, output)
        finally:
            await browser.close()


def main() -> None:
    """明示した loopback mock harness だけを許可し、実 API と credential は使わない。"""
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
        parser.error("Use an explicitly started loopback mock projects harness")
    asyncio.run(check(args.url, args.output))


if __name__ == "__main__":
    main()
