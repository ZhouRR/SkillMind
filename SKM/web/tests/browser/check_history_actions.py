"""履歴の補助メニューが元 Run の確認・回収・復元・完全削除を保つことを確認する。"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from check_accounts import CSRF
from check_projects import PROJECT, RUN, layout, messages
from check_workspace_reports import CONTRACTS, WorkspaceApi
from playwright.async_api import Route, async_playwright, expect


class HistoryApi(WorkspaceApi):
    """一件の架空 Run だけを状態変更し、元 ID・CSRF・成果物選択を厳密に検査する。"""

    def __init__(self, url: str, language: str) -> None:
        """実操作を持たない完了 Run と独立した回収状態を用意する。"""
        super().__init__(url, language)
        self.deleted = False
        self.purged = False
        self.running = False
        self.actions: list[tuple[str, str, object]] = []
        self.previews = 0

    def preview(self) -> dict:
        """文書本文を含まない合法な空成果一覧を返す。"""
        return {"run_id": RUN, "deleted": self.deleted, "cleanup_pending": 0,
                "output_count": 0, "protected_output_count": 0, "outputs": []}

    async def respond(self, route: Route) -> None:
        """既知の履歴 API 以外は親の deny-by-default handler へ渡す。"""
        request = route.request
        address = urlsplit(request.url)
        suffix = address.path.removeprefix(self.prefix)
        base = f"projects/{PROJECT}/runs"
        if suffix == base and request.method == "GET":
            query = parse_qs(address.query)
            template = json.loads((CONTRACTS / "examples/run-history.v1.json").read_text())
            row = {**template["items"][0], "run_id": RUN, "project_id": PROJECT,
                   "status": "RUNNING" if self.running else "SUCCEEDED"}
            in_trash = query.get("trashed") == ["true"]
            visible = not self.purged and in_trash == self.deleted
            await route.fulfill(json={**template, "items": [row] if visible else [],
                "offset": int(query.get("offset", ["0"])[0]),
                "limit": int(query["limit"][0]), "has_more": False})
            return
        if suffix == f"{base}/{RUN}/deletion-preview" and request.method == "GET":
            self.previews += 1
            await route.fulfill(json=self.preview())
            return
        if suffix in {f"{base}/{RUN}", f"{base}/{RUN}/restore", f"{base}/{RUN}/purge"} and request.method in {"POST", "DELETE"}:
            assert not self.running and not self.purged
            assert request.headers.get("x-csrf-token") == CSRF
            assert request.headers.get("origin") == self.origin
            body = request.post_data_json if request.post_data else None
            if suffix.endswith("/restore"):
                assert request.method == "POST" and body is None and self.deleted
                self.deleted = False
            else:
                assert request.method == "DELETE" and body == {"include_outputs": True}
                if suffix.endswith("/purge"):
                    assert self.deleted
                    self.purged = True
                self.deleted = True
            self.actions.append((request.method, suffix, body))
            await route.fulfill(json=self.preview())
            return
        await super().respond(route)


async def check(url: str, output: Path) -> None:
    """三語・狭幅で開くだけの無変更、稼働中の無効化、元確認を経る mutation を検証する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            for language in ("ja", "zh", "en"):
                api = HistoryApi(url, language)
                context = await browser.new_context(viewport={"width": 390, "height": 900})
                await context.route("**/*", api.route)
                page = await context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))
                try:
                    await page.goto(f"{url}#/history?project={PROJECT}")
                    labels = (await messages(page, language))["fileManagement"]
                    row = page.locator(".historyItem")
                    trigger = row.locator('button[aria-haspopup="menu"]')
                    await expect(row).to_have_count(1)
                    await trigger.click()
                    await expect(page.get_by_role("menuitem", name=labels["trashAction"], exact=True)).to_be_visible()
                    assert not api.actions and api.previews == 0
                    await layout(page)
                    await page.screenshot(path=str(output / f"history-menu-{language}.png"))
                    await page.keyboard.press("Escape")
                    await expect(trigger).to_be_focused()
                    api.running = True
                    await page.reload()
                    await trigger.click()
                    await expect(page.get_by_role("menuitem", name=labels["trashAction"], exact=True)).to_be_disabled()
                    await page.keyboard.press("Escape")
                    assert not api.actions and api.previews == 0
                    api.running = False
                    await page.reload()
                    # 回収→復元→再回収→完全削除。各項目は元確認を開くだけで mutation しない。
                    for action, tab in (("trashAction", None), ("restore", "trash"),
                                        ("trashAction", "active"), ("purge", "trash")):
                        if tab:
                            await page.get_by_role("button", name=labels[tab], exact=True).click()
                        await expect(row).to_have_count(1)
                        count = len(api.actions)
                        await trigger.click()
                        await page.get_by_role("menuitem", name=labels[action], exact=True).click()
                        dialog = page.get_by_role("dialog", name=labels[action], exact=True)
                        confirm = dialog.get_by_role("button", name=labels[action], exact=True)
                        await expect(confirm).to_be_enabled()
                        assert len(api.actions) == count
                        await confirm.click()
                        await expect(dialog).to_have_count(0)
                        await expect(row).to_have_count(0)
                        assert len(api.actions) == count + 1
                    assert len(api.actions) == 4 and api.purged
                    assert not errors and not api.failures and not api.unexpected, (errors, api.failures, api.unexpected)
                    print(f"PASS history action menu {language}", flush=True)
                finally:
                    await context.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    target = urlsplit(arguments.url)
    if target.scheme != "http" or target.hostname not in {"localhost", "127.0.0.1"} or not target.path.endswith("/tests/browser/projects.html"):
        parser.error("Only the loopback App mock harness is allowed")
    asyncio.run(check(arguments.url, arguments.output))
