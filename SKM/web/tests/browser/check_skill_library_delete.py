"""削除拒否・成功後の再読失敗でも Skill 一覧を保ち、原 DELETE を再送しない。"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from check_accounts import CSRF
from check_projects import PROJECT, ProjectsApi, layout, messages
from playwright.async_api import Route, async_playwright, expect

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
VERSION = json.loads((CONTRACTS / "examples/skill-version.v1.json").read_text())


class LibraryApi(ProjectsApi):
    """二つの架空版について削除結果と一覧の再読を独立に制御する。"""

    def __init__(self, url: str, language: str) -> None:
        """削除される版と残る版を独立な fixture として用意する。"""
        super().__init__(url, language)
        self.versions = [
            {**deepcopy(VERSION), "status": "DEPRECATED"},
            {
                **deepcopy(VERSION),
                "skill_version_id": "00000000-0000-4000-8000-000000000199",
                "version": "0.2.0",
            },
        ]
        self.deletes: list[str] = []
        self.reject_delete = True
        self.reject_read = False

    async def respond(self, route: Route) -> None:
        """元資格を検証し、409 と読取 503 を UI に返す。実データは使わない。"""
        path = urlsplit(route.request.url).path.removeprefix(self.prefix)
        if route.request.method == "GET" and path == "skill-versions":
            if self.reject_read:
                await route.fulfill(
                    status=503, json={"title": "Unavailable", "status": 503, "code": "unavailable"}
                )
            else:
                await route.fulfill(json={"skill_versions": self.versions})
            return
        if route.request.method == "GET" and path == f"projects/{PROJECT}/skill-versions":
            await route.fulfill(json={"skill_versions": []})
            return
        if route.request.method == "DELETE" and path.startswith("skill-versions/"):
            assert route.request.headers.get("x-csrf-token") == CSRF
            self.deletes.append(path)
            if self.reject_delete:
                await route.fulfill(
                    status=409,
                    json={
                        "title": "Referenced",
                        "status": 409,
                        "code": "skill_version_delete_blocked",
                    },
                )
            else:
                self.versions = [
                    v for v in self.versions if v["skill_version_id"] != path.split("/")[-1]
                ]
                self.reject_read = True
                await route.fulfill(status=204)
            return
        await super().respond(route)


async def check(url: str, output: Path) -> None:
    """実 App で拒否後の全行保持・成功後の読取再試行を三語で確認する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            for language in ("ja", "zh", "en"):
                api = LibraryApi(url, language)
                context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                await context.route("**/*", api.route)
                page = await context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda e, captured=errors: captured.append(str(e)))
                try:
                    await page.goto(f"{url}#/skills?project={PROJECT}")
                    labels = await messages(page, language)
                    rows = page.locator(".skillLibraryList > li")
                    await expect(rows).to_have_count(2)
                    for succeeds in (False, True):
                        api.reject_delete = not succeeds
                        await rows.first.get_by_role(
                            "button", name=labels["skills"]["deleteVersion"], exact=True
                        ).click()
                        await (
                            page.get_by_role("dialog")
                            .get_by_role(
                                "button", name=labels["skills"]["deleteVersion"], exact=True
                            )
                            .click()
                        )
                        await expect(page.locator('.skillLibrary [role="alert"]')).to_be_visible()
                        await expect(rows).to_have_count(2)
                        if not succeeds:
                            await expect(rows.first).to_contain_text(
                                labels["skills"]["deleteBlocked"]
                            )
                            await expect(
                                rows.last.get_by_role(
                                    "button", name=labels["skills"]["deleteVersion"], exact=True
                                )
                            ).to_be_enabled()
                            for width in (1366, 1920, 390):
                                await page.set_viewport_size({"width": width, "height": 1000})
                                await layout(page)
                            await page.set_viewport_size({"width": 1440, "height": 1000})
                            await page.screenshot(
                                path=str(output / f"delete-blocked-{language}.png")
                            )
                        else:
                            await expect(
                                rows.first.get_by_role(
                                    "button", name=labels["skills"]["deleteVersion"], exact=True
                                )
                            ).to_be_disabled()
                    assert len(api.deletes) == 2
                    api.reject_read = False
                    await (
                        page.locator(".skillLibrary")
                        .get_by_role("button", name=labels["runHistory"]["retry"], exact=True)
                        .click()
                    )
                    await expect(rows).to_have_count(1)
                    await expect(rows.first).to_contain_text("v0.2.0")
                    assert len(api.deletes) == 2
                    assert not errors and not api.failures and not api.unexpected, (
                        errors,
                        api.failures,
                        api.unexpected,
                    )
                    print(f"PASS skill deletion {language}", flush=True)
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
    if (
        target.scheme != "http"
        or target.hostname not in {"127.0.0.1", "localhost"}
        or not target.path.endswith("/tests/browser/projects.html")
    ):
        parser.error("Only loopback projects.html is allowed")
    asyncio.run(check(args.url, args.output))
