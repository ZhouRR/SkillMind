"""実 App の提案差分・重複判断・離頁後応答を loopback の合成 HTTP で確認する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from itertools import product
from pathlib import Path
from urllib.parse import urlsplit

from check_accounts import ACTOR, ResponseGate
from check_projects import PROJECT, RUN, layout, messages
from check_workspace_reports import WorkspaceApi
from playwright.async_api import Error, Route, async_playwright, expect

PROPOSAL = "00000000-0000-4000-8000-000000000701"
NOW = "2026-09-19T00:00:00Z"


def pending_proposal() -> dict:
    """外部 repository へ到達しない、公開 shape の未決提案を作る。"""
    return {
        "proposal_id": PROPOSAL,
        "proposal_ref": "cp_browser_decision",
        "project_id": PROJECT,
        "run_id": RUN,
        "run_segment_id": "00000000-0000-4000-8000-000000000702",
        "agent_session_id": "00000000-0000-4000-8000-000000000703",
        "target_binding_id": "00000000-0000-4000-8000-000000000704",
        "integration_id": "00000000-0000-4000-8000-000000000705",
        "effect_intent_key": "browser-only-change",
        "capability_version": "repository.write/v1",
        "operation": "commit",
        "target": {"display": "Browser fixture repository", "locator": "main"},
        "summary": "Review the exact file contents before deciding",
        "changes": [{"path": "/files/example.txt", "action": "SET", "value": "first line\nsecond line\n"}],
        "precondition": {"revision": "a" * 40},
        "evidence_refs": [],
        "risk_level": "LOW",
        "reversible": True,
        "rollback": {},
        "verification": {},
        "status": "PENDING_APPROVAL",
        "version": 1,
        "checksum": "sha256:" + "a" * 64,
        "expires_at": "2099-01-01T00:00:00Z",
        "created_at": NOW,
        "updated_at": NOW,
    }


class ProposalApi(WorkspaceApi):
    """既存 App fixture に一つの提案と制御可能な判断応答だけを追加する。"""

    def __init__(self, url: str, language: str) -> None:
        """各 case に独立した提案、要求記録、応答 gate を持たせる。"""
        super().__init__(url, language)
        self.body["change_proposals"] = [pending_proposal()]
        self.decisions: list[dict] = []
        self.decision_gate = ResponseGate()
        self.reject_decision = False

    async def respond(self, route: Route) -> None:
        """精確な decision endpoint 以外は既存の全面拒否 handler に渡す。"""
        request = route.request
        address = urlsplit(request.url)
        suffix = address.path.removeprefix(self.prefix)
        if (f"{address.scheme}://{address.netloc}" != self.origin
                or request.method != "POST"
                or suffix != f"projects/{PROJECT}/runs/{RUN}/proposals/{PROPOSAL}/decision"):
            await super().respond(route)
            return
        body = request.post_data_json
        self.decisions.append({"body": body, "key": request.headers.get("idempotency-key")})
        gate = self.decision_gate
        gate.received.set()
        await gate.release.wait()
        try:
            if self.reject_decision:
                await route.fulfill(status=409, json={
                    "status": 409, "title": "Fixture conflict", "code": "proposal_version_conflict",
                    "detail": "Synthetic conflict; no external operation was performed",
                })
            else:
                proposal = {**pending_proposal(), "status": body["decision"]}
                self.body["change_proposals"] = [proposal]
                await route.fulfill(json={
                    "proposal": deepcopy(proposal),
                    "approval": {
                        "approval_id": "00000000-0000-4000-8000-000000000706",
                        "proposal_id": PROPOSAL, "run_id": RUN, "source": "USER",
                        "decision": body["decision"], "actor_id": ACTOR,
                        "preauthorization_id": None, "proposal_version": 1,
                        "proposal_checksum": proposal["checksum"], "reason": body["reason"],
                        "created_at": NOW,
                    },
                    "effect_execution": None,
                    "run_status": "QUEUED",
                    "idempotent_replay": False,
                })
        except Error:
            # Native abort が route を閉じた場合も、遅い応答の終了を必ず通知する。
            pass
        finally:
            gate.returned.set()


FETCH_OBSERVER = """(() => {
  const original = window.fetch;
  window.__decisionFetches = [];
  window.fetch = function(input, options) {
    if (String(input).endsWith('/decision')) {
      window.__decisionFetches.push({method: options?.method,
        key: options?.headers?.['Idempotency-Key']});
    }
    return original.call(this, input, options);
  };
})();"""


async def check(url: str, output: Path) -> None:
    """三語と狭幅で差分を保持し、同一 tick と離頁境界の所有権を実測する。"""
    output.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language, scenario, theme in product(
                    ("ja", "zh", "en"), ("conflict", "late-success"), ("dark", "light")):
                api = ProposalApi(url, language)
                api.reject_decision = scenario == "conflict"
                width = 390 if scenario == "conflict" else 1440
                context = await browser.new_context(viewport={"width": width, "height": 1000})
                await context.route("**/*", api.route)
                await context.add_init_script(FETCH_OBSERVER)
                await context.add_init_script(f"localStorage.setItem('skillmind.theme', '{theme}')")
                if scenario == "late-success":
                    # Abort 非対応 transport でも古い所有者の callback を受理しない。
                    await context.add_init_script("""const original = window.fetch;
                      window.fetch = (input, init) => original(input,
                        init ? {...init, signal: undefined} : init);""")
                page = await context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                try:
                    await page.goto(f"{url}#/history?project={PROJECT}&run={RUN}")
                    labels = await messages(page, language)
                    await page.get_by_role("tab", name=labels["workspace"]["tabResult"], exact=True).click()
                    card = page.locator(".proposalCard")
                    await expect(card).to_be_visible()
                    file = card.locator("details.proposalFile")
                    await file.locator("summary").click()
                    reason = "Reviewed exact fixture contents"
                    await card.locator("textarea").fill(reason)
                    assert await file.evaluate("element => element.open")
                    await expect(card.locator(".proposalFileBody")).to_have_text("first line\nsecond line\n")
                    await page.screenshot(path=str(output / f"{language}-{scenario}-{theme}-draft.png"))
                    try:
                        await layout(page)
                    except AssertionError:
                        print(await page.evaluate("""() => [...document.querySelectorAll('main *')]
                          .filter(e => e.getBoundingClientRect().right > innerWidth + 1)
                          .map(e => ({tag:e.tagName, cls:e.className,
                            text:e.textContent.slice(0,160), width:e.getBoundingClientRect().width}))"""), flush=True)
                        raise
                    approve = card.locator("button.primaryButton")
                    await approve.evaluate("element => { element.click(); element.click(); }")
                    await asyncio.wait_for(api.decision_gate.received.wait(), timeout=5)
                    await expect(approve).to_be_disabled()
                    assert await file.evaluate("element => element.open")
                    assert await page.evaluate("window.__decisionFetches.length") == 1
                    assert len(api.decisions) == 1
                    assert api.decisions[0]["body"] == {
                        "decision": "APPROVED", "proposal_version": 1,
                        "proposal_checksum": pending_proposal()["checksum"], "reason": reason,
                    }
                    if scenario == "conflict":
                        api.decision_gate.release.set()
                        await expect(card.get_by_role("alert")).to_be_visible()
                        await expect(approve).to_be_enabled()
                        assert await file.evaluate("element => element.open")
                        assert await card.locator("textarea").input_value() == reason
                        await page.screenshot(path=str(output / f"{language}-conflict-{theme}-retained.png"))
                    else:
                        await page.locator('.sideNav a[href*="/accounts"]').click()
                        await expect(page.locator(".accountsPage")).to_be_visible()
                        reads_before_response = api.detail_reads
                        api.decision_gate.release.set()
                        await asyncio.wait_for(api.decision_gate.returned.wait(), timeout=5)
                        await page.wait_for_timeout(200)
                        assert api.detail_reads == reads_before_response
                        await expect(page.locator(".proposalCard")).to_have_count(0)
                    assert not errors and not api.unexpected and not api.failures
                    results.append({"language": language, "scenario": scenario, "width": width, "theme": theme,
                                    "decision_requests": len(api.decisions), "errors": errors})
                    print(f"PASS proposal-decision {language} {scenario} {theme}", flush=True)
                finally:
                    api.decision_gate.release.set()
                    await context.close()
        finally:
            await browser.close()
    (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    target = urlsplit(args.url)
    if (target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost"}
            or not target.path.endswith("/tests/browser/projects.html")):
        parser.error("Only loopback projects.html is allowed")
    asyncio.run(check(args.url, args.output))
