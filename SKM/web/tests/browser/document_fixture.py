"""文書の架空 API と行 menu・選択操作を各ブラウザ検証で共有する。"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from account_fixture import (
    ACTOR,
    CSRF,
    NEXT_PROJECT,
    PROJECT,
    ResponseGate,
)
from check_projects import ARCHIVED, ProjectsApi
from playwright.async_api import Locator, Page, Route, expect

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
        if (
            len(parts) == 3 and parts[0] == "projects" and parts[2] == "document-folders"
            and request.method == "GET" and parts[1] in self.rows
            and f"{address.scheme}://{address.netloc}" == self.origin
        ):
            # 整理 UI が読む目录も、同じ fixture 文書から投影して外部へ出さない。
            await route.fulfill(json={
                "folders": sorted({row["folder"] for row in self.rows[parts[1]] if row["folder"]}),
            })
            return
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


async def open_ancestor_folders(row: Locator) -> None:
    """既定で閉じた文書目录を外側から native summary で開く。"""
    for folder in await row.locator('xpath=ancestor::details[@class="docFolder"]').all():
        if await folder.get_attribute("open") is None:
            await folder.locator(':scope > summary > strong').click()


async def row_menu(page: Page, row: Locator) -> Locator:
    """対象行の実入口を開き、Portal に描画された同じ名前の menu を取得する。"""
    if "documentItem" in (await row.get_attribute("class") or "").split():
        await open_ancestor_folders(row)
    trigger = row.locator('button[aria-haspopup="menu"]')
    label = await trigger.get_attribute("aria-label")
    assert label
    await trigger.click()
    result = page.get_by_role("menu", name=label, exact=True)
    await expect(result).to_be_visible()
    return result


async def confirm_first_document_action(page: Page, labels: dict) -> None:
    """行 menu の同 tick 二重 click でも原 ID 一件だけを確認対象にする。"""
    await expect(page.locator(".documentItem")).to_have_count(2)
    actions = await row_menu(page, page.locator(".documentItem").first)
    action = actions.get_by_role("menuitem").last
    label = await action.inner_text()
    await action.evaluate(
        "button => { button.click(); button.click(); }"
    )
    dialog = page.get_by_role("dialog")
    await expect(dialog).to_be_visible()
    confirm = dialog.get_by_role("button", name=label, exact=True)
    await confirm.focus()
    await page.keyboard.press("Enter")


async def assert_document_writes(page: Page, *, disabled: bool) -> None:
    """メニューを実際に開き、空 locator を成功扱いせず各行の変更門禁を確認する。"""
    rows = page.locator(".documentItem")
    assert await rows.count() > 0
    for row in await rows.all():
        actions = await row_menu(page, row)
        action = actions.get_by_role("menuitem").last
        if disabled:
            await expect(action).to_be_disabled()
        else:
            await expect(action).to_be_enabled()
        await page.keyboard.press("Escape")
