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
        self.publishes: list[dict] = []
        self.reject_publish = False

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
        if route.request.method == "POST" and path.endswith("/publish"):
            assert route.request.headers.get("x-csrf-token") == CSRF
            self.publishes.append(route.request.post_data_json)
            if self.reject_publish:
                await route.fulfill(status=503, json={"title": "Unavailable", "status": 503})
            else:
                version = next(v for v in self.versions if v["skill_version_id"] == path.split("/")[1])
                version.update(status="PUBLISHED", published_at="2026-09-14T00:00:00Z")
                await route.fulfill(json=version)
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
            await check_draft_publication(browser, url, output)
            await check_library_filters(browser, url, output)
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
                    original = rows.filter(has=page.locator(".skillLibraryIdentity strong", has_text=f"v{VERSION['version']}"))
                    other = rows.filter(has=page.locator(".skillLibraryIdentity strong", has_text="v0.2.0"))
                    await expect(rows).to_have_count(2)
                    for succeeds in (False, True):
                        api.reject_delete = not succeeds
                        await original.locator('button[aria-haspopup="menu"]').click()
                        await page.get_by_role(
                            "menuitem", name=labels["skills"]["deleteVersion"], exact=True
                        ).click()
                        assert len(api.deletes) == int(succeeds)
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
                            await expect(original).to_contain_text(
                                labels["skills"]["deleteBlocked"]
                            )
                            await expect(
                                other.locator('button[aria-haspopup="menu"]')
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
                                original.locator('button[aria-haspopup="menu"]')
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


async def check_draft_publication(browser, url: str, output: Path) -> None:
    """再読込後の草稿から gate・警告同意・失敗回復を経て発行できることを確認する。"""
    for language in ("ja", "zh", "en"):
        api = LibraryApi(url, language)
        api.versions[1]["gate_passed"] = False
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        await context.route("**/*", api.route)
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e, captured=errors: captured.append(str(e)))
        try:
            await page.goto(f"{url}#/skills?project={PROJECT}")
            labels = await messages(page, language)
            await page.reload()
            draft = page.locator(".skillLibraryList > li").filter(has=page.locator(".skillLibraryIdentity strong", has_text="v0.2.0"))
            await expect(draft.locator(".skillLibraryDraft > summary")).to_have_text(labels["skills"]["reviewDraft"])
            await draft.locator(".skillLibraryDraft > summary").click()
            publish = draft.get_by_role("button", name=labels["skills"]["publishVersion"], exact=True)
            await expect(publish).to_be_disabled()
            assert not api.publishes
            api.versions[1]["gate_passed"] = True
            await page.reload()
            await draft.locator(".skillLibraryDraft > summary").click()
            await publish.click()
            confirmation = page.get_by_role("dialog", name=labels["skills"]["publishVersion"], exact=True)
            await expect(confirmation).to_be_visible()
            await confirmation.get_by_role("button", name=labels["elements"]["close"], exact=True).click()
            await expect(publish).to_be_enabled()
            assert not api.publishes
            api.reject_publish = True
            await publish.click()
            await confirmation.get_by_role("button", name=labels["skills"]["publishVersion"], exact=True).click()
            await expect(draft.locator('[role="alert"]')).to_be_visible()
            await expect(publish).to_be_enabled()
            await expect(page.locator(".skillLibraryList > li")).to_have_count(2)
            for width in (1366, 1920, 390):
                await page.set_viewport_size({"width": width, "height": 900})
                await layout(page)
            await page.set_viewport_size({"width": 1440, "height": 900})
            await page.screenshot(path=str(output / f"draft-review-{language}.png"))
            api.reject_publish = False
            await publish.click()
            await confirmation.get_by_role("button", name=labels["skills"]["publishVersion"], exact=True).click()
            await expect(draft.locator(".skillLibraryDraft")).to_have_count(0)
            await expect(draft.get_by_role("button", name=labels["skills"]["enableForProject"], exact=True)).to_be_enabled()
            assert api.publishes == [{"accepted_warnings": ["assisted_review_required"]}] * 2
            assert not api.deletes and not errors and not api.failures and not api.unexpected
            print(f"PASS draft publication {language}", flush=True)
        finally:
            await context.close()


async def check_library_filters(browser, url: str, output: Path) -> None:
    """公開版の優先表示・検索・空態からの解除を実 UI の三語で確認する。"""
    for language in ("ja", "zh", "en"):
        api = LibraryApi(url, language)
        api.versions[1]["status"] = "PUBLISHED"
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        await context.route("**/*", api.route)
        page = await context.new_page()
        try:
            await page.goto(f"{url}#/skills?project={PROJECT}")
            labels = (await messages(page, language))["skills"]
            rows = page.locator(".skillLibraryList > li")
            await expect(rows).to_have_count(2)
            await expect(rows.first).to_contain_text("v0.2.0")
            search = page.get_by_role("searchbox", name=labels["librarySearch"], exact=True)
            status = page.get_by_role("combobox", name=labels["libraryStatus"], exact=True)
            await search.fill("  " + api.versions[0]["skill_key"].upper() + "  ")
            await expect(rows).to_have_count(2)
            await status.select_option("DEPRECATED")
            await expect(rows).to_have_count(1)
            await expect(rows).to_contain_text(f"v{VERSION['version']}")
            await page.get_by_role("tab", name=labels["tabWorkbench"], exact=True).click()
            await page.get_by_role("tab", name=labels["libraryTitle"]).click()
            await expect(rows).to_have_count(1)
            await search.fill("no-matching-synthetic-skill")
            await expect(rows).to_have_count(0)
            await expect(page.get_by_text(labels["libraryNoMatches"], exact=True)).to_be_visible()
            await page.get_by_role("button", name=labels["libraryClearFilters"], exact=True).click()
            await expect(rows).to_have_count(2)
            await expect(search).to_have_value("")
            await expect(status).to_have_value("all")
            for width in (1440, 390):
                await page.set_viewport_size({"width": width, "height": 1000})
                for theme in ("light", "dark"):
                    await page.evaluate("value => document.documentElement.dataset.theme = value", theme)
                    await layout(page)
                    await page.screenshot(path=str(output / f"filter-{language}-{width}-{theme}.png"))
            assert not api.deletes and not api.publishes and not api.unexpected and not api.failures
            print(f"PASS skill library filters {language}", flush=True)
        finally:
            await context.close()


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
