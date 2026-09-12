"""実 App で原要求の応答喪失・SSE 切断・再読を全面 mock HTTP で検証する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from check_accounts import CSRF
from check_projects import PROJECT, ProjectsApi, messages, settle
from playwright.async_api import Route, async_playwright, expect

SOURCE = "00000000-0000-4000-8000-000000000040"
RESULT = "00000000-0000-4000-8000-000000000050"
KEY = "sha256:" + "a" * 64
ADJUSTMENT = "Clarify the synthetic review criteria."
RECEIPT = "skillmind:interpretation-request:v1"
PREVIEW = {
    "normalized_package": {
        "package_format": "skillmind.normalized/v1",
        "source": {
            "type": "directory",
            "content_hash": KEY,
            "detected_adapter": "directory-skill/v1",
            "files": [],
        },
        "metadata": {
            "name": "Synthetic Skill",
            "description": "Browser fixture",
            "argument_hint": None,
        },
        "resources": {"scripts": [], "references": [], "assets": []},
        "declared_tools": [],
        "diagnostics": [],
    },
    "runtime_manifest_draft": {
        "identity": {
            "skill_key": "synthetic",
            "source_hash": KEY,
            "interpreter_version": "synthetic/v1",
        },
        "compatibility": {"level": "assisted", "confidence": 0.5, "diagnostics": []},
        "tools": [],
        "extensions": {},
    },
    "capability_blueprint": None,
}
STORED = {
    "skill_source_id": SOURCE,
    "interpretation_id": RESULT,
    "organization_id": "00000000-0000-4000-8000-000000000002",
    "name": "Synthetic Skill",
    "source_hash": KEY,
    "source_type": "directory",
    "interpretation_status": "PREVIEW_READY",
    "compatibility_level": "assisted",
    "confidence": 0.5,
    "interpreter_version": "synthetic/v1",
    "created_at": "2026-09-11T00:00:00Z",
    "preview": PREVIEW,
}


class InterpretationApi(ProjectsApi):
    """実行 POST と原 UUID の GET を別々に観測し、未知時の再送を拒否する。"""

    def __init__(self, url: str, language: str, mode: str) -> None:
        """外部 model/DB を使わず、受理の後に応答だけを落とす。"""
        super().__init__(url, language)
        self.mode = mode
        self.original: str | None = None
        self.posts = 0
        self.adjustments = 0
        self.reads: list[str] = []
        self.state = "UNKNOWN"

    async def respond(self, route: Route) -> None:
        """Skill endpoint だけを拡張し、他の通信は既存の遮断 handler を通す。"""
        request = route.request
        address = urlsplit(request.url)
        suffix = address.path.removeprefix(self.prefix)
        if f"{address.scheme}://{address.netloc}" != self.origin:
            await super().respond(route)
            return
        if suffix == "skill-versions" or suffix == f"projects/{PROJECT}/skill-versions":
            assert request.method == "GET"
            await route.fulfill(json={"items": []})
        elif suffix == "skill-imports/upload":
            assert request.method == "POST"
            await route.fulfill(status=201, json=STORED)
        elif suffix == f"skill-sources/{SOURCE}/interpretation-requests":
            assert request.method == "POST" and self.posts == 0
            assert request.headers.get("x-csrf-token") == CSRF
            body = request.post_data_json
            assert set(body) == {"request_id", "force_regenerate"}
            assert body["force_regenerate"] is False
            assert UUID(body["request_id"]).int != 0 and UUID(body["request_id"]).version == 4
            self.original = body["request_id"]
            self.posts += 1
            if self.mode == "lost-response":
                await route.abort("failed")
            else:
                await route.fulfill(json=self.receipt("RUNNING"))
        elif suffix == f"skill-interpretations/{RESULT}/adjustment-requests":
            assert request.method == "POST" and self.adjustments == 0
            assert request.headers.get("x-csrf-token") == CSRF
            body = request.post_data_json
            assert set(body) == {"request_id", "instruction"}
            assert body["instruction"] == ADJUSTMENT
            assert UUID(body["request_id"]).int != 0 and UUID(body["request_id"]).version == 4
            assert body["request_id"] != self.original
            self.original = body["request_id"]
            self.adjustments += 1
            self.state = "UNKNOWN"
            await route.abort("failed")
        elif suffix == f"skill-interpretation-requests/{self.original}/events":
            assert request.method == "GET" and not address.query
            event = {
                "event": "interpret.disconnected",
                "execution_key": KEY,
                "occurred_at": STORED["created_at"],
                "data": {},
            }
            await route.fulfill(
                content_type="text/event-stream",
                body=("event: interpret.disconnected\ndata: " + json.dumps(event) + "\n\n"),
            )
        elif suffix == f"skill-interpretation-requests/{self.original}":
            assert request.method == "GET" and not request.post_data and not address.query
            self.reads.append(self.original)
            await route.fulfill(json=self.receipt(self.state))
        elif suffix == f"skill-interpretations/{RESULT}/execution":
            assert request.method == "GET" and self.state in {"SUCCEEDED", "FAILED"}
            await route.fulfill(
                json={
                    **STORED,
                    "status": "FAILED" if self.state == "FAILED" else "PREVIEW_READY",
                    "origin": "MODEL",
                    "model": "synthetic-model",
                    "execution_key": KEY,
                    "error_code": "provider_error" if self.state == "FAILED" else None,
                    "summary": "Synthetic result",
                    "report": None,
                    "reused": False,
                    "parent_interpretation_id": None,
                    "adjustment": None,
                    "diff": {"has_changes": False},
                }
            )
        else:
            await super().respond(route)

    def receipt(self, state: str) -> dict:
        """公開 metadata だけを返し、credential や model 入力を含めない。"""
        return {
            "request_id": self.original,
            "skill_source_id": SOURCE,
            "status": state,
            "execution_key": KEY,
            "interpretation_id": RESULT if state in {"SUCCEEDED", "FAILED"} else None,
            "error_code": "provider_error" if state == "FAILED" else None,
        }


async def check(url: str, output: Path) -> None:
    """randomUUID 非公開でも三語の解釈/調整が一度だけ送信され、原 GET で復元する。"""
    output.mkdir(parents=True, exist_ok=True)
    fixture = output / "synthetic-skill"
    fixture.mkdir(exist_ok=True)
    (fixture / "SKILL.md").write_text("# Synthetic Skill\n")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ("zh", "ja", "en"):
                for width, mode in (
                    (390, "lost-response"),
                    (1440, "sse-disconnected"),
                    (1440, "failed-result"),
                ):
                    context = await browser.new_context(viewport={"width": width, "height": 900})
                    # HTTP origin と同じ API 欠落を再現し、getRandomValues は実 browser を使う。
                    await context.add_init_script(
                        "Object.defineProperty(globalThis.crypto, 'randomUUID', "
                        "{ value: undefined, configurable: true })"
                    )
                    api = InterpretationApi(url, language, mode)
                    await context.route("**/*", api.route)
                    page = await context.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))
                    name = f"{language}-{width}-{mode}"
                    try:
                        await page.goto(f"{url}#/skills?project={PROJECT}")
                        assert await page.evaluate(
                            "typeof crypto.randomUUID === 'undefined' "
                            "&& typeof crypto.getRandomValues === 'function'"
                        )
                        labels = (await messages(page, language))["skills"]
                        await page.get_by_role(
                            "tab", name=labels["tabWorkbench"], exact=True
                        ).click()
                        await page.locator('input[type="file"]').set_input_files(str(fixture))
                        await page.get_by_role(
                            "button", name=labels["interpretAction"], exact=True
                        ).click()
                        confirm = page.get_by_role(
                            "button", name=labels["confirmInterpretation"], exact=True
                        )
                        await expect(confirm).to_be_visible()
                        assert api.posts == 1 and api.original is not None
                        assert (
                            await page.evaluate("key => sessionStorage.getItem(key)", RECEIPT)
                            == api.original
                        )
                        await confirm.click()
                        await expect(confirm).to_be_visible()
                        assert api.reads and all(value == api.original for value in api.reads)
                        await page.reload()
                        await expect(confirm).to_be_visible()
                        assert api.posts == 1
                        await page.screenshot(
                            path=str(output / f"{name}-unknown.png"), full_page=True
                        )
                        api.state = "FAILED" if mode == "failed-result" else "SUCCEEDED"
                        await page.reload()
                        await expect(page.locator(".interpretationPanel")).to_be_visible()
                        await expect(page.locator(".interpretationPanel")).to_contain_text(RESULT)
                        assert (
                            await page.evaluate("key => sessionStorage.getItem(key)", RECEIPT)
                            is None
                        )
                        await settle(page)
                        assert (
                            api.posts == 1
                            and not api.failures
                            and not api.unexpected
                            and not errors
                        ), (api.failures, api.unexpected, errors)
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth + 1"
                        )
                        await page.screenshot(
                            path=str(output / f"{name}-result.png"), full_page=True
                        )
                        if mode != "failed-result":
                            original = api.original
                            await page.locator(".adjustForm textarea").fill(ADJUSTMENT)
                            await page.locator('.adjustForm button[type="submit"]').click()
                            await expect(confirm).to_be_visible()
                            assert api.posts == 1 and api.adjustments == 1
                            assert api.original != original
                            assert (
                                await page.evaluate("key => sessionStorage.getItem(key)", RECEIPT)
                                == api.original
                            )
                            await confirm.click()
                            await expect(confirm).to_be_visible()
                            assert api.reads[-1] == api.original
                            await page.reload()
                            await expect(confirm).to_be_visible()
                            assert api.posts == 1 and api.adjustments == 1
                            assert api.reads[-1] == api.original
                            assert not api.failures and not api.unexpected and not errors
                        print(f"PASS {name}", flush=True)
                    finally:
                        if api.failures or api.unexpected or errors:
                            print(api.failures, api.unexpected, errors, flush=True)
                        await context.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    address = urlsplit(args.url)
    if (
        address.scheme != "http"
        or address.hostname not in {"localhost", "127.0.0.1"}
        or not address.path.endswith("/tests/browser/projects.html")
    ):
        parser.error("Only the loopback projects.html mock harness is allowed")
    asyncio.run(check(args.url, args.output))
