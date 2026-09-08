"""実 Workspace の原要求再送を、loopback Vite と全面 mock API だけで検証する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import Error, Page, Route, async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "00000000-0000-4000-8000-000000000020"
OTHER_PROJECT = "00000000-0000-4000-8000-000000000021"
VERSION = "00000000-0000-4000-8000-000000000061"
TASK = "00000000-0000-4000-8000-000000000030"


def task_catalog() -> dict:
    """API validator を通る通常 task。実 Skill/資源/credential は読み込まない。"""

    return {
        "tasks": [
            {
                "skill_id": VERSION,
                "skill_version_id": VERSION,
                "skill_key": "analysis-fixture",
                "skill_name": "Analysis fixture",
                "version": "1.0.0",
                "task_key": "analyze",
                "task_id": TASK,
                "capability": "analysis/v1",
                "title": "Analysis fixture",
                "task_type": "analysis",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "input_schema_checksum": "sha256:" + "1" * 64,
                "output_schema_checksum": "sha256:" + "2" * 64,
                "task_output_schema": None,
                "task_output_schema_checksum": None,
                "workflow": "default",
                "view": "standard",
                "default_view": "standard",
                "compatibility_level": "native",
                "tool_requirements": [],
                "published_at": "2026-09-08T00:00:00Z",
                "last_run": None,
                "readiness": {
                    "level": "RUNNABLE",
                    "requirements": [
                        {
                            "key": "docs",
                            "kind": "document",
                            "required": True,
                            "access": "read",
                            "status": "AVAILABLE",
                            "reason": "Fixture resource",
                            "capabilities": ["document.read/v1"],
                            "selection_guidance": None,
                            "candidates": [
                                {
                                    "key": "project-documents:all",
                                    "kind": "document",
                                    "provider": "project-documents",
                                    "label": "Fixture set",
                                },
                                {
                                    "key": "document:" + VERSION,
                                    "kind": "document",
                                    "provider": "project-documents",
                                    "label": "Fixture document",
                                },
                            ],
                        }
                    ],
                },
            }
        ]
    }


class ApiFixture:
    """同一要求の疑似 commit と応答喪失を分離し、二重 Run の発生を数える。"""

    def __init__(self, origin: str, actions: list[str]) -> None:
        """各 case の state を独立させ、外部/未定義 API への到達を失敗として残す。"""

        self.origin = origin
        self.actions = list(actions)
        self.posts: list[dict] = []
        self.runs: dict[tuple, dict] = {}
        self.unexpected: list[str] = []
        self.release = asyncio.Event()
        self.received = asyncio.Event()

    async def route(self, route: Route) -> None:
        """静的資産だけ Vite へ通し、API は fake から返す。"""

        request = route.request
        url = urlsplit(request.url)
        if f"{url.scheme}://{url.netloc}" != self.origin:
            self.unexpected.append(request.url)
            await route.abort()
            return
        if "/api/v1/" not in url.path:
            await route.continue_()
            return
        if request.method == "POST" and url.path.endswith("/task-runs"):
            await self.create(route)
        elif request.method == "GET" and url.path.endswith("/tasks"):
            await route.fulfill(json=task_catalog())
        elif request.method == "GET" and url.path.endswith("/modules"):
            await route.fulfill(json={"modules": []})
        elif request.method == "GET" and url.path.endswith("/detail"):
            record = next(
                item for item in self.runs.values() if item["run_id"] in url.path
            )
            detail = json.loads(
                (ROOT.parent / "contracts/examples/run-detail.v1.json").read_text()
            )
            detail.update(
                {
                    key: record[key]
                    for key in (
                        "run_id",
                        "project_id",
                        "task_id",
                        "status",
                        "row_version",
                    )
                }
            )
            await route.fulfill(json=detail)
        elif request.method == "GET" and url.path.endswith("/events"):
            record = next(
                item for item in self.runs.values() if item["run_id"] in url.path
            )
            event = {
                "run_id": record["run_id"],
                "run_attempt_id": None,
                "agent_session_id": None,
                "sequence": 1,
                "event_type": "RUN_SNAPSHOT",
                "occurred_at": record["created_at"],
                "payload": {"status": "SUCCEEDED", "row_version": 4},
                "trace_id": None,
            }
            await route.fulfill(
                content_type="text/event-stream",
                body="event: run.snapshot\ndata: " + json.dumps(event) + "\n\n",
            )
        elif request.method == "GET" and url.path.endswith("/evaluations"):
            await route.fulfill(json={"evaluations": []})
        else:
            self.unexpected.append(f"{request.method} {request.url}")
            await route.abort()

    async def create(self, route: Route) -> None:
        """API 応答の欠落は、既に commit した Run を消さない。"""

        request = route.request
        payload = request.post_data_json
        project = urlsplit(request.url).path.split("/projects/")[1].split("/")[0]
        key = request.headers["idempotency-key"]
        self.posts.append(
            {
                "project": project,
                "key": key,
                "body": request.post_data,
                "csrf": request.headers.get("x-csrf-token"),
            }
        )
        self.received.set()
        action = self.actions.pop(0) if self.actions else "success"
        if action in {"conflict", "reject", "forbidden"}:
            status = {"conflict": 409, "reject": 422, "forbidden": 403}[action]
            await route.fulfill(
                status=status,
                json={
                    "status": status,
                    "detail": "Fixture refusal",
                    "code": "idempotency_conflict"
                    if status == 409
                    else "fixture_refused",
                },
            )
            return
        scope = (project, payload["skill_version_id"], payload["task_key"], key)
        replay = scope in self.runs
        if not replay:
            self.runs[scope] = {
                "run_id": str(uuid4()),
                "project_id": project,
                "task_id": TASK,
                "status": "SUCCEEDED",
                "row_version": 4,
                "created_at": "2026-09-08T00:00:00Z",
                "idempotent_replay": False,
            }
        record = deepcopy(self.runs[scope])
        record["idempotent_replay"] = replay
        if action == "drop":
            await route.abort("connectionreset")
        elif action == "gateway":
            await route.fulfill(status=502, json={"detail": "Fixture gateway failure"})
        elif action == "non-json":
            await route.fulfill(
                status=201, content_type="text/plain", body="Fixture invalid response"
            )
        elif action == "contract":
            await route.fulfill(status=201, json={"invalid": "fixture"})
        elif action == "wrong-project":
            await route.fulfill(
                status=201, json={**record, "project_id": OTHER_PROJECT}
            )
        else:
            if action == "hold":
                await self.release.wait()
            try:
                await route.fulfill(status=200 if replay else 201, json=record)
            except Error:
                # abort/unmount 後でも server 側 commit は残る。閉じた request へ届かないのは正常。
                if action != "hold":
                    raise


async def open_form(page: Page, url: str) -> None:
    """実 catalog の取得を待ってから form を開く。"""

    await page.goto(url)
    await page.locator(".runLauncher > button").click()
    await expect(page.get_by_role("dialog")).to_be_visible()
    await page.locator('.documentSourceField select').first.select_option("ALL")
    await page.locator(".jsonInput").fill('{"query":"original","positions":[2,1]}')


async def start(page: Page) -> None:
    """native form の検証を通して明示的に一回送信する。"""

    await page.locator('.runForm button[type="submit"]').click()


async def pending(page: Page, phase: str = "unknown") -> None:
    """見た目の不変な class と利用者の文言の両方で未確認状態を確認する。"""

    phrases = {
        "unknown": "尚未确认创建结果",
        "rejected": "本次请求被拒绝",
        "conflict": "原键发生冲突",
    }
    await expect(page.locator(".submissionNotice")).to_contain_text(phrases[phase])


async def confirmed(page: Page) -> None:
    """成功/再送応答は既存の Run 観測と detail/SSE へ接続される。"""

    await expect(page.get_by_role("dialog")).not_to_be_visible()
    await expect(page.locator(".runFacts")).to_be_visible()


async def exercise(
    page: Page, api: ApiFixture, url: str, case: str, output: Path | None
) -> None:
    """原要求・草稿・認証 context・遅い応答の競争を、実 React event で再現する。"""

    await open_form(page, url)
    if case == "timeout":
        await page.clock.install()
    await start(page)
    await asyncio.wait_for(api.received.wait(), timeout=10)
    if case in {"double-click", "timeout", "actor-switch", "project-switch"}:
        await expect(page.locator('.runForm button[type="submit"]')).to_be_disabled()
        await page.locator(".runForm").evaluate(
            "form => { form.dispatchEvent(new Event('submit', {bubbles:true,cancelable:true})); form.dispatchEvent(new Event('submit', {bubbles:true,cancelable:true})); }"
        )
        assert len(api.posts) == 1
        if case == "double-click":
            await page.get_by_role("button", name="关闭", exact=True).click()
            await page.locator(".runLauncher > button").click()
            await expect(page.locator(".submissionNotice")).to_contain_text(
                "正在等待创建结果"
            )
            api.release.set()
            await confirmed(page)
        elif case == "timeout":
            await page.clock.fast_forward(30_001)
            await pending(page)
            await page.get_by_role("button", name="用原请求再次确认").click()
            await confirmed(page)
            api.release.set()
            assert api.posts[0]["key"] == api.posts[1]["key"]
            assert len(api.runs) == 1
        else:
            update = (
                {"actorId": "00000000-0000-4000-8000-000000000002"}
                if case == "actor-switch"
                else {"projectId": OTHER_PROJECT}
            )
            await page.evaluate(
                "next => window.updateSubmissionTestContext(next)", update
            )
            await expect(page.locator(".submissionNotice")).to_have_count(0)
            api.release.set()
            await page.locator(".runLauncher > button").click()
            await page.locator(".jsonInput").fill('{"query":"new context"}')
            await page.locator('.documentSourceField select').first.select_option("ALL")
            await expect(page.locator(".runFacts")).to_have_count(0)
            await start(page)
            await confirmed(page)
            assert api.posts[0]["key"] != api.posts[1]["key"]
            if case == "project-switch":
                assert api.posts[1]["project"] == OTHER_PROJECT
        return

    await pending(
        page,
        "conflict"
        if case == "conflict"
        else "rejected"
        if case == "reject"
        else "unknown",
    )
    await expect(page.locator('.runForm button[type="submit"]')).to_be_disabled()
    if case == "reload":
        await page.reload()
        await expect(page.locator(".runLauncher > button")).to_be_enabled()
        await expect(page.locator(".submissionNotice")).to_have_count(0)
        assert len(api.posts) == len(api.runs) == 1
        return
    if case in {"new-intent", "conflict"}:
        await page.locator(".jsonInput").fill('{"query":"edited"}')
        await page.get_by_role("checkbox").check()
        await start(page)
        await confirmed(page)
        assert api.posts[0]["key"] != api.posts[1]["key"]
        assert api.posts[0]["body"] != api.posts[1]["body"]
        assert len(api.runs) == (2 if case == "new-intent" else 1)
        return

    # form を不正な JSON に編集しても、確認操作は固定済み本文だけを送る。
    await page.locator(".jsonInput").fill("incomplete edited draft")
    await page.evaluate(
        "() => window.updateSubmissionTestContext({csrfToken:'r'.repeat(32)})"
    )
    if case == "translations":
        for language, phrase in (
            ("en", "Confirm the previous submission"),
            ("ja", "前回の送信を確認"),
            ("zh", "确认上一次提交"),
        ):
            await page.evaluate(
                "language => window.updateSubmissionTestContext({language})", language
            )
            await expect(page.locator(".submissionNotice h3")).to_have_text(phrase)
            for width in (1440, 390):
                await page.set_viewport_size({"width": width, "height": 1000})
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                await page.get_by_role("checkbox").focus()
                await page.keyboard.press("Space")
                await expect(page.get_by_role("checkbox")).to_be_checked()
                focus = await page.get_by_role("checkbox").evaluate(
                    "element => ({focused: document.activeElement === element, active: document.activeElement?.outerHTML.slice(0, 300)})"
                )
                assert focus["focused"], (language, width, focus)
                await page.keyboard.press("Space")
                if output is not None:
                    await page.screenshot(
                        path=str(output / f"submission-{language}-{width}.png")
                    )
    await page.get_by_role("button", name="用原请求再次确认").click()
    if case == "revoked-after-loss":
        await pending(page, "rejected")
        await expect(page.locator(".submissionNotice")).to_contain_text(
            "原执行可能已经创建"
        )
        await page.get_by_role("button", name="用原请求再次确认").click()
    await confirmed(page)
    assert len({post["key"] for post in api.posts}) == 1
    assert len({post["body"] for post in api.posts}) == 1
    assert api.posts[0]["csrf"] == "c" * 32 and api.posts[-1]["csrf"] == "r" * 32
    assert len(api.runs) == 1


async def check(url: str, output: Path | None, selected_case: str | None) -> None:
    """各ケースは別 browser context。server DB/モデルへは接続しない。"""

    address = urlsplit(url)
    if address.scheme != "http" or address.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("Browser fixture URL must use a loopback Vite server")
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
    cases = {
        "lost-response": ["drop"],
        "non-json": ["non-json"],
        "contract": ["contract"],
        "gateway": ["gateway"],
        "wrong-project": ["wrong-project"],
        "new-intent": ["drop"],
        "conflict": ["conflict"],
        "reject": ["reject"],
        "revoked-after-loss": ["drop", "forbidden"],
        "translations": ["drop"],
        "double-click": ["hold"],
        "timeout": ["hold"],
        "actor-switch": ["hold"],
        "project-switch": ["hold"],
        "reload": ["drop"],
    }
    if selected_case is not None:
        cases = {selected_case: cases[selected_case]}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        for case, actions in cases.items():
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000}
            )
            page = await context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.add_init_script(
                "window.storageWrites=[]; const original=Storage.prototype.setItem; Storage.prototype.setItem=function(key,value){window.storageWrites.push(key); return original.call(this,key,value);};"
            )
            api = ApiFixture(f"{address.scheme}://{address.netloc}", actions)
            await context.route("**/*", api.route)
            try:
                await exercise(page, api, url, case, output)
                assert not errors, errors
                assert not api.unexpected, api.unexpected
                assert await page.evaluate("window.storageWrites") == []
                print(
                    f"{case}: passed ({len(api.posts)} request(s), {len(api.runs)} Run(s))",
                    flush=True,
                )
            finally:
                api.release.set()
                await context.close()
        await browser.close()


def main() -> None:
    """Vite の fixture URL と任意の外部 screenshot directory を受け取る。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:5173/projectmind/tests/browser/run-submission.html",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", help="Run a single named scenario")
    args = parser.parse_args()
    asyncio.run(check(args.url, args.output, args.case))


if __name__ == "__main__":
    main()
