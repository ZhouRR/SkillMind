"""実 App の評価原要求・読取確認・履歴ページングを全面 mock HTTP で検証する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from check_accounts import CSRF, OTHER, PASSWORD, ResponseGate, self_revoke
from check_artifacts import NEXT_RUN, ArtifactApi
from check_projects import NEXT_PROJECT, PROJECT, RUN, messages, privacy, settle
from playwright.async_api import Browser, Page, Route, async_playwright, expect

RESULT = "00000000-0000-4000-8000-000000000080"
NEXT_RESULT = "00000000-0000-4000-8000-000000000081"
KEY = "00000000-0000-4000-8000-000000000911"
DATA = {"summary": "Original result", "nullable": None, "a/b": {"~value": {"a": 1}}}
PRIVATE = "Evaluation internal transaction detail must not display"
LATE = (
    "cancel-post",
    "timeout-post",
    "deadline-post",
    "project-late",
    "run-late",
    "result-late",
    "actor-late",
    "session-late",
)
CASES = (
    "same-tick",
    "revisions",
    "invalid-json",
    "duplicate-pointer",
    "pagination",
    "history-race",
    "unknown-get",
    "notfound-late-publish",
    "explicit-resend",
    "get-cancel-401",
    "get-timeout",
    "get-deadline",
    "conflict",
    "bad-receipt",
    "bad-actor",
    "storage",
    "read-denied",
    "bad-page",
    "detail-refresh",
    "bad-result",
    "history-401",
    "unicode",
    "manual-change",
    "lookup-cancel",
    "get-401",
    "get-denied-late",
    "get-notfound-late",
    *LATE,
)


class EvaluationApi(ArtifactApi):
    """共有 App/Project/Run fixture を使い、評価保存と元受付記録だけを追加する。"""

    def __init__(self, url: str, language: str, mode: str) -> None:
        """各 case に別の保存領域と遅延 gate を持ち、実 API を使わない。"""
        super().__init__(url, language, "v1")
        self.case = mode
        self.posts: list[dict] = []
        self.confirmations: list[tuple[str, str]] = []
        self.pages_read: list[str | None] = []
        self.receipts: dict[str, dict] = {}
        self.post_gate = ResponseGate()
        self.get_gate = ResponseGate()
        self.history_gate = ResponseGate()
        self.event_gate = ResponseGate()
        self.detail_gate = ResponseGate()
        self.body["result"].update(
            result_id=RESULT,
            result_kind="STRUCTURED_OUTPUT",
            output_schema="fixture/v1",
            data=deepcopy(DATA),
            artifact_refs=[],
            change_proposal_refs=[],
            validation={"schema_valid": True},
        )
        self.body.update(
            document_snapshots=[],
            selected_sources={},
            interactions=[],
            change_proposals=[],
            approvals=[],
            effect_executions=[],
        )
        self.history = [self.record(index) for index in range(25)] if mode == "pagination" else []
        if mode in (
            "archived",
            "manual",
            "manual-change",
            "lookup-cancel",
            "get-401",
            "get-denied-late",
            "get-notfound-late",
        ):
            self.receipts[KEY] = self.receipt(
                {
                    "submission_key": KEY,
                    "result_id": RESULT,
                    "rating": 4,
                    "verdict": "accurate",
                    "comment": "Historical original receipt",
                    "revisions": [],
                }
            )
        if mode == "archived":
            self.details[PROJECT]["status"] = "ARCHIVED"
            self.projects[0]["status"] = "ARCHIVED"
        if mode == "bad-result":
            self.body["result"]["result_id"] = "bad historical identity"
        if mode == "unicode":
            self.body["result"]["data"]["😀" * 511] = None

    def record(self, index: int) -> dict:
        """時刻と UUID の昇順が明確な追加式履歴を作る。"""
        return {
            "evaluation_id": f"00000000-0000-4000-8000-{index + 200:012d}",
            "result_id": RESULT,
            "run_id": RUN,
            "user_id": self.actor,
            "rating": 3,
            "verdict": "uncertain",
            "comment": f"Saved history {index}",
            "revisions": [],
            "created_at": (
                datetime(2026, 9, 10, tzinfo=UTC) + timedelta(seconds=index)
            ).isoformat(),
        }

    def receipt(self, body: dict) -> dict:
        """原入力を複製し、fixture の元 Result から original_value を付ける。"""
        item = self.record(90 + len(self.posts))
        item.update(
            {key: deepcopy(body[key]) for key in ("result_id", "rating", "verdict", "comment")}
        )
        for revision in body["revisions"]:
            value = self.body["result"]["data"]
            for token in revision["pointer"][1:].split("/"):
                value = value[token.replace("~1", "/").replace("~0", "~")]
            item["revisions"].append({**deepcopy(revision), "original_value": deepcopy(value)})
        return {
            "project_id": PROJECT,
            "run_id": RUN,
            "submission_key": body["submission_key"],
            "evaluation": item,
        }

    async def respond(self, route: Route) -> None:
        """公開 endpoint・原 payload・CSRF を照合し、外部 URL は共有拒否へ送る。"""
        request = route.request
        address = urlsplit(request.url)
        suffix = address.path.removeprefix(self.prefix)
        parts = suffix.split("/")
        query = parse_qs(address.query)
        if f"{address.scheme}://{address.netloc}" != self.origin:
            await super().respond(route)
            return
        if (
            request.method == "GET"
            and suffix == f"runs/{RUN}/events"
            and self.case in ("detail-refresh", "result-late")
        ):
            self.event_gate.received.set()
            await asyncio.wait_for(self.event_gate.release.wait(), 40)
            event = {
                "run_id": RUN,
                "run_attempt_id": None,
                "sequence": 1,
                "event_type": "RUN_SNAPSHOT",
                "occurred_at": self.body["created_at"],
                "trace_id": None,
                "agent_session_id": None,
                "payload": {"status": "SUCCEEDED", "row_version": 5},
            }
            await route.fulfill(
                content_type="text/event-stream",
                body=f"event: run.snapshot\ndata: {json.dumps(event)}\n\n",
            )
            self.event_gate.returned.set()
            return
        if len(parts) >= 5 and parts[0] == "projects" and parts[2] == "runs":
            project_id, run_id, action = parts[1], parts[3], parts[4]
            if action == "detail":
                self.detail_reads += 1
                if self.case in ("detail-refresh", "result-late") and self.body["row_version"] == 5:
                    self.detail_gate.received.set()
                    await asyncio.wait_for(self.detail_gate.release.wait(), 40)
                    await route.fulfill(json=deepcopy(self.body))
                    self.detail_gate.returned.set()
                    return
            if action == "artifacts":
                await route.fulfill(json=[])
                return
            if action == "evaluations" and len(parts) == 6 and parts[5] == "page":
                assert request.method == "GET" and query.get("limit") == ["20"]
                after = query.get("after", [None])[0]
                self.pages_read.append(after)
                if self.case in ("read-denied", "history-401"):
                    await self.problem(
                        route,
                        401 if self.case == "history-401" else 403,
                        "unauthorized" if self.case == "history-401" else "forbidden",
                    )
                    return
                if (
                    self.case in ("get-denied-late", "get-notfound-late")
                    and len(self.pages_read) > 1
                ):
                    await self.problem(
                        route,
                        403 if self.case == "get-denied-late" else 404,
                        "forbidden" if self.case == "get-denied-late" else "run_not_found",
                    )
                    return
                items = deepcopy(self.history)
                if after:
                    position = next(
                        index for index, item in enumerate(items) if item["evaluation_id"] == after
                    )
                    items = items[position + 1 :]
                result_id = self.body["result"]["result_id"]
                page = {
                    "project_id": project_id,
                    "run_id": run_id,
                    "result_id": result_id,
                    "items": items[:20],
                    "next_cursor": items[19]["evaluation_id"] if len(items) > 20 else None,
                }
                if self.case == "bad-page":
                    page["result_id"] = NEXT_RESULT
                if self.case == "history-race" and len(self.pages_read) == 1:
                    self.history_gate.received.set()
                    await asyncio.wait_for(self.history_gate.release.wait(), 40)
                await route.fulfill(json=page)
                self.history_gate.returned.set()
                return
            if action == "evaluation-submissions" and len(parts) == 5 and request.method == "POST":
                assert request.headers.get("x-csrf-token") == CSRF
                assert (project_id, run_id) == (PROJECT, RUN)
                body = request.post_data_json
                assert set(body) == {
                    "submission_key",
                    "result_id",
                    "rating",
                    "verdict",
                    "comment",
                    "revisions",
                }
                assert UUID(body["submission_key"]).int and body["result_id"] == RESULT
                self.posts.append(deepcopy(body))
                if len(self.posts) > 1:
                    assert body == self.posts[0], "Original retry changed its key or content"
                if self.case == "refused":
                    await self.problem(route, 400, "invalid_evaluation_revision")
                    return
                if self.case in ("forbidden", "conflict", "storage"):
                    status, code = {
                        "forbidden": (403, "forbidden"),
                        "conflict": (409, "evaluation_submission_conflict"),
                        "storage": (503, "evaluation_storage_unavailable"),
                    }[self.case]
                    await self.problem(route, status, code)
                    return
                receipt = self.receipts.get(body["submission_key"]) or self.receipt(body)
                if self.case in (*LATE, "notfound-late-publish", "detail-refresh"):
                    self.post_gate.received.set()
                    await asyncio.wait_for(self.post_gate.release.wait(), 40)
                self.receipts[body["submission_key"]] = receipt
                if not any(
                    item["evaluation_id"] == receipt["evaluation"]["evaluation_id"]
                    for item in self.history
                ):
                    self.history.append(receipt["evaluation"])
                if (
                    self.case
                    in (
                        "unknown-get",
                        "explicit-resend",
                        "get-cancel-401",
                        "get-timeout",
                        "get-deadline",
                    )
                    and len(self.posts) == 1
                ):
                    await route.abort()
                elif self.case in (*LATE, "get-cancel-401") and self.case != "cancel-post":
                    await self.problem(route, 401, "session_expired")
                else:
                    published = deepcopy(receipt)
                    if self.case == "bad-receipt":
                        published["evaluation"]["comment"] = "Another payload"
                    if self.case == "bad-actor":
                        published["evaluation"]["user_id"] = OTHER
                    await route.fulfill(status=201 if len(self.posts) == 1 else 200, json=published)
                self.post_gate.returned.set()
                return
            if action == "evaluation-submissions" and len(parts) == 6 and request.method == "GET":
                key = parts[5]
                assert UUID(key).int and query == {"result_id": [RESULT]}
                self.confirmations.append((key, query["result_id"][0]))
                if self.case == "get-401":
                    await self.problem(route, 401, "session_expired")
                elif self.case in (
                    "get-cancel-401",
                    "get-timeout",
                    "get-deadline",
                    "lookup-cancel",
                    "get-denied-late",
                    "get-notfound-late",
                ):
                    self.get_gate.received.set()
                    await asyncio.wait_for(self.get_gate.release.wait(), 40)
                    if self.case in ("get-denied-late", "get-notfound-late"):
                        await route.fulfill(json=self.receipts[key])
                    else:
                        await self.problem(route, 401, "session_expired")
                elif key not in self.receipts:
                    await self.problem(route, 404, "evaluation_submission_not_found")
                else:
                    await route.fulfill(json=self.receipts[key])
                self.get_gate.returned.set()
                return
        await super().respond(route)

    async def problem(self, route: Route, status: int, code: str) -> None:
        """私有の説明を含む Problem でも UI は静的分類だけを表示する。"""
        await route.fulfill(status=status, json={"status": status, "code": code, "detail": PRIVATE})


async def open_evaluation(page: Page, labels: dict) -> None:
    """結果の初期選択を待ち、評価 drawer をキーボードで開く。"""
    await expect(page.locator(f'.runFacts dd[title="{RUN}"]')).to_be_visible()
    section = page.locator(".evaluationSection")
    if not await section.locator(".evaluationForm").is_visible():
        await page.get_by_role(
            "button", name=labels["runResult"]["manualEvaluation"], exact=True
        ).focus()
        await page.keyboard.press("Enter")
    await expect(section.locator(".evaluationForm")).to_be_visible()


async def submit_form(page: Page, labels: dict, mode: str) -> None:
    """表示 form を操作し、同 tick case だけ二回の submit event を同期発火する。"""
    form = page.locator(".evaluationForm")
    await form.get_by_label(labels["runResult"]["commentLabel"], exact=True).fill(
        "Original human evaluation <not-html>"
    )
    if mode == "same-tick":
        await form.evaluate(
            """form => { for (let i=0; i<2; i++) form.dispatchEvent(
              new Event('submit', {bubbles:true,cancelable:true})); }"""
        )
    elif mode == "success":
        await form.get_by_role(
            "button", name=labels["runResult"]["addEvaluation"], exact=True
        ).focus()
        await page.keyboard.press("Space")
    else:
        await form.get_by_role(
            "button", name=labels["runResult"]["addEvaluation"], exact=True
        ).click()


async def expect_draft_locked(section) -> None:
    """fieldset 属性に加え、実際の入力と submit が無効なことを確認する。"""
    form = section.locator(".evaluationForm")
    await expect(form.locator("fieldset").first).to_have_attribute("disabled", "")
    for control in await form.locator("input, select, textarea, button").all():
        await expect(control).to_be_disabled()


async def scenario(
    browser: Browser, url: str, mode: str, language: str, width: int, output: Path
) -> None:
    """完全 mock App の操作と旧応答の観測を、独立 context に閉じる。"""
    api = EvaluationApi(url, language, mode)
    context = await browser.new_context(viewport={"width": width, "height": 1000}, locale=language)
    await context.route("**/*", api.route)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            errors.append(message.text)
            if message.type == "error" and "same key" in message.text
            else None
        ),
    )
    await page.add_init_script("""(() => {
      const send = window.fetch.bind(window);
      window.fetch = (input, init) => String(input).includes('/evaluation-submissions')
        ? send(input, init ? {...init, signal: undefined} : init) : send(input, init);
    })();""")
    name = f"{mode}-{language}-{width}"
    try:
        await page.goto(f"{url}#/workspace?project={PROJECT}&run={RUN}")
        catalog = await messages(page, language)
        labels = catalog["evaluation"]
        section = page.locator(".evaluationSection")
        intent = section.locator(".evaluationSubmission")
        if mode == "history-401":
            await expect(page.locator('input[name="email"]')).to_be_visible()
            assert api.pages_read and not api.posts and not api.confirmations
        elif mode == "bad-result":
            await expect(page.locator(".resultSummary")).to_be_visible()
            await expect(
                page.get_by_text(labels["failures"]["loadFailed"], exact=True)
            ).to_be_visible()
            await expect(section).to_have_count(0)
            assert not api.pages_read and not api.posts
        else:
            await open_evaluation(page, catalog)
            if mode in (
                "archived",
                "manual",
                "manual-change",
                "lookup-cancel",
                "get-401",
                "get-denied-late",
                "get-notfound-late",
            ):
                if mode == "archived":
                    await expect_draft_locked(section)
                    await expect(section).to_contain_text(labels["failures"]["projectArchived"])
                if mode in ("manual-change", "lookup-cancel"):
                    await section.get_by_label(
                        catalog["runResult"]["commentLabel"], exact=True
                    ).fill("Keep this draft")
                await section.get_by_label(labels["lookupLabel"], exact=True).fill(
                    PROJECT if mode == "manual-change" else KEY
                )
                await (
                    section.locator(".evaluationLookup")
                    .get_by_role("button", name=labels["confirm"], exact=True)
                    .click()
                )
                if mode == "get-401":
                    await expect(page.locator('input[name="email"]')).to_be_visible()
                elif mode in ("lookup-cancel", "get-denied-late", "get-notfound-late"):
                    await asyncio.wait_for(api.get_gate.received.wait(), 5)
                    if mode == "lookup-cancel":
                        await intent.get_by_role(
                            "button", name=labels["closeLookup"], exact=True
                        ).click()
                    else:
                        await section.get_by_role(
                            "button", name=labels["refreshHistory"], exact=True
                        ).click()
                        await expect(section).to_contain_text(
                            labels["failures"][
                                "forbidden" if mode == "get-denied-late" else "notFound"
                            ]
                        )
                    api.get_gate.release.set()
                    await asyncio.wait_for(api.get_gate.returned.wait(), 5)
                    await settle(page)
                    await expect(page.locator('input[name="email"]')).to_have_count(0)
                    if mode == "lookup-cancel":
                        await expect(intent).to_have_count(0)
                        await expect(
                            section.get_by_label(catalog["runResult"]["commentLabel"], exact=True)
                        ).to_have_value("Keep this draft")
                    else:
                        await expect(intent).to_contain_text(labels["phase"]["unknown"])
                        await expect(section.locator(".evaluationItem")).to_have_count(0)
                else:
                    if mode == "manual-change":
                        await expect(intent).to_contain_text(labels["failures"]["notSeen"])
                        await intent.evaluate("element => element.scrollIntoView({block:'start'})")
                        await page.screenshot(path=str(output / f"{name}-lookup-viewport.png"))
                        await intent.get_by_role(
                            "button", name=labels["closeLookup"], exact=True
                        ).click()
                        await expect(
                            section.get_by_label(catalog["runResult"]["commentLabel"], exact=True)
                        ).to_have_value("Keep this draft")
                        await section.get_by_label(labels["lookupLabel"], exact=True).fill(KEY)
                        await (
                            section.locator(".evaluationLookup")
                            .get_by_role("button", name=labels["confirm"], exact=True)
                            .click()
                        )
                    await expect(intent).to_contain_text(labels["phase"]["confirmed"])
                    await expect(intent).to_contain_text(labels["lookupOnly"])
                    await expect(
                        intent.get_by_role("button", name=labels["resend"], exact=True)
                    ).to_have_count(0)
                    assert api.confirmations[-1] == (KEY, RESULT)
                assert not api.posts
            elif mode in ("read-denied", "bad-page"):
                await expect(section.locator(".evaluationHistory")).to_contain_text(
                    labels["failures"]["forbidden" if mode == "read-denied" else "loadFailed"]
                )
                if mode == "read-denied":
                    await expect_draft_locked(section)
                assert not api.posts
            elif mode == "unicode":
                comment = section.get_by_label(catalog["runResult"]["commentLabel"], exact=True)
                await comment.fill("😀" * 4000)
                await expect(comment).to_have_value("😀" * 4000)
                await section.get_by_role(
                    "button", name=catalog["runResult"]["addRevision"], exact=True
                ).click()
                row = section.locator(".evaluationRevisionEditor")
                pointer = "/" + "😀" * 511
                await row.get_by_label(catalog["runResult"]["jsonPointerLabel"], exact=True).fill(
                    pointer
                )
                await row.get_by_label(
                    catalog["runResult"]["suggestedValueLabel"], exact=True
                ).fill("null")
                reason = row.get_by_label(catalog["runResult"]["revisionReasonLabel"], exact=True)
                await reason.fill("😀" * 1000)
                await expect(reason).to_have_value("😀" * 1000)
                submit = section.locator(".evaluationForm").get_by_role(
                    "button", name=catalog["runResult"]["addEvaluation"], exact=True
                )
                await comment.fill("a" * 4001)
                await submit.click()
                await expect(section).to_contain_text(labels["draftErrors"]["invalidDraft"])
                assert not api.posts
                await comment.fill("😀" * 4000)
                await reason.fill("a" * 1001)
                await submit.click()
                await expect(section).to_contain_text(labels["draftErrors"]["invalidReason"])
                assert not api.posts
                await reason.fill("😀" * 1000)
                await submit.click()
                await expect(intent).to_contain_text(labels["phase"]["confirmed"])
                assert api.posts[0]["comment"] == "😀" * 4000
                assert api.posts[0]["revisions"][0]["reason"] == "😀" * 1000
                assert api.posts[0]["revisions"][0]["pointer"] == pointer
            elif mode in ("revisions", "invalid-json", "duplicate-pointer"):
                for pointer, value, reason in [
                    ("/nullable", "null", "Keep original null"),
                    ("/a~1b/~0value", '{"a":2}', "Escaped original"),
                ]:
                    await section.get_by_role(
                        "button", name=catalog["runResult"]["addRevision"], exact=True
                    ).click()
                    row = section.locator(".evaluationRevisionEditor").last
                    await row.get_by_label(
                        catalog["runResult"]["jsonPointerLabel"], exact=True
                    ).fill(pointer)
                    await row.get_by_label(
                        catalog["runResult"]["suggestedValueLabel"], exact=True
                    ).fill(value)
                    await row.get_by_label(
                        catalog["runResult"]["revisionReasonLabel"], exact=True
                    ).fill(reason)
                if mode == "invalid-json":
                    await (
                        section.locator(".evaluationRevisionEditor")
                        .last.get_by_label(catalog["runResult"]["suggestedValueLabel"], exact=True)
                        .fill("unquoted text")
                    )
                if mode == "duplicate-pointer":
                    await (
                        section.locator(".evaluationRevisionEditor")
                        .last.get_by_label(catalog["runResult"]["jsonPointerLabel"], exact=True)
                        .fill("/nullable")
                    )
                await submit_form(page, catalog, mode)
                if mode != "revisions":
                    await expect(section).to_contain_text(
                        labels["draftErrors"][
                            "invalidJson" if mode == "invalid-json" else "duplicatePointer"
                        ]
                    )
                    assert not api.posts
                    await (
                        section.locator(".evaluationRevisionEditor")
                        .last.get_by_role("button", name=labels["removeRevision"], exact=True)
                        .click()
                    )
                    await expect(section.locator(".evaluationRevisionEditor")).to_have_count(1)
                else:
                    await expect(intent).to_contain_text(labels["phase"]["confirmed"])
                    assert api.posts[0]["revisions"][0]["suggested_value"] is None
                    assert len(api.posts[0]["revisions"]) == 2
                    await expect(
                        section.locator(".evaluationHistory .evaluationRevision")
                    ).to_have_count(2)
            else:
                if mode == "pagination":
                    await expect(section.locator(".evaluationItem")).to_have_count(20)
                if mode in ("timeout-post", "get-timeout"):
                    await page.clock.install()
                await submit_form(page, catalog, mode)
                if mode in (*LATE, "notfound-late-publish", "detail-refresh"):
                    await asyncio.wait_for(api.post_gate.received.wait(), 5)
                if mode in ("cancel-post", "notfound-late-publish"):
                    await intent.get_by_role("button", name=labels["cancel"], exact=True).click()
                    await expect(intent).to_contain_text(labels["phase"]["unknown"])
                if mode == "notfound-late-publish":
                    await intent.get_by_role("button", name=labels["confirm"], exact=True).click()
                    await expect(intent).to_contain_text(labels["failures"]["notSeen"])
                    assert len(api.posts) == 1
                if mode in ("detail-refresh", "result-late"):
                    await page.keyboard.press("Escape")
                    await expect(page.locator(".evaluationNotice")).to_contain_text(
                        labels["phase"]["sending"]
                    )
                    await page.get_by_role(
                        "tab", name=catalog["workspace"]["tabConversation"], exact=True
                    ).click()
                    await page.get_by_role(
                        "tab", name=catalog["workspace"]["tabResult"], exact=True
                    ).click()
                    await open_evaluation(page, catalog)
                    await expect(intent).to_contain_text(labels["phase"]["sending"])
                    api.body["row_version"] = 5
                    marker = f"Accepted detail {mode}"
                    api.body["result"]["summary"] = marker
                    if mode == "result-late":
                        api.body["result"]["result_id"] = NEXT_RESULT
                    before = api.detail_reads
                    api.event_gate.release.set()
                    await asyncio.wait_for(api.event_gate.returned.wait(), 5)
                    await asyncio.wait_for(api.detail_gate.received.wait(), 5)
                    await expect(page.locator(".resultView")).to_have_count(0)
                    await expect(intent).to_contain_text(labels["phase"]["sending"])
                    api.detail_gate.release.set()
                    await asyncio.wait_for(api.detail_gate.returned.wait(), 5)
                    await expect(page.locator(".resultSummary h3")).to_have_text(marker)
                    await page.wait_for_function(
                        "() => document.querySelector('.runArtifacts') !== null"
                    )
                    await settle(page)
                    assert api.detail_reads > before
                    if mode == "result-late":
                        await expect(intent).to_have_count(0)
                        await expect(
                            section.get_by_label(catalog["runResult"]["commentLabel"], exact=True)
                        ).to_have_value("")
                    else:
                        await expect(intent).to_contain_text(labels["phase"]["sending"])
                if mode in ("project-late", "run-late"):
                    if mode == "project-late":
                        api.run_project = NEXT_PROJECT
                    target = NEXT_PROJECT if mode == "project-late" else PROJECT
                    await page.evaluate(
                        "hash => location.hash = hash",
                        f"/workspace?project={target}&run={NEXT_RUN}",
                    )
                    await expect(page.locator(f'.runFacts dd[title="{NEXT_RUN}"]')).to_be_visible()
                    await expect(intent).to_have_count(0)
                if mode in ("actor-late", "session-late"):
                    await page.evaluate("location.hash = '/accounts'")
                    await expect(page.locator("[data-account-own]")).to_be_visible()
                    await self_revoke(page, api, catalog["account"])
                    if mode == "actor-late":
                        api.actor = OTHER
                    await page.locator('input[name="email"]').fill(api.users[api.actor]["email"])
                    await page.locator('input[name="password"]').fill(PASSWORD)
                    await page.locator('button[type="submit"]').click()
                    await expect(page.locator("[data-account-own]")).to_be_visible()
                    await page.evaluate(
                        "hash => location.hash = hash", f"/workspace?project={PROJECT}&run={RUN}"
                    )
                    await open_evaluation(page, catalog)
                    await expect(intent).to_have_count(0)
                    await expect(
                        section.get_by_label(catalog["runResult"]["commentLabel"], exact=True)
                    ).to_have_value("")
                if mode == "timeout-post":
                    await page.clock.run_for(30_001)
                    await expect(intent).to_contain_text(labels["phase"]["unknown"])
                if mode == "deadline-post":
                    await page.evaluate(
                        "Object.defineProperty(performance, 'now', "
                        "{value: () => 31000 + performance.timeOrigin})"
                    )
                if mode in (*LATE, "notfound-late-publish", "detail-refresh"):
                    api.post_gate.release.set()
                    await asyncio.wait_for(api.post_gate.returned.wait(), 5)
                    await settle(page)
                    await expect(page.locator('input[name="email"]')).to_have_count(0)
                if mode in (
                    "unknown-get",
                    "explicit-resend",
                    "get-cancel-401",
                    "get-timeout",
                    "get-deadline",
                ):
                    await expect(intent).to_contain_text(labels["phase"]["unknown"])
                    if mode == "explicit-resend":
                        await intent.get_by_role(
                            "button", name=labels["resend"], exact=True
                        ).click()
                    else:
                        await intent.get_by_role(
                            "button", name=labels["confirm"], exact=True
                        ).click()
                    if mode.startswith("get-"):
                        await asyncio.wait_for(api.get_gate.received.wait(), 5)
                        if mode == "get-cancel-401":
                            await intent.get_by_role(
                                "button", name=labels["cancel"], exact=True
                            ).click()
                        elif mode == "get-timeout":
                            await page.clock.run_for(30_001)
                        else:
                            await page.evaluate(
                                "Object.defineProperty(performance, 'now', "
                                "{value: () => 31000 + performance.timeOrigin})"
                            )
                        api.get_gate.release.set()
                        await asyncio.wait_for(api.get_gate.returned.wait(), 5)
                        await settle(page)
                        await expect(intent).to_contain_text(labels["phase"]["unknown"])
                        await expect(page.locator('input[name="email"]')).to_have_count(0)
                if mode == "notfound-late-publish":
                    await expect(intent).to_contain_text(labels["phase"]["unknown"])
                    await intent.get_by_role("button", name=labels["confirm"], exact=True).click()
                if mode == "history-race":
                    await expect(intent).to_contain_text(labels["phase"]["confirmed"])
                    api.history_gate.release.set()
                    await asyncio.wait_for(api.history_gate.returned.wait(), 5)
                if mode in (
                    "success",
                    "same-tick",
                    "unknown-get",
                    "notfound-late-publish",
                    "explicit-resend",
                    "history-race",
                    "pagination",
                    "detail-refresh",
                ):
                    await expect(intent).to_contain_text(labels["phase"]["confirmed"])
                    expected = 2 if mode == "explicit-resend" else 1
                    assert len(api.posts) == expected
                    if mode == "pagination":
                        await expect(section.locator(".evaluationItem")).to_have_count(21)
                        await section.get_by_role(
                            "button", name=labels["loadMore"], exact=True
                        ).click()
                        await expect(section.locator(".evaluationItem")).to_have_count(26)
                        await expect(
                            section.get_by_role("button", name=labels["loadMore"], exact=True)
                        ).to_have_count(0)
                        assert len(api.pages_read) == 2 and api.pages_read[1]
                    else:
                        await expect(section.locator(".evaluationItem")).to_have_count(1)
                    if mode == "success":
                        await section.locator(".modalDialog").screenshot(
                            path=str(output / f"{name}-evaluation.png")
                        )
                        await section.locator(".modalBody").evaluate(
                            "element => { element.scrollTop = 0 }"
                        )
                        await page.screenshot(path=str(output / f"{name}-evaluation-viewport.png"))
                        await page.reload()
                        await open_evaluation(page, catalog)
                        await expect(intent).to_have_count(0)
                        assert len(api.posts) == 1
                elif mode in ("refused", "forbidden"):
                    await expect(intent).to_contain_text(labels["phase"]["refused"])
                    await expect(section).to_contain_text(
                        labels["failures"]["invalidRevision" if mode == "refused" else "forbidden"]
                    )
                    if mode == "refused":
                        await intent.get_by_role(
                            "button", name=labels["editRejected"], exact=True
                        ).click()
                        await expect(intent).to_have_count(0)
                    else:
                        await expect_draft_locked(section)
                elif mode in (
                    "conflict",
                    "bad-receipt",
                    "bad-actor",
                    "storage",
                    "cancel-post",
                    "timeout-post",
                    "deadline-post",
                ):
                    await expect(intent).to_contain_text(labels["phase"]["unknown"])
                    await expect_draft_locked(section)
                    assert len(api.posts) == 1
        if mode in ("success", "revisions"):
            raw = page.locator(".rawResultBody")
            await expect(raw).to_have_count(1)
            assert json.loads(await raw.text_content()) == DATA
            before = api.detail_reads
            await page.reload()
            await open_evaluation(page, catalog)
            assert api.detail_reads > before
            assert json.loads(await page.locator(".rawResultBody").text_content()) == DATA
            await expect(section.locator(".evaluationItem")).to_have_count(1)
            assert len(api.posts) == 1
        await settle(page)
        await privacy(page)
        assert PRIVATE not in await page.locator("body").inner_text()
        assert DATA == {"summary": "Original result", "nullable": None, "a/b": {"~value": {"a": 1}}}
        assert not errors and not api.unexpected and not api.failures, (
            errors,
            api.unexpected,
            api.failures,
        )
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert len(context.pages) == 1
        if mode in ("revisions", "cancel-post", "manual", "archived", "conflict"):
            target = (
                section.locator(".evaluationHistory .evaluationRevision").first
                if mode == "revisions"
                else intent
            )
            await target.evaluate("element => element.scrollIntoView({block:'start'})")
            await page.screenshot(path=str(output / f"{name}-evaluation-viewport.png"))
        await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
        print(f"PASS {name}", flush=True)
    except Exception:
        await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        print("Fixture failures:", errors, api.unexpected, api.failures, flush=True)
        raise
    finally:
        for gate in (
            api.post_gate,
            api.get_gate,
            api.history_gate,
            api.event_gate,
            api.detail_gate,
        ):
            gate.release.set()
        await context.close()


async def check(url: str, output: Path, cases: list[str] | None) -> None:
    """各 case を独立 context で読み、所有した browser を必ず終了する。"""
    output.mkdir(parents=True, exist_ok=False)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            if cases:
                for mode in cases:
                    await scenario(browser, url, mode, "zh", 390, output)
            else:
                for language in ("zh", "ja", "en"):
                    for width in (390, 1440):
                        await scenario(browser, url, "success", language, width, output)
                    for mode in ("refused", "forbidden", "archived", "manual"):
                        await scenario(browser, url, mode, language, 390, output)
                for mode in CASES:
                    await scenario(browser, url, mode, "zh", 1440, output)
        finally:
            await browser.close()


def main() -> None:
    """明示的な自有 loopback mock harness と新しい外部出力先だけを受理する。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--case",
        action="append",
        choices=("success", "refused", "forbidden", "archived", "manual", *CASES),
    )
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
    asyncio.run(check(args.url, args.output, args.case))


if __name__ == "__main__":
    main()
