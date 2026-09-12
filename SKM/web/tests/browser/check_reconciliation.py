"""実 App の只読核対、元 ID の応答喪失と再読を全面 mock HTTP で確認する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from urllib.parse import urlsplit

from check_projects import PROJECT, RUN, ResponseGate, privacy, settle
from check_result_references import ResultApi
from playwright.async_api import async_playwright, expect


class ReconciliationApi(ResultApi):
    """元 Effect は不変にし、合成の受理/原 ID 確認/最新観測だけを追加する。"""

    def __init__(self, url, language, mode="normal", outcome="NOT_OBSERVED"):
        """各ケースで独立した核対要求と送信履歴を持つ。"""
        super().__init__(url, language, "legacy")
        self.body["result"] = None
        self.body["status"] = "FAILED"
        effect = deepcopy(self.body["effect_executions"][0])
        effect.update(
            provider="postgres",
            status="FAILED",
            error={"code": "effect_result_unknown", "retryable": False},
        )
        self.body["effect_executions"] = [effect]
        self.effect_id = effect["effect_execution_id"]
        self.record = None
        self.submitted = []
        self.mode, self.outcome = mode, outcome
        self.gate = ResponseGate()

    def run(self):
        """別 lifecycle response で元 Run を成功へ補造しない。"""
        return {**super().run(), "status": "FAILED"}

    async def respond(self, route):
        """核対だけを mock に閉じ、他の write は基底の拒否検査へ渡す。"""
        request = route.request
        path = urlsplit(request.url).path
        root = f"/projects/{PROJECT}/runs/{RUN}"
        if path.endswith(f"{root}/effects/{self.effect_id}/reconciliations"):
            assert request.method == "POST"
            body = request.post_data_json
            assert set(body) == {"request_id"}
            self.submitted.append(body["request_id"])
            if self.mode == "late":
                self.gate.received.set()
                await self.gate.release.wait()
                await route.fulfill(
                    status=401,
                    json={
                        "type": "about:blank",
                        "title": "Expired",
                        "status": 401,
                        "code": "authentication_required",
                    },
                )
                self.gate.returned.set()
                return
            if self.mode == "lost-before" and len(self.submitted) == 1:
                await route.abort()
                return
            self.record = {
                "request_id": body["request_id"],
                "project_id": PROJECT,
                "run_id": RUN,
                "effect_execution_id": self.effect_id,
                "kind": "DATABASE_TRANSACTION",
                "status": "SUCCEEDED",
                "created_at": "2026-09-11T12:00:00Z",
                "finished_at": "2026-09-11T12:00:03Z",
                "observed_at": "2026-09-11T12:00:02Z",
                "observation_status": self.outcome,
                "error_code": None,
            }
            if self.mode == "lost-after":
                await route.abort()
            else:
                await route.fulfill(status=202, json=self.record)
            return
        if path.endswith(f"{root}/effects/{self.effect_id}/reconciliations/latest"):
            assert request.method == "GET"
            await route.fulfill(json={"latest": self.record})
            return
        if f"{root}/effect-reconciliations/" in path:
            assert request.method == "GET"
            if self.record and path.endswith(self.record["request_id"]):
                await route.fulfill(json=self.record)
            else:
                await route.fulfill(
                    status=404,
                    json={
                        "type": "about:blank",
                        "title": "Not found",
                        "status": 404,
                        "code": "reconciliation_not_found",
                    },
                )
            return
        if path.endswith(f"/runs/{RUN}/events"):
            await route.fulfill(
                content_type="text/event-stream",
                body="event: run.snapshot\ndata: "
                + json.dumps(
                    {
                        "run_id": RUN,
                        "run_attempt_id": None,
                        "agent_session_id": None,
                        "sequence": 1,
                        "event_type": "RUN_SNAPSHOT",
                        "occurred_at": "2026-09-11T00:00:00Z",
                        "payload": {"status": "FAILED", "row_version": 4},
                        "trace_id": None,
                    }
                )
                + "\n\n",
            )
            return
        await super().respond(route)


async def check(url, only_mode=None):
    """三言語/二幅と送信応答喪失を実 browser の操作・reload で確認する。"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            cases = [
                (language, width, "normal", outcome)
                for language, outcome in (
                    ("zh", "CONFIRMED"),
                    ("ja", "NOT_OBSERVED"),
                    ("en", "CONFLICT"),
                )
                for width in (390, 1440)
            ]
            cases += [
                ("en", 1440, mode, "NOT_OBSERVED") for mode in ("lost-before", "lost-after", "late")
            ]
            if only_mode is not None:
                cases = [case for case in cases if case[2] == only_mode]
            for language, width, mode, outcome in cases:
                api = ReconciliationApi(url, language, mode, outcome)
                context = await browser.new_context(viewport={"width": width, "height": 1000})
                await context.route("**/*", api.route)
                page = await context.new_page()
                errors = []
                page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                try:
                    await page.goto(f"{url}#/workspace?project={PROJECT}&run={RUN}")
                    labels = await page.evaluate(
                        "async language => (await import('/skillmind/src/lib/i18n/messages.ts'))"
                        ".MESSAGES[language].runResult.reconciliation",
                        language,
                    )
                    panel = page.locator(".reconciliationPanel")
                    await expect(
                        panel.get_by_role("button", name=labels["start"], exact=True)
                    ).to_be_enabled()
                    await panel.get_by_role("button", name=labels["start"], exact=True).click()
                    if mode == "late":
                        await asyncio.wait_for(api.gate.received.wait(), 5)
                        await page.evaluate("location.hash = '#/projects'")
                        await expect(panel).to_have_count(0)
                        api.gate.release.set()
                        await asyncio.wait_for(api.gate.returned.wait(), 5)
                        await settle(page)
                        assert await page.locator("input[type=password]").count() == 0
                        assert not errors and not api.failures and not api.unexpected
                        assert len(api.submitted) == 1
                        print("late 401 after navigation: discarded", flush=True)
                        continue
                    if mode == "lost-before":
                        await expect(
                            panel.get_by_role("button", name=labels["retry"], exact=True)
                        ).to_be_enabled()
                        await page.reload()
                        await expect(
                            panel.get_by_role("button", name=labels["retry"], exact=True)
                        ).to_be_enabled()
                        await panel.get_by_role("button", name=labels["retry"], exact=True).click()
                        assert len(api.submitted) == 2 and len(set(api.submitted)) == 1
                    await expect(panel).to_contain_text(labels["observations"][outcome])
                    await expect(panel.get_by_role("alert")).to_have_count(0)
                    await page.reload()
                    await expect(panel).to_contain_text(labels["observations"][outcome])
                    assert len(api.submitted) == (2 if mode == "lost-before" else 1)
                    await settle(page)
                    await privacy(page)
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 1"
                    )
                    assert not errors and not api.unexpected and not api.failures, (
                        errors,
                        api.unexpected,
                        api.failures,
                    )
                    if (language, width, mode) == ("zh", 1440, "normal"):
                        await page.screenshot(
                            path="/tmp/skillmind-reconciliation.png", full_page=True
                        )
                    print(f"{language} {width}px {mode} {outcome}: passed", flush=True)
                finally:
                    await context.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5187/skillmind/")
    parser.add_argument("--mode", choices=("normal", "lost-before", "lost-after", "late"))
    args = parser.parse_args()
    asyncio.run(check(args.url, args.mode))
