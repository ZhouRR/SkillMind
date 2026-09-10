"""実 App の原 Result と保存時の検証範囲を全面 mock HTTP で確認する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from check_projects import PROJECT, RUN, ProjectsApi, messages, privacy, settle
from playwright.async_api import Browser, Page, Route, async_playwright, expect

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
DETAIL = json.loads((CONTRACTS / "examples/run-detail.v1.json").read_text())
CHECKED_DETAIL = json.loads((CONTRACTS / "examples/run-detail-documents.v1.json").read_text())
OUTCOME = json.loads((CONTRACTS / "examples/outcome-envelope.v1.json").read_text())
CHECKS = {
    "version": "projectmind.result-reference-checks/v1",
    "evidence": "RUN_OWNERSHIP",
    "proposals": "RUN_OWNERSHIP_AND_STATE",
    "effects": "PLATFORM_RECORD_MATCH",
    "artifacts": "NOT_VERIFIED",
}
INJECTION = '<img data-result-injection src="https://example.invalid/not-requested">'
ARTIFACT = "art_result_fixture_attachment"
INVALID = ("version", "extra", "missing", "value", "null", "kind", "flags")


class ResultApi(ProjectsApi):
    """保存時 scope と現在一覧を別々に保持し、後者から歴史を補造させない。"""

    def __init__(self, url: str, language: str, mode: str) -> None:
        """同じ Run の合成結果だけを返し、実行/評価/添付の実 API は呼ばない。"""
        super().__init__(url, language)
        self.with_run = True
        self.detail_reads = 0
        self.body = deepcopy(DETAIL)
        if mode == "contract":
            self.body = deepcopy(CHECKED_DETAIL)
            self.body.update({key: value for key, value in self.run().items() if key in self.body})
            return
        self.body.update({key: value for key, value in self.run().items() if key in self.body})
        self.body.update(
            selected_sources={},
            document_snapshots=[],
            interactions=[],
            change_proposals=[],
            approvals=[],
            effect_executions=[],
            tool_calls=[],
            evidence=[],
            attempts=[],
            sessions=[],
        )
        outcome = deepcopy(OUTCOME)
        outcome.update(
            summary="Original model result remains immutable",
            status="PARTIAL",
            evidence_refs=[],
            findings=[],
            artifact_refs=[ARTIFACT],
            change_proposal_refs=["cp_result_fixture"],
            deliverables=[
                {
                    "key": "attachment",
                    "kind": "artifact",
                    "title": "Original artifact claim",
                    "artifact_ref": ARTIFACT,
                }
            ],
            effects=[
                {
                    "proposal_ref": "cp_result_fixture",
                    "status": "APPLIED",
                    "summary": INJECTION,
                }
            ],
        )
        self.body["result"] = {
            "result_id": "00000000-0000-4000-8000-000000000040",
            "output_schema": "projectmind.outcome-envelope/v1",
            "result_kind": "STRUCTURED_OUTPUT" if mode == "structured" else "OUTCOME_ENVELOPE",
            "data": {"summary": "Original structured value"} if mode == "structured" else outcome,
            "evidence_refs": [],
            "artifact_refs": [ARTIFACT],
            "change_proposal_refs": ["cp_result_fixture"],
            "optional_schema_identity": {},
            "summary": "Original model result remains immutable",
            "confidence": 0.8,
            "needs_review": True,
            "usage": {},
            "cost": {},
            "validation": {"schema_valid": True},
            "created_at": self.body["created_at"],
        }
        if mode not in ("legacy", "none"):
            self.body["result"]["validation"].update(
                evidence_refs_valid=True,
                change_proposal_refs_valid=True,
                outcome_envelope_valid=True,
            )
            checks = deepcopy(CHECKS)
            if mode == "structured":
                checks["effects"] = "NOT_APPLICABLE"
            if mode == "version":
                checks["version"] = "projectmind.result-reference-checks/v999"
            if mode == "extra":
                checks["private"] = "must-not-display"
            if mode == "missing":
                del checks["proposals"]
            if mode == "value":
                checks["artifacts"] = "VERIFIED"
            if mode == "kind":
                checks["effects"] = "NOT_APPLICABLE"
            if mode == "flags":
                self.body["result"]["validation"]["evidence_refs_valid"] = False
            self.body["result"]["validation"]["reference_checks"] = (
                None if mode == "null" else checks
            )
        if mode == "none":
            self.body["result"] = None
        elif mode != "structured":
            self.body["effect_executions"] = [
                {
                    "effect_execution_id": "00000000-0000-4000-8000-000000000050",
                    "proposal_id": "00000000-0000-4000-8000-000000000051",
                    "run_id": RUN,
                    "approval_id": "00000000-0000-4000-8000-000000000052",
                    "tool_call_id": None,
                    "status": "APPLIED",
                    "provider": "saved-platform-record",
                    "provider_version": "v1",
                    "before_ref": None,
                    "after_ref": None,
                    "verification": {},
                    "error": None,
                    "attempt_no": 1,
                    "executed_at": None,
                    "created_at": self.body["created_at"],
                    "updated_at": self.body["created_at"],
                }
            ]

    def run(self) -> dict:
        """通常 App が読む Project-scoped 完了概要を返す。"""
        return {**super().run(), "project_id": PROJECT}

    async def respond(self, route: Route) -> None:
        """原 detail と評価履歴だけを拡張し、未知 request は共有拒否 handler に渡す。"""
        request = route.request
        address = urlsplit(request.url)
        suffix = address.path.removeprefix(self.prefix)
        if f"{address.scheme}://{address.netloc}" == self.origin and request.method == "GET":
            if suffix == f"projects/{PROJECT}/runs/{RUN}/detail":
                self.detail_reads += 1
                await route.fulfill(json=deepcopy(self.body))
                return
            if suffix == f"projects/{PROJECT}/runs/{RUN}/evaluations":
                await route.fulfill(json={"evaluations": []})
                return
        await super().respond(route)


async def check_view(page: Page, mode: str, labels: dict) -> None:
    """scope は既定で可視にし、旧/不正 record と現在の APPLIED を混同しない。"""
    texts = labels["runResult"]
    scope = page.locator(".resultValidationScope")
    if mode in INVALID:
        await page.get_by_role("tab", name=labels["workspace"]["tabResult"], exact=True).click()
        await expect(
            page.get_by_text(
                labels["interactionResponse"]["failures"]["loadFailed"], exact=True
            ).first
        ).to_be_visible()
        await expect(scope).to_have_count(0)
        await expect(page.locator(".resultSummary")).to_have_count(0)
        return
    if mode == "none":
        await expect(page.locator(".resultView")).to_be_visible()
        await expect(scope).to_have_count(0)
        await expect(page.locator(".resultSummary")).to_have_count(0)
        return
    await expect(scope).to_be_visible()
    await expect(scope.get_by_text(texts["referenceChecks"]["limit"], exact=True)).to_be_visible()
    await expect(
        scope.get_by_text(
            texts["referenceChecks"]["legacy" if mode == "legacy" else "recorded"], exact=True
        )
    ).to_be_visible()
    if mode == "legacy":
        await expect(
            scope.get_by_text(texts["referenceChecks"]["effects"], exact=True)
        ).to_have_count(0)
    else:
        for key in (
            "references",
            "artifacts",
            "effectsNotApplicable" if mode == "structured" else "effects",
        ):
            await expect(
                scope.get_by_text(texts["referenceChecks"][key], exact=True)
            ).to_be_visible()
    if mode not in ("structured", "contract"):
        await expect(page.locator(".outcomeStatus")).to_contain_text("PARTIAL")
        await expect(page.get_by_text(texts["modelEffectsHint"], exact=True)).to_be_visible()
        await expect(page.locator(".outcomeGroup > p").filter(has_text=INJECTION)).to_be_visible()
        await page.get_by_role("button", name=texts["technicalDetails"], exact=True).click()
        await expect(page.locator(".outcomeCard code").filter(has_text=ARTIFACT)).to_be_visible()
        await expect(page.locator(".outcomeCard a")).to_have_count(0)
        audit = page.locator(".resultCollapse").filter(has_text=texts["platformEffectsHint"])
        await audit.locator("summary").click()
        await expect(audit.get_by_text(texts["platformEffectsHint"], exact=True)).to_be_visible()
        await expect(audit).to_contain_text("saved-platform-record")
        if mode == "legacy":
            await expect(
                scope.get_by_text(texts["referenceChecks"]["recorded"], exact=True)
            ).to_have_count(0)
        await page.get_by_role("button", name=texts["hideTechnicalDetails"], exact=True).click()


async def scenario(
    browser: Browser, url: str, language: str, mode: str, width: int, output: Path
) -> None:
    """実 App の描画と native reload を独立した mock context で確認する。"""
    api = ResultApi(url, language, mode)
    original = deepcopy(api.body["result"])
    context = await browser.new_context(viewport={"width": width, "height": 1000}, locale=language)
    await context.route("**/*", api.route)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    name = f"{mode}-{language}-{width}"
    try:
        await page.goto(f"{url}#/workspace?project={PROJECT}&run={RUN}")
        labels = await messages(page, language)
        await expect(page.locator(".workspace .runFacts")).to_contain_text(RUN[:8])
        await check_view(page, mode, labels)
        await page.reload()
        await expect(page.locator(".workspace .runFacts")).to_contain_text(RUN[:8])
        await check_view(page, mode, labels)
        await settle(page)
        await privacy(page)
        assert not api.unexpected and not api.failures and not errors, (
            api.unexpected,
            api.failures,
            errors,
        )
        assert not api.mutations() and api.body["result"] == original
        assert api.detail_reads >= 2
        assert await page.locator("img[data-result-injection]").count() == 0
        assert "must-not-display" not in await page.locator("body").inner_text()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        if mode not in (*INVALID, "none"):
            await page.locator(".resultValidationScope").screenshot(
                path=str(output / f"{name}-scope.png")
            )
        await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
        print(f"PASS {name}", flush=True)
    except Exception:
        await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        print("Fixture failures:", api.unexpected, api.failures, errors, flush=True)
        raise
    finally:
        await context.close()


async def check(url: str, output: Path) -> None:
    """三語/狭幅と不正な新 protocol を検査し、自分の browser だけを終了する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ("zh", "ja", "en"):
                for width in (390, 1440):
                    for mode in ("checked", "contract", "legacy", "structured", "none"):
                        await scenario(browser, url, language, mode, width, output)
            for mode in INVALID:
                await scenario(browser, url, "zh", mode, 390, output)
        finally:
            await browser.close()


def main() -> None:
    """明示された専用 loopback harness 以外では実行しない。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    address = urlsplit(args.url)
    if (
        address.scheme != "http"
        or address.hostname not in ("127.0.0.1", "localhost")
        or not address.path.endswith("/tests/browser/projects.html")
        or address.query
        or address.fragment
    ):
        parser.error("Only an owned loopback projects.html mock harness is allowed")
    asyncio.run(check(args.url, args.output))


if __name__ == "__main__":
    main()
