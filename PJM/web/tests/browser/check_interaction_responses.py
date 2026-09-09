"""実 Workspace の普通回答を全面 mock HTTP で検証し、実 DB の証拠とは分ける。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from check_accounts import ACTOR, CSRF, OTHER, ResponseGate
from check_run_submission import OTHER_PROJECT, PROJECT, TASK, task_catalog
from playwright.async_api import Browser, Error, Locator, Page, Route, async_playwright, expect

RUN = "00000000-0000-4000-8000-000000000010"
OTHER_RUN = "00000000-0000-4000-8000-000000000011"
PROJECT_RUN = "00000000-0000-4000-8000-000000000012"
INTERACTION = "00000000-0000-4000-8000-000000000110"
OTHER_INTERACTION = "00000000-0000-4000-8000-000000000111"
SEGMENT = "00000000-0000-4000-8000-000000000080"
NEXT_SEGMENT = "00000000-0000-4000-8000-000000000081"
RESPONSE = "00000000-0000-4000-8000-000000000120"
NOW = "2026-09-09T00:00:00Z"
ANSWER = "Browser original answer; retain this exact intent"
EDITED = "Browser later editable draft; never reuse as original"
PRIVATE = "Fixture private refusal detail must not be rendered"
DETAIL_FILE = Path(__file__).resolve().parents[3] / "contracts/examples/run-detail.v1.json"
CONTRACTS = DETAIL_FILE.parent.parent
DETAIL_SCHEMA = json.loads((CONTRACTS / "runs/detail/v1.schema.json").read_text())
RESPONSE_SCHEMA = json.loads(
    (CONTRACTS / "runs/interaction-response/v1/response.schema.json").read_text()
)


def assert_public_shape(value: dict, schema: dict) -> None:
    """変更対象の whitelist/enum を実 Schema と照合し、汎用 validator を複製しない。"""

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) <= set(value) <= set(schema["properties"])
    for key, rule in schema["properties"].items():
        if key in value and "enum" in rule:
            assert value[key] in rule["enum"], (key, value[key])


def assert_detail_shape(detail: dict) -> None:
    """成功用 mock の現行22項目と質問/継続区間の公開形状を毎回検査する。"""

    assert_public_shape(detail, DETAIL_SCHEMA)
    assert len(detail) == 22
    for segment in detail["segments"]:
        assert_public_shape(segment, DETAIL_SCHEMA["properties"]["segments"]["items"])
        assert type(segment["segment_no"]) is int and segment["segment_no"] > 0
    question_schema = DETAIL_SCHEMA["properties"]["interactions"]["items"]
    for question in detail["interactions"]:
        assert_public_shape(question, question_schema)
        assert type(question["version"]) is int and question["version"] > 0
        if question["response"] is not None:
            assert_public_shape(
                question["response"], question_schema["properties"]["response"]["oneOf"][1]
            )


def interaction(identity: str = INTERACTION, kind: str = "CHOICE") -> dict:
    """推薦と非必須は回答の代用にならない通常質問を作る。"""

    return {
        "interaction_id": identity,
        "run_segment_id": SEGMENT,
        "agent_session_id": "00000000-0000-4000-8000-000000000090",
        "interaction_type": kind,
        "prompt": {
            "prompt": "Choose the evidence scope for this review",
            "rationale": "The original question remains authoritative.",
            "impact": "The answer continues this run without changing permissions.",
            "allow_multiple": False,
        },
        "options": [
            {
                "key": key,
                "label": f"Option {key}: " + "long-readable-label-" * 5,
                "description": "Explain the evidence scope without hidden instructions. " * 2,
                "recommended": key == "alpha",
            }
            for key in ("alpha", "beta", "gamma")
        ],
        "required": False,
        "expires_at": "2099-01-01T00:00:00Z",
        "status": "OPEN",
        "version": 7,
        "continuation_mode": "RESUME",
        "checkpoint_checksum": "sha256:" + "a" * 64,
        "change_proposal_id": None,
        "response": None,
        "created_at": NOW,
    }


class InteractionApi:
    """観測・疑似 commit・応答喪失を分離し、未知の HTTP は必ず拒否する。"""

    def __init__(self, url: str) -> None:
        """実 credential や DB を使わず case ごとの認証と原回答を保持する。"""

        address = urlsplit(url)
        self.origin = f"{address.scheme}://{address.netloc}"
        self.prefix = "/projectmind/api/v1/"
        self.actor, self.csrf = ACTOR, CSRF
        template = json.loads(DETAIL_FILE.read_text())
        self.details = {}
        for run_id, project_id in (
            (RUN, PROJECT),
            (OTHER_RUN, PROJECT),
            (PROJECT_RUN, OTHER_PROJECT),
        ):
            self.details[run_id] = {
                **deepcopy(template),
                "run_id": run_id,
                "project_id": project_id,
                "task_id": TASK,
                "status": "WAITING_FOR_INPUT",
                "row_version": 7,
                "input": {},
                "selected_sources": {},
                "result": None,
                "tool_calls": [],
                "evidence": [],
                "attempts": [],
                "sessions": [],
                "interactions": [interaction()],
            }
            self.details[run_id]["segments"][0].update(status="WAITING", finished_at=None)
        self.posts: list[dict] = []
        self.reads: list[str] = []
        self.unexpected: list[str] = []
        self.failures: list[str] = []
        self.write_modes: list[str] = ["success"]
        self.detail_mode = "success"
        self.write_gate: ResponseGate | None = None
        self.detail_gate: ResponseGate | None = None
        self.commit_before_release = False
        self.saved: dict[tuple[str, str], dict] = {}
        self.sse_gates: list[ResponseGate] = []
        self.sse_received = asyncio.Event()
        self.gates: list[ResponseGate] = []
        self.received = asyncio.Event()
        self.output: Path | None = None
        self.case_name = ""

    def continue_run(
        self, run_id: str, trigger_type: str, trigger_ref: str, question: dict
    ) -> dict:
        """期限切れも回答受理も fake 正本に独立した継続 Segment を持つ。"""

        detail = self.details[run_id]
        detail.update(status="QUEUED", row_version=8)
        detail["segments"][0].update(status="COMPLETED", finished_at=NOW)
        if not any(item["run_segment_id"] == NEXT_SEGMENT for item in detail["segments"]):
            detail["segments"].append(
                {
                    **deepcopy(detail["segments"][0]),
                    "run_segment_id": NEXT_SEGMENT,
                    "segment_no": 2,
                    "trigger_type": trigger_type,
                    "trigger_ref": trigger_ref,
                    "status": "CREATED",
                    "continuation_mode": "RESUME",
                    "parent_agent_session_id": question["agent_session_id"],
                    "task_brief_checksum": None,
                    "started_at": None,
                    "finished_at": None,
                    "created_at": NOW,
                }
            )
        segment = next(
            item for item in detail["segments"] if item["run_segment_id"] == NEXT_SEGMENT
        )
        assert segment["parent_agent_session_id"] == question["agent_session_id"]
        assert segment["task_brief_checksum"] is None
        assert segment["trigger_type"] == trigger_type and segment["trigger_ref"] == trigger_ref
        assert_detail_shape(detail)
        return segment

    def run(self, run_id: str) -> dict:
        """Run detail と同一 fake 正本から snapshot を返す。"""

        detail = self.details[run_id]
        return {
            **{
                key: detail[key]
                for key in (
                    "run_id",
                    "project_id",
                    "task_id",
                    "status",
                    "row_version",
                    "created_at",
                )
            },
            "idempotent_replay": False,
        }

    async def problem(self, route: Route, status: int, code: str) -> None:
        """既知 code と任意内部文言を分け、画面側の安全な分類を検査する。"""

        await route.fulfill(
            status=status,
            json={"status": status, "code": code, "detail": PRIVATE, "title": "Fixture refusal"},
        )

    async def wait_gate(self, gate: ResponseGate | None) -> None:
        """受信と解放を明示し、sleep による時序推測を使わない。"""

        if gate is not None:
            if gate not in self.gates:
                self.gates.append(gate)
            gate.received.set()
            await asyncio.wait_for(gate.release.wait(), 45)

    async def route(self, route: Route) -> None:
        """静的 source は自有 Vite のみ許可し、API と native SSE を全面 mock する。"""

        request = route.request
        address = urlsplit(request.url)
        if f"{address.scheme}://{address.netloc}" != self.origin:
            self.unexpected.append(request.url)
            await route.abort()
            return
        if not address.path.startswith(self.prefix):
            if request.method == "GET" and not address.path.startswith("/projectmind/api/"):
                await route.continue_()
            else:
                self.unexpected.append(f"{request.method} {request.url}")
                await route.abort()
            return
        parts = address.path.removeprefix(self.prefix).split("/")
        try:
            if request.method == "GET":
                self.reads.append("/".join(parts))
                if (
                    len(parts) == 3
                    and parts[0] == "projects"
                    and parts[1] in (PROJECT, OTHER_PROJECT)
                ):
                    if parts[2] == "tasks":
                        await route.fulfill(json=task_catalog())
                        return
                    if parts[2] == "modules":
                        await route.fulfill(json={"modules": []})
                        return
                    if parts[2] == "schedules" and address.query == "limit=100&offset=0":
                        await route.fulfill(json={"schedules": [], "total": 0, "limit": 100, "offset": 0})
                        return
                if len(parts) == 2 and parts[0] == "runs" and parts[1] in self.details:
                    await route.fulfill(json=self.run(parts[1]))
                    return
                if (
                    len(parts) == 3
                    and parts[0] == "runs"
                    and parts[1] in self.details
                    and parts[2] == "events"
                ):
                    gate = ResponseGate()
                    self.sse_gates.append(gate)
                    self.sse_received.set()
                    await self.wait_gate(gate)
                    record = self.run(parts[1])
                    sequence = int(parse_qs(address.query).get("after", ["0"])[0]) + 1
                    event = {
                        "run_id": parts[1],
                        "sequence": sequence,
                        "event_type": "RUN_SNAPSHOT",
                        "run_attempt_id": None,
                        "agent_session_id": None,
                        "occurred_at": NOW,
                        "trace_id": None,
                        "payload": {
                            "status": record["status"],
                            "row_version": record["row_version"],
                        },
                    }
                    await route.fulfill(
                        content_type="text/event-stream",
                        body=f"id: {sequence}\nevent: run.snapshot\ndata: {json.dumps(event)}\n\n",
                    )
                    gate.returned.set()
                    return
                if (
                    len(parts) == 5
                    and parts[0] == "projects"
                    and parts[2] == "runs"
                    and parts[3] in self.details
                ):
                    assert parts[1] == self.details[parts[3]]["project_id"]
                    if parts[4] == "detail":
                        detail, mode, gate = (
                            deepcopy(self.details[parts[3]]),
                            self.detail_mode,
                            self.detail_gate,
                        )
                        await self.wait_gate(gate)
                        if mode == "success":
                            assert_detail_shape(detail)
                            await route.fulfill(json=detail)
                        elif mode == "invalid":
                            await route.fulfill(json={"invalid": True})
                        elif mode == "drop":
                            await route.abort("connectionreset")
                        else:
                            await self.problem(
                                route,
                                int(mode),
                                "run_not_found" if mode == "404" else "fixture_read_failed",
                            )
                        if gate:
                            gate.returned.set()
                        return
                    if parts[4] == "evaluations":
                        await route.fulfill(json={"evaluations": []})
                        return
            if (
                request.method == "POST"
                and len(parts) == 7
                and parts[0] == "projects"
                and parts[2] == "runs"
                and parts[4] == "interactions"
                and parts[6] == "responses"
            ):
                await self.respond(route, parts)
                return
            self.unexpected.append(f"{request.method} {request.url}")
            await route.abort()
        except Error:
            # 離頁後は native HTTP transport が閉じるが、疑似 commit を取り消さない。
            return
        except Exception as error:
            self.failures.append(f"{type(error).__name__}: {error}")
            await route.abort()

    async def respond(self, route: Route, parts: list[str]) -> None:
        """原 key/body/actor と commit を記録し、再送で別 Segment を作らない。"""

        request = route.request
        assert parts[3] in self.details and self.details[parts[3]]["project_id"] == parts[1]
        question = next(
            item
            for item in self.details[parts[3]]["interactions"]
            if item["interaction_id"] == parts[5]
        )
        assert request.headers.get("origin") == self.origin
        assert request.headers.get("x-csrf-token") == self.csrf
        key = request.headers.get("idempotency-key")
        assert key and len(key) <= 128
        body = request.post_data_json
        assert set(body) == {"interaction_version", "response"}
        record = {
            "key": key,
            "body": request.post_data,
            "actor": self.actor,
            "project": parts[1],
            "run": parts[3],
            "interaction": parts[5],
            "csrf": self.csrf,
        }
        self.posts.append(record)
        self.received.set()
        mode = self.write_modes.pop(0) if self.write_modes else "success"
        gate = self.write_gate
        scope = (parts[3], parts[5])

        def commit() -> dict:
            """同一 scope の原回答を再利用し、後からの draft は取り込まない。"""

            existing = self.saved.get(scope)
            if existing is not None:
                assert all(
                    existing["request"][field] == record[field]
                    for field in ("key", "body", "actor")
                )
                assert question["version"] == existing["closed_version"]
                assert self.details[parts[3]]["segments"][-1] == existing["segment"]
                return {
                    **existing["receipt"],
                    "idempotent_replay": True,
                    "status": self.details[parts[3]]["status"],
                    "row_version": self.details[parts[3]]["row_version"],
                }
            result = {
                "project_id": parts[1],
                "run_id": parts[3],
                "interaction_id": parts[5],
                "response_id": RESPONSE,
                "run_segment_id": NEXT_SEGMENT,
                "segment_no": 2,
                "continuation_mode": "RESUME",
                "status": "QUEUED",
                "row_version": 8,
                "idempotent_replay": False,
            }
            question.update(
                status="RESPONDED",
                version=question["version"] + 1,
                response={
                    "response_id": RESPONSE,
                    "actor_id": record["actor"],
                    "interaction_version": body["interaction_version"],
                    "response": body["response"],
                    "created_at": NOW,
                },
            )
            assert question["version"] == body["interaction_version"] + 1
            segment = self.continue_run(parts[3], "INTERACTION_RESPONSE", RESPONSE, question)
            self.saved[scope] = {
                "request": record,
                "receipt": result,
                "closed_version": question["version"],
                "segment": deepcopy(segment),
            }
            return result

        result = (
            commit()
            if self.commit_before_release and not mode.isdigit() and mode != "archived"
            else None
        )
        try:
            await self.wait_gate(gate)
            if mode.isdigit() or mode == "archived":
                status = 409 if mode == "archived" else int(mode)
                code = {
                    401: "authentication_required",
                    403: "csrf_rejected",
                    404: "run_not_found",
                    409: "interaction_conflict",
                    410: "interaction_expired",
                    422: "interaction_response_invalid",
                }.get(status, "fixture_failed")
                if mode == "archived":
                    code = "project_archived"
                if status == 410:
                    previous_version = question["version"]
                    question.update(status="EXPIRED", response=None, version=previous_version + 1)
                    assert (
                        question["version"] == previous_version + 1 and question["response"] is None
                    )
                    self.continue_run(parts[3], "INTERACTION_TIMEOUT", parts[5], question)
                elif status == 409 and mode != "archived":
                    previous_version = question["version"]
                    question.update(
                        status="RESPONDED",
                        version=previous_version + 1,
                        response={
                            "response_id": RESPONSE,
                            "actor_id": OTHER,
                            "interaction_version": previous_version,
                            "response": {"selected_option_keys": ["alpha"]},
                            "created_at": NOW,
                        },
                    )
                    assert question["version"] == question["response"]["interaction_version"] + 1
                    self.continue_run(parts[3], "INTERACTION_RESPONSE", RESPONSE, question)
                await self.problem(route, status, code)
                return
            if mode == "drop-uncommitted":
                await route.abort("connectionreset")
                return
            result = result or commit()
            if mode == "drop":
                await route.abort("connectionreset")
                return
            status = 200 if result["idempotent_replay"] else 201
            headers = {"Idempotent-Replay": str(result["idempotent_replay"]).lower()}
            assert_public_shape(result, RESPONSE_SCHEMA)
            assert result["run_segment_id"] == NEXT_SEGMENT and result["segment_no"] == 2
            assert status == (200 if headers["Idempotent-Replay"] == "true" else 201)
            if mode == "invalid":
                result = {"invalid": True}
            elif mode == "wrong-project":
                result = {**result, "project_id": OTHER_PROJECT}
            elif mode == "wrong-interaction":
                result = {**result, "interaction_id": OTHER_INTERACTION}
            elif mode == "extra":
                result = {**result, "unexpected": "fixture"}
            elif mode == "wrong-version":
                result = {**result, "row_version": 1.5}
            elif mode == "wrong-status":
                status = 202
            elif mode == "wrong-replay":
                headers = {"Idempotent-Replay": "true"}
            elif mode == "missing-replay":
                headers = {}
            elif mode == "wrong-200":
                status = 200
            elif mode == "wrong-201":
                result = {**result, "idempotent_replay": True}
                headers = {"Idempotent-Replay": "true"}
            elif mode == "non-json":
                await route.fulfill(
                    status=status,
                    content_type="text/plain",
                    body="fixture-not-json",
                    headers=headers,
                )
                return
            elif mode == "empty":
                await route.fulfill(status=204)
                return
            await route.fulfill(status=status, json=result, headers=headers)
        finally:
            if gate:
                gate.returned.set()

    def release_all(self) -> None:
        """この case が所有する待機だけを解放する。"""

        for gate in [*self.gates, self.write_gate, self.detail_gate]:
            if gate is not None:
                gate.release.set()


async def settle(page: Page) -> None:
    """React の描画 frame を待ち、任意 sleep を状態判定に使わない。"""

    await page.evaluate(
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )


def card(page: Page, identity: str = INTERACTION) -> Locator:
    """同じ質問を pending/audit の重複 card から選ばない。"""

    return page.locator(f'[data-interaction-card="{identity}"]')


async def phase(page: Page, value: str) -> None:
    """tab を隠した場合も、原要求の状態が owner に残ることを確認する。"""

    await expect(page.locator(f'[data-interaction-phase="{value}"]')).to_have_count(1)


async def answer(page: Page, *, choice: bool = True) -> None:
    """推薦ではない明示選択と本文を作る。"""

    if choice:
        await card(page).locator('[data-interaction-option="beta"]').check()
    await card(page).locator("[data-interaction-text]").fill(ANSWER)


async def submit(page: Page) -> None:
    """同 tick 二回 submit を配送し、state disabled 以外の同期 guard を検証する。"""

    await (
        card(page)
        .locator("[data-interaction-form]")
        .evaluate("""form => {
      for (let i=0; i<2; i++) {
        form.dispatchEvent(new Event('submit', {bubbles:true,cancelable:true}));
      }
    }""")
    )


async def confirm_original(page: Page) -> None:
    """利用者の明示操作だけが原 payload/key の POST を許可する。"""

    await page.locator("[data-interaction-confirm-original]").focus()
    await page.keyboard.press("Enter")


async def basic(page: Page, api: InteractionApi, labels: dict) -> None:
    """三語の質問表示・推奨非選択・同 tick 受付・新 Segment の受理を確認する。"""

    await expect(card(page).locator("input:checked")).to_have_count(0)
    assert not api.posts
    await expect(card(page)).to_contain_text(labels["runResult"]["recommendedSuffix"])
    if api.output:
        await page.screenshot(
            path=str(api.output / f"QUESTION-{api.case_name}.png"), full_page=True
        )
    await answer(page)
    await submit(page)
    await phase(page, "confirmed")
    await expect(card(page).locator(".segmentHeading")).to_contain_text("v8")
    assert len(api.posts) == len(api.saved) == 1
    payload = json.loads(api.posts[0]["body"])
    assert payload == {
        "interaction_version": 7,
        "response": {"selected_option_keys": ["beta"], "text": ANSWER},
    }
    await expect(page.locator('[data-interaction-phase="confirmed"] h4')).to_be_focused()
    await expect(page.locator("[data-interaction-receipt]")).to_contain_text(NEXT_SEGMENT)


async def unknown_confirmation(page: Page, api: InteractionApi, _: dict) -> None:
    """commit 済み/未到達の双方を同一原要求で手動確認し、新しい key を作らない。"""

    await answer(page)
    await submit(page)
    await phase(page, "unknown")
    original = deepcopy(api.posts[0])
    field = card(page).locator("[data-interaction-text]")
    if await field.is_editable():
        await field.fill(EDITED)
    await settle(page)
    assert len(api.posts) == 1
    await confirm_original(page)
    await phase(page, "confirmed")
    assert len(api.posts) == 2 and api.posts[1] == original
    assert len(api.saved) == 1
    assert api.details[RUN]["interactions"][0]["version"] == 8
    assert api.details[RUN]["interactions"][0]["response"]["interaction_version"] == 7


async def known_rejection(page: Page, api: InteractionApi, _: dict) -> None:
    """拒否と既に起きた競合/期限切れを区別し、勝手に再送しない。"""

    mode = api.write_modes[0]
    await answer(page)
    await submit(page)
    await phase(page, {"409": "conflict", "410": "expired"}.get(mode, "rejected"))
    await settle(page)
    assert len(api.posts) == 1
    assert PRIVATE not in await page.locator("body").inner_text()
    if mode in {"409", "410"}:
        await page.locator("[data-interaction-read-original]").click()
        await expect(page.locator("[data-interaction-facts]")).to_be_visible()
        assert len(api.posts) == 1
        if mode == "409":
            await expect(page.locator("[data-interaction-facts]")).to_contain_text("alpha")
        else:
            assert api.details[RUN]["interactions"][0]["response"] is None
    if mode == "401":
        assert await page.evaluate("window.interactionSessionEvents") == 1
    if mode == "archived":
        assert len(api.details[RUN]["segments"]) == 1
        assert api.details[RUN]["interactions"][0]["version"] == 7
        await expect(page.locator("[data-interaction-confirm-original]")).to_have_count(0)
        await expect(page.locator("[data-interaction-edit]")).to_have_count(0)


async def refresh_preserves(page: Page, api: InteractionApi, _: dict) -> None:
    """detail/SSE と tab 移動が原要求を別 owner へ移したり捨てたりしない。"""

    await answer(page)
    await submit(page)
    await phase(page, "unknown")
    before = deepcopy(api.posts)
    await page.locator(".tabBar button").nth(0).click()
    await phase(page, "unknown")
    await page.locator(".tabBar button").nth(1).click()
    await phase(page, "unknown")
    await page.locator(".runPanel button").first.click()
    await expect(card(page)).to_have_count(1)
    await phase(page, "unknown")
    for gate in api.sse_gates:
        gate.release.set()
    await settle(page)
    await phase(page, "unknown")
    assert api.posts == before
    await confirm_original(page)
    await phase(page, "confirmed")
    assert len(api.posts) == 2 and api.posts[0] == api.posts[1]


async def lookup_failure(page: Page, api: InteractionApi, _: dict) -> None:
    """精確 GET が失敗しても原要求を失わず、GET 成功だけでは POST 成功にしない。"""

    await answer(page)
    await submit(page)
    await phase(page, "unknown")
    api.detail_mode = "500"
    await page.locator("[data-interaction-read-original]").click()
    await expect(page.locator("[data-interaction-read-failure]")).to_be_visible()
    await phase(page, "unknown")
    assert len(api.posts) == 1
    api.detail_mode = "success"
    await page.locator("[data-interaction-read-original]").click()
    await expect(page.locator("[data-interaction-facts]")).to_be_visible()
    await phase(page, "unknown")
    assert len(api.posts) == 1
    await confirm_original(page)
    await phase(page, "confirmed")


async def text_question(page: Page, api: InteractionApi, _: dict) -> None:
    """非必須の自由文でも空/空白を自動送信せず、明示入力を必要とする。"""

    field = card(page).locator("[data-interaction-text]")
    await expect(card(page).locator("[data-interaction-submit]")).to_be_disabled()
    await submit(page)
    await field.fill("   ")
    await submit(page)
    await settle(page)
    assert not api.posts
    await answer(page, choice=False)
    await submit(page)
    await phase(page, "confirmed")
    assert json.loads(api.posts[0]["body"])["response"] == {"text": ANSWER}


async def effect_readonly(page: Page, api: InteractionApi, _: dict) -> None:
    """歴史 EFFECT_APPROVAL の普通回答を禁止し、Proposal を偽装しない。"""

    await expect(card(page).locator("[data-interaction-effect-readonly]")).to_be_visible()
    await expect(card(page).locator("[data-interaction-form]")).to_have_count(0)
    assert not api.posts


async def multiple_choice(page: Page, api: InteractionApi, _: dict) -> None:
    """鍵盤選択の順序を含む原本文を、再確認で正規化し直さない。"""

    for option in ("gamma", "beta"):
        await card(page).locator(f'[data-interaction-option="{option}"]').focus()
        await page.keyboard.press("Space")
    await submit(page)
    await phase(page, "unknown")
    assert json.loads(api.posts[0]["body"])["response"] == {
        "selected_option_keys": ["gamma", "beta"]
    }
    await card(page).locator('[data-interaction-option="alpha"]').check()
    await confirm_original(page)
    await phase(page, "confirmed")
    assert len(api.posts) == 2 and api.posts[0] == api.posts[1]


async def validation_edit(page: Page, api: InteractionApi, _: dict) -> None:
    """初回の既知422だけを編集に戻し、新しい明示回答に新しいkeyを割り当てる。"""

    await answer(page)
    await submit(page)
    await phase(page, "rejected")
    await page.locator("[data-interaction-edit]").click()
    await card(page).locator("[data-interaction-text]").fill(EDITED)
    await submit(page)
    await phase(page, "confirmed")
    assert len(api.posts) == 2
    assert api.posts[0]["key"] != api.posts[1]["key"]
    assert json.loads(api.posts[1]["body"])["response"]["text"] == EDITED


async def unknown_then_rejected(page: Page, api: InteractionApi, labels: dict) -> None:
    """不明の後の拒否を初回不成立と誤認せず、原要求と警告を保持する。"""

    status = api.write_modes[1]
    await answer(page)
    await submit(page)
    await phase(page, "unknown")
    await confirm_original(page)
    await phase(page, "rejected")
    assert len(api.posts) == 2 and api.posts[0] == api.posts[1]
    await expect(page.locator("[data-interaction-intent]")).to_contain_text(
        labels["interactionResponse"]["unknownHint"]
    )
    await expect(page.locator("[data-interaction-edit]")).to_have_count(0)
    if status == "403":
        await expect(page.locator("[data-interaction-confirm-original]")).to_have_count(0)
    else:
        await confirm_original(page)
        await phase(page, "confirmed")
        assert len(api.posts) == 3 and api.posts[0] == api.posts[2]


async def update_context(page: Page, values: dict) -> None:
    """fixture に Session/対象の明示変更だけを入力し、実 Workspace を再描画する。"""

    await page.evaluate("value => window.updateSubmissionTestContext(value)", values)
    await settle(page)


async def late_context(page: Page, api: InteractionApi, _: dict) -> None:
    """AbortSignal を無視する旧結果が、対象変更/ABA後の新 owner を汚染しない。"""

    dimension = api.case_name.split("-")[1]
    aba = "-aba-" in api.case_name
    gate = ResponseGate()
    api.write_gate = gate
    await answer(page)
    await submit(page)
    await asyncio.wait_for(gate.received.wait(), 5)
    await phase(page, "sending")
    changes = {
        "actor": {"actorId": OTHER},
        "csrf": {"csrfToken": "d" * 32},
        "project": {"projectId": OTHER_PROJECT, "initialRunId": PROJECT_RUN},
        "run": {"initialRunId": OTHER_RUN},
        "unmount": {"screen": "tasks"},
    }
    await update_context(page, changes[dimension])
    if dimension == "unmount":
        await expect(card(page)).to_have_count(0)
    else:
        await expect(card(page)).to_be_visible()
        await expect(page.locator("[data-interaction-intent]")).to_have_count(0)
    if aba or dimension == "unmount":
        await update_context(
            page,
            {
                "actorId": ACTOR,
                "csrfToken": CSRF,
                "projectId": PROJECT,
                "initialRunId": RUN,
                "screen": "workspace",
            },
        )
        await expect(card(page)).to_be_visible()
    await card(page).locator("[data-interaction-text]").fill(EDITED)
    api.write_gate = None
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 5)
    await settle(page)
    await expect(page.locator("[data-interaction-intent]")).to_have_count(0)
    await expect(card(page).locator("[data-interaction-text]")).to_have_value(EDITED)
    assert await page.evaluate("window.interactionSessionEvents") == 0
    assert len(api.posts) == 1


async def sending_refresh(page: Page, api: InteractionApi, _: dict) -> None:
    """送信中の同Run detail再読込とOPEN→settledを越えて一つのownerを保つ。"""

    gate = ResponseGate()
    api.write_gate, api.commit_before_release = gate, True
    await answer(page)
    await submit(page)
    await asyncio.wait_for(gate.received.wait(), 5)
    await phase(page, "sending")
    await page.locator(".runPanel button").first.click()
    await expect(card(page)).to_have_count(1)
    await phase(page, "sending")
    await page.locator(".tabBar button").nth(0).click()
    focus = page.locator(".tabBar button").nth(0)
    await focus.focus()
    gate.release.set()
    await phase(page, "confirmed")
    await expect(focus).to_be_focused()
    await page.locator(".tabBar button").nth(1).click()
    await phase(page, "confirmed")
    assert len(api.posts) == 1


async def real_deadline(page: Page, api: InteractionApi, _: dict) -> None:
    """fake clockを使わず30秒の実時間を待ち、期限後の200を成功扱いしない。"""

    gate = ResponseGate()
    api.write_gate = gate
    await answer(page)
    started = time.monotonic()
    await submit(page)
    await asyncio.wait_for(gate.received.wait(), 5)
    await expect(page.locator('[data-interaction-phase="unknown"]')).to_have_count(1, timeout=35000)
    elapsed = time.monotonic() - started
    assert elapsed >= 29, f"Expected real 30s deadline, observed {elapsed:.3f}s"
    assert len(api.posts) == 1
    api.write_gate = None
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 5)
    await settle(page)
    await phase(page, "unknown")
    await confirm_original(page)
    await phase(page, "confirmed")
    assert len(api.posts) == 2 and api.posts[0] == api.posts[1]
    print(f"Real deadline elapsed: {elapsed:.3f}s; no virtual clock", flush=True)


async def interaction_aba(page: Page, api: InteractionApi, _: dict) -> None:
    """同Run内で消失/再出現した質問が旧transport結果を採用しない。"""

    old_question = deepcopy(api.details[RUN]["interactions"][0])
    gate = ResponseGate()
    api.write_gate = gate
    await answer(page)
    await submit(page)
    await asyncio.wait_for(gate.received.wait(), 5)
    api.details[RUN]["interactions"] = [interaction(OTHER_INTERACTION)]
    api.details[RUN]["row_version"] += 1
    await page.locator(".runPanel button").first.click()
    await expect(card(page, OTHER_INTERACTION)).to_be_visible()
    await phase(page, "unknown")
    await expect(card(page).locator("[data-interaction-confirm-original]")).to_be_disabled()
    api.details[RUN]["interactions"] = [old_question]
    api.details[RUN]["row_version"] += 1
    await page.locator(".runPanel button").first.click()
    await expect(card(page).locator("[data-interaction-confirm-original]")).to_be_enabled()
    api.write_gate = None
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 5)
    await settle(page)
    await phase(page, "unknown")
    assert await page.evaluate("window.interactionSessionEvents") == 0
    assert len(api.posts) == 1


async def read_failure_modes(page: Page, api: InteractionApi, _: dict) -> None:
    """原GETの認証/権限/不在/解析失敗は回答の不成立や成功を証明しない。"""

    mode = api.case_name.removeprefix("read-")
    await answer(page)
    await submit(page)
    await phase(page, "unknown")
    api.detail_mode = mode
    await page.locator("[data-interaction-read-original]").click()
    await expect(page.locator("[data-interaction-read-failure]")).to_be_visible()
    await phase(page, "rejected" if mode in {"401", "403", "404"} else "unknown")
    assert len(api.posts) == 1
    await expect(page.locator("[data-interaction-facts]")).to_have_count(0)
    assert await page.evaluate("window.interactionSessionEvents") == (1 if mode == "401" else 0)
    if mode in {"401", "403", "404"}:
        await expect(page.locator("[data-interaction-confirm-original]")).to_have_count(0)
        await expect(page.locator("[data-interaction-edit]")).to_have_count(0)
        await expect(page.locator("[data-interaction-intent]")).to_contain_text(ANSWER)


async def facts_refresh_gate(page: Page, api: InteractionApi, _: dict) -> None:
    """再読込開始後の古い成功factsを現在の成功として残さず、失敗も明示する。"""

    await answer(page)
    await submit(page)
    await phase(page, "unknown")
    await page.locator("[data-interaction-read-original]").click()
    await expect(page.locator("[data-interaction-facts]")).to_be_visible()
    gate = ResponseGate()
    api.detail_gate, api.detail_mode = gate, "500"
    await page.locator("[data-interaction-read-original]").click()
    await asyncio.wait_for(gate.received.wait(), 5)
    await expect(page.locator("[data-interaction-facts]")).to_have_count(0)
    await expect(page.locator("[data-interaction-read-original]")).to_be_disabled()
    gate.release.set()
    await expect(page.locator("[data-interaction-read-failure]")).to_be_visible()
    await phase(page, "unknown")
    assert len(api.posts) == 1


async def stale_receipt(page: Page, api: InteractionApi, labels: dict) -> None:
    """SSEで進んだRunを遅れた原受理snapshotで過去へ戻さない。"""

    gate = ResponseGate()
    api.write_gate, api.commit_before_release = gate, True
    await answer(page)
    await submit(page)
    await asyncio.wait_for(gate.received.wait(), 5)
    api.details[RUN].update(status="SUCCEEDED", row_version=12)
    for current in list(api.sse_gates):
        current.release.set()
    await expect(page.locator(".runPanel")).to_contain_text(
        labels["enums"]["runStatus"]["SUCCEEDED"]
    )
    gate.release.set()
    await phase(page, "confirmed")
    await expect(page.locator(".runPanel")).to_contain_text(
        labels["enums"]["runStatus"]["SUCCEEDED"]
    )
    assert len(api.posts) == 1


async def unknown_layout(page: Page, api: InteractionApi, labels: dict) -> None:
    """三語/狭幅の原対象・原本文・警告と明示確認を鍵盤だけで読める。"""

    await answer(page)
    await card(page).locator("[data-interaction-submit]").focus()
    await page.keyboard.press("Enter")
    await phase(page, "unknown")
    await expect(page.locator('[data-interaction-phase="unknown"] h4')).to_be_focused()
    await expect(page.locator("[data-interaction-intent]")).to_contain_text(ANSWER)
    await expect(page.locator("[data-interaction-intent]")).to_contain_text(PROJECT)
    await expect(page.locator("[data-interaction-intent]")).to_contain_text(
        labels["interactionResponse"]["confirmHint"]
    )
    assert CSRF not in await page.locator("body").inner_text()
    await page.locator("[data-interaction-read-original]").focus()
    await page.keyboard.press("Enter")
    await expect(page.locator("[data-interaction-facts]")).to_be_visible()
    await phase(page, "unknown")
    if api.output:
        await page.screenshot(path=str(api.output / f"FACTS-{api.case_name}.png"), full_page=True)
        await page.locator("[data-interaction-intent]").evaluate(
            "element => element.scrollIntoView({block:'start'})"
        )
        await page.screenshot(path=str(api.output / f"VIEWPORT-{api.case_name}.png"))
    assert len(api.posts) == 1


async def unicode_limit(page: Page, api: InteractionApi, labels: dict) -> None:
    """サーバーと同じUnicode codepoint上限を使い、UTF16長さで切り捨てない。"""

    field = card(page).locator("[data-interaction-text]")
    await card(page).locator('[data-interaction-option="beta"]').check()
    await field.fill("🧭" * 10001)
    await expect(card(page).locator("[data-interaction-submit]")).to_be_disabled()
    await expect(card(page)).to_contain_text(labels["interactionResponse"]["answerTooLong"])
    await submit(page)
    await settle(page)
    assert not api.posts
    await field.fill("🧭" * 10000)
    await submit(page)
    await phase(page, "confirmed")
    assert len(api.posts) == 1
    assert json.loads(api.posts[0]["body"])["response"]["text"] == "🧭" * 10000


async def double_confirmation(page: Page, api: InteractionApi, _: dict) -> None:
    """原回答確認の同tick二重clickも同じ同期門禁を通し、一回だけPOSTする。"""

    await answer(page)
    await submit(page)
    await phase(page, "unknown")
    await page.locator("[data-interaction-confirm-original]").evaluate(
        "button => {button.click(); button.click()}"
    )
    await phase(page, "confirmed")
    assert len(api.posts) == 2 and api.posts[0] == api.posts[1]


async def original_version(page: Page, api: InteractionApi, _: dict) -> None:
    """detail更新が原versionを改版せず、確認は原versionの競合として止まる。"""

    await answer(page)
    await submit(page)
    await phase(page, "unknown")
    api.details[RUN]["interactions"][0]["version"] = 8
    api.details[RUN]["row_version"] = 8
    await page.locator(".runPanel button").first.click()
    await expect(card(page).locator(".segmentHeading")).to_contain_text("v8")
    await phase(page, "unknown")
    await confirm_original(page)
    await phase(page, "conflict")
    assert len(api.posts) == 2 and api.posts[0] == api.posts[1]
    assert json.loads(api.posts[1]["body"])["interaction_version"] == 7


async def late_facts(page: Page, api: InteractionApi, _: dict) -> None:
    """精確GETも旧ownerのものとして隔離し、ABA後の草稿/認証を更新しない。"""

    _, dimension, mode = api.case_name.split("-")
    await answer(page)
    await submit(page)
    await phase(page, "unknown")
    gate = ResponseGate()
    api.detail_gate, api.detail_mode = gate, mode
    await page.locator("[data-interaction-read-original]").click()
    await asyncio.wait_for(gate.received.wait(), 5)
    api.detail_gate, api.detail_mode = None, "success"
    api.details[RUN].update(status="WAITING_FOR_INPUT", row_version=9, interactions=[interaction()])
    await update_context(
        page, {"actorId": OTHER} if dimension == "actor" else {"initialRunId": OTHER_RUN}
    )
    await expect(card(page)).to_be_visible()
    await update_context(page, {"actorId": ACTOR, "initialRunId": RUN})
    await expect(card(page)).to_be_visible()
    await card(page).locator("[data-interaction-text]").fill(EDITED)
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 5)
    await settle(page)
    await expect(page.locator("[data-interaction-facts]")).to_have_count(0)
    await expect(page.locator("[data-interaction-intent]")).to_have_count(0)
    await expect(card(page).locator("[data-interaction-text]")).to_have_value(EDITED)
    assert await page.evaluate("window.interactionSessionEvents") == 0
    assert len(api.posts) == 1


async def virtual_late_deadline(page: Page, api: InteractionApi, _: dict) -> None:
    """timer配送遅延を仮想monotonic時刻で検証し、実30秒証拠とは明確に区別する。"""

    gate = ResponseGate()
    read = api.case_name.endswith("read")
    if not read:
        api.write_gate = gate
    await answer(page)
    await submit(page)
    if read:
        await phase(page, "unknown")
        api.detail_gate = gate
        await page.locator("[data-interaction-read-original]").click()
    await asyncio.wait_for(gate.received.wait(), 5)
    await page.evaluate("""() => {
      const original = performance.now.bind(performance);
      Object.defineProperty(performance, 'now', {value: () => original() + 31000});
    }""")
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 5)
    if read:
        await expect(page.locator("[data-interaction-read-failure]")).to_be_visible()
        await expect(page.locator("[data-interaction-facts]")).to_have_count(0)
    await phase(page, "unknown")
    assert len(api.posts) == 1
    assert await page.evaluate("window.interactionSessionEvents") == 0
    print("Virtual +31s monotonic only; not evidence of 30s wall-clock waiting", flush=True)


async def release_snapshot(api: InteractionApi) -> None:
    """実EventSourceを通じたSnapshotだけでWorkspaceの自発detail読取を起こす。"""

    await asyncio.wait_for(api.sse_received.wait(), 5)
    for gate in list(api.sse_gates):
        gate.release.set()


async def workspace_read_failure(page: Page, api: InteractionApi, labels: dict) -> None:
    """カードのGETボタンを使わず、Workspaceの再読取拒否を原回答gateへ伝える。"""

    status = api.case_name.rsplit("-", 1)[1]
    await answer(page)
    await submit(page)
    await phase(page, "unknown")
    api.detail_mode = status
    api.details[RUN]["row_version"] = 9
    before = len(api.reads)
    await release_snapshot(api)
    await expect(page.locator(".workspaceResultMount > .error")).to_be_visible()
    assert len(api.reads) > before
    expected = "unknown" if status == "500" else "rejected"
    await phase(page, expected)
    await expect(page.locator(".runInteractions > h3")).to_have_text(
        labels["interactionResponse"]["recordsTitle"]
    )
    await expect(page.locator(".runInteractions")).not_to_contain_text(
        labels["runResult"]["pendingHint"]
    )
    await expect(page.locator("[data-interaction-intent]")).to_contain_text(ANSWER)
    await expect(page.locator("[data-interaction-intent]")).to_contain_text(
        labels["interactionResponse"]["unknownHint"]
    )
    assert len(api.posts) == 1
    assert await page.evaluate("window.interactionSessionEvents") == (1 if status == "401" else 0)
    if status == "500":
        await expect(page.locator("[data-interaction-confirm-original]")).to_be_enabled()
        api.detail_mode = "success"
        await confirm_original(page)
        await phase(page, "confirmed")
        assert len(api.posts) == 2 and api.posts[0] == api.posts[1]
    else:
        await expect(page.locator("[data-interaction-confirm-original]")).to_have_count(0)
        await expect(page.locator("[data-interaction-edit]")).to_have_count(0)


async def workspace_rejects_sending(page: Page, api: InteractionApi, labels: dict) -> None:
    """自発GETの拒否が在途送信を閉じ、古い受理/401が資格や会話を復活させない。"""

    status, mode = api.case_name.rsplit("-", 2)[1:]
    gate = ResponseGate()
    api.write_gate, api.commit_before_release = gate, mode == "success"
    await answer(page)
    await submit(page)
    await asyncio.wait_for(gate.received.wait(), 5)
    await phase(page, "sending")
    api.detail_mode = status
    api.details[RUN]["row_version"] = 9
    await release_snapshot(api)
    await phase(page, "rejected")
    await expect(page.locator("[data-interaction-confirm-original]")).to_have_count(0)
    await expect(page.locator("[data-interaction-intent]")).to_contain_text(
        labels["interactionResponse"]["unknownHint"]
    )
    expected_sessions = 1 if status == "401" else 0
    assert await page.evaluate("window.interactionSessionEvents") == expected_sessions
    gate.release.set()
    await asyncio.wait_for(gate.returned.wait(), 5)
    await settle(page)
    await phase(page, "rejected")
    await expect(page.locator("[data-interaction-confirm-original]")).to_have_count(0)
    assert await page.evaluate("window.interactionSessionEvents") == expected_sessions
    assert len(api.posts) == 1


def legacy_duplicate_options(api: InteractionApi) -> None:
    """異なる意味を持つ同keyの歴史投影を作り、原選択肢を合併/修正しない。"""

    options = api.details[RUN]["interactions"][0]["options"]
    options[0] = {**options[0], "key": "beta", "label": "Legacy scope A"}
    options[1] = {**options[1], "key": "beta", "label": "Legacy scope B"}


async def expect_legacy_options(page: Page, labels: dict) -> None:
    """両方の原文と説明を表示し、曖昧な新規答復formを作らない。"""

    await expect(page.locator("[data-interaction-duplicate-options]")).to_contain_text(
        labels["interactionResponse"]["duplicateOptions"]
    )
    options = page.locator("[data-interaction-readonly-options]")
    await expect(options.locator("li")).to_have_count(3)
    await expect(options).to_contain_text("beta · Legacy scope A")
    await expect(options).to_contain_text("beta · Legacy scope B")
    await expect(options).to_contain_text("Option gamma:")
    await expect(page.locator("[data-interaction-form]")).to_have_count(0)
    await expect(page.locator("[data-interaction-submit]")).to_have_count(0)


async def legacy_readonly(page: Page, api: InteractionApi, labels: dict) -> None:
    """歴史CHOICEの重複keyは読取専用で、推薦や同名による回答を合成しない。"""

    await expect_legacy_options(page, labels)
    await settle(page)
    assert not api.posts and not api.saved


async def legacy_pending_confirmation(page: Page, api: InteractionApi, labels: dict) -> None:
    """再読取への不正歴史投影を注入しても原要求は保持し、新規validatorで整形しない。"""

    await answer(page)
    await submit(page)
    await phase(page, "unknown")
    original = deepcopy(api.posts[0])
    legacy_duplicate_options(api)
    api.details[RUN]["row_version"] = 9
    await page.locator(".runPanel button").first.click()
    await expect_legacy_options(page, labels)
    await phase(page, "unknown")
    assert len(api.posts) == 1
    await expect(page.locator("[data-interaction-intent]")).to_contain_text(ANSWER)
    await confirm_original(page)
    await phase(page, "confirmed")
    await expect_legacy_options(page, labels)
    assert len(api.posts) == 2 and api.posts[1] == original
    assert len(api.saved) == 1 and api.details[RUN]["interactions"][0]["version"] == 8


async def exercise(
    browser: Browser,
    url: str,
    name: str,
    action: Callable[[Page, InteractionApi, dict], Awaitable[None]],
    *,
    setup: Callable[[InteractionApi], None] | None = None,
    language: str = "en",
    width: int = 1440,
    output: Path | None = None,
) -> None:
    """case を独立させ、HTTP/JS/Storage/横幅を検査して自分の待機を閉じる。"""

    api = InteractionApi(url)
    api.output, api.case_name = output, name
    if setup:
        setup(api)
    context = await browser.new_context(viewport={"width": width, "height": 1000}, locale=language)
    await context.route("**/*", api.route)
    page = await context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            errors.append(message.text)
            if message.type == "error" and not message.text.startswith("Failed to load resource:")
            else None
        ),
    )
    await page.add_init_script("""(() => {
      window.interactionSessionEvents = 0;
      window.addEventListener('interaction-session-expired', () => {
        window.interactionSessionEvents++;
      });
      window.interactionStorageWrites = [];
      const save = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key, value) {
        window.interactionStorageWrites.push({key, value}); return save.call(this, key, value);
      };
      const fetchOriginal = window.fetch.bind(window);
      window.fetch = (input, init) => fetchOriginal(input,
        init ? {...init, signal: undefined} : init);
    })();""")
    try:
        await page.goto(f"{url}?run={RUN}")
        await page.wait_for_function("Boolean(window.updateSubmissionTestContext)")
        await page.evaluate("language => window.updateSubmissionTestContext({language})", language)
        await expect(card(page)).to_be_visible()
        labels = await page.evaluate(
            """async language => {
          const {MESSAGES} = await import('/projectmind/src/lib/i18n/messages.ts');
          return {workspace: MESSAGES[language].workspace, runResult: MESSAGES[language].runResult,
            interactionResponse: MESSAGES[language].interactionResponse,
            enums: MESSAGES[language].enums};
        }""",
            language,
        )
        await action(page, api, labels)
        await settle(page)
        assert not api.unexpected, api.unexpected
        assert not api.failures, api.failures
        assert not errors, errors
        assert await page.evaluate("window.interactionStorageWrites") == []
        assert ANSWER not in page.url and EDITED not in page.url
        assert PRIVATE not in await page.locator("body").inner_text()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (
            "Page overflow"
        )
        if output:
            await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
        print(f"PASS {name}: {len(api.posts)} POST, {len(api.saved)} stored response", flush=True)
    except Exception:
        print(
            f"FAIL {name}: unexpected={api.unexpected}, mock={api.failures}, JS={errors}",
            flush=True,
        )
        if output:
            await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        raise
    finally:
        api.release_all()
        await context.close()


async def check(url: str, output: Path | None, only: str | None) -> None:
    """loopback fixture 以外を拒否し、同一 browser 内でも各 case の会話を分離する。"""

    address = urlsplit(url)
    if address.hostname not in {"127.0.0.1", "localhost", "::1"} or address.scheme != "http":
        raise ValueError("Only a loopback fixture URL is allowed")
    if not address.path.endswith("tests/browser/run-submission.html"):
        raise ValueError("Use the real Workspace run-submission fixture")
    if output:
        output.mkdir(parents=True, exist_ok=True)
    count = 0
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:

            async def run(name: str, action: Callable, **kwargs) -> None:
                """名前 filter で対象 case だけ実行し、実行数を正しく報告する。"""

                nonlocal count
                if only is None or only in name:
                    await exercise(browser, url, name, action, output=output, **kwargs)
                    count += 1

            for language in ("zh", "ja", "en"):
                for width in (390, 1440):
                    await run(f"basic-{language}-{width}", basic, language=language, width=width)
            for mode in (
                "drop",
                "drop-uncommitted",
                "invalid",
                "wrong-project",
                "wrong-interaction",
                "extra",
                "wrong-version",
                "wrong-status",
                "wrong-replay",
                "missing-replay",
                "wrong-200",
                "wrong-201",
                "non-json",
                "empty",
                "500",
            ):
                await run(
                    f"unknown-{mode}",
                    unknown_confirmation,
                    setup=lambda api, mode=mode: setattr(api, "write_modes", [mode, "success"]),
                )
            for status in (401, 403, 404, 409, 410, 422):
                await run(
                    f"rejected-{status}",
                    known_rejection,
                    setup=lambda api, status=status: setattr(api, "write_modes", [str(status)]),
                )
            await run(
                "rejected-archived",
                known_rejection,
                setup=lambda api: setattr(api, "write_modes", ["archived"]),
            )
            for name, action in (
                ("refresh-preserves", refresh_preserves),
                ("lookup-failure", lookup_failure),
            ):
                await run(
                    name, action, setup=lambda api: setattr(api, "write_modes", ["drop", "success"])
                )
            for kind in ("CLARIFICATION", "REVIEW", "EFFECT_APPROVAL"):

                def setup_kind(api: InteractionApi, kind: str = kind) -> None:
                    """普通自由文と旧承認型を同じdetail契約で供給する。"""

                    api.details[RUN]["interactions"] = [interaction(kind=kind)]

                await run(
                    f"kind-{kind}",
                    effect_readonly if kind == "EFFECT_APPROVAL" else text_question,
                    setup=setup_kind,
                )

            def setup_multiple(api: InteractionApi) -> None:
                """配列順序を観測できる複数選択を明示する。"""

                api.details[RUN]["interactions"][0]["prompt"]["allow_multiple"] = True
                api.write_modes = ["drop", "success"]

            await run("multiple-choice", multiple_choice, setup=setup_multiple)
            await run(
                "validation-edit",
                validation_edit,
                setup=lambda api: setattr(api, "write_modes", ["422", "success"]),
            )
            for status in ("403", "422"):
                await run(
                    f"uncertain-then-{status}",
                    unknown_then_rejected,
                    setup=lambda api, status=status: setattr(
                        api, "write_modes", ["drop", status, "success"]
                    ),
                )
            for dimension in ("actor", "csrf", "project", "run", "unmount"):
                for suffix, mode in (
                    ("late-200", "success"),
                    ("aba-401", "401"),
                    ("aba-200", "success"),
                    ("late-500", "500"),
                ):
                    await run(
                        f"context-{dimension}-{suffix}",
                        late_context,
                        setup=lambda api, mode=mode: setattr(api, "write_modes", [mode]),
                    )
            await run("sending-refresh", sending_refresh)
            for mode in ("success", "401", "500"):
                await run(
                    f"interaction-aba-{mode}",
                    interaction_aba,
                    setup=lambda api, mode=mode: setattr(api, "write_modes", [mode]),
                )
            for mode in ("401", "403", "404", "invalid", "drop"):
                await run(
                    f"read-{mode}",
                    read_failure_modes,
                    setup=lambda api: setattr(api, "write_modes", ["drop"]),
                )
            await run(
                "facts-refresh-gate",
                facts_refresh_gate,
                setup=lambda api: setattr(api, "write_modes", ["drop"]),
            )
            await run("stale-receipt", stale_receipt)
            await run("unicode-limit", unicode_limit)
            await run(
                "double-confirmation",
                double_confirmation,
                setup=lambda api: setattr(api, "write_modes", ["drop", "success"]),
            )
            await run(
                "original-version",
                original_version,
                setup=lambda api: setattr(api, "write_modes", ["drop-uncommitted", "409"]),
            )
            for dimension in ("actor", "run"):
                for mode in ("success", "401"):
                    await run(
                        f"facts-{dimension}-{mode}",
                        late_facts,
                        setup=lambda api: setattr(api, "write_modes", ["drop"]),
                    )
            for language in ("zh", "ja", "en"):
                for width in (390, 1440):
                    await run(
                        f"unknown-layout-{language}-{width}",
                        unknown_layout,
                        language=language,
                        width=width,
                        setup=lambda api: setattr(api, "write_modes", ["drop"]),
                    )
            await run("real-30s-deadline", real_deadline)
            for mode in ("success", "401", "read"):
                await run(
                    f"virtual-deadline-{mode}",
                    virtual_late_deadline,
                    setup=lambda api, mode=mode: setattr(
                        api, "write_modes", ["drop" if mode == "read" else mode]
                    ),
                )
            for status in ("401", "403", "404", "500"):
                await run(
                    f"workspace-read-{status}",
                    workspace_read_failure,
                    setup=lambda api: setattr(api, "write_modes", ["drop", "success"]),
                )
            for status in ("401", "403", "404"):
                for mode in ("success", "401"):
                    await run(
                        f"workspace-sending-{status}-{mode}",
                        workspace_rejects_sending,
                        setup=lambda api, mode=mode: setattr(api, "write_modes", [mode]),
                    )
            await run("legacy-duplicate-readonly", legacy_readonly, setup=legacy_duplicate_options)
            await run(
                "legacy-duplicate-original-confirmation",
                legacy_pending_confirmation,
                setup=lambda api: setattr(api, "write_modes", ["drop", "success"]),
            )
        finally:
            await browser.close()
    assert count > 0, "No matching browser case"
    print(f"Interaction browser checks passed: {count}; all business HTTP mocked", flush=True)


def main() -> None:
    """保存先は明示された外部 directory だけを利用する。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default="http://127.0.0.1:5203/projectmind/tests/browser/run-submission.html"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--only")
    options = parser.parse_args()
    asyncio.run(check(options.url, options.output, options.only))


if __name__ == "__main__":
    main()
