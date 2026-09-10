"""実 Task Center の読取専用投影を、全面 mock HTTP と実 App で検証する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from check_accounts import OTHER, PASSWORD, ResponseGate, layout, privacy, project, settle
from check_projects import ARCHIVED, NEXT_PROJECT, PROJECT, ProjectsApi, menu, messages, selected
from check_run_submission import task_catalog
from playwright.async_api import Browser, Page, Route, async_playwright, expect

VERSION = "00000000-0000-4000-8000-000000000061"
TASK = "00000000-0000-4000-8000-000000000070"
SECOND_TASK = "00000000-0000-4000-8000-000000000071"
FIRST = f"{VERSION}::review"
SECOND = f"{VERSION}::compare"
ITEM_REFERENCES = (
    "/tasks/1/objective",
    "/resource_requirements/1",
    "/resource_requirements/0",
    "/tasks/1/deliverables/0",
    "/interaction_points/0",
    "/effect_intents/0",
    "/questions/0",
)


class FlowApi(ProjectsApi):
    """原公開 fixture を共有し、精確 Task の新 GET だけを追加する。"""

    def __init__(
        self, url: str, language: str, mode: str, numeric_wire: bytes | None = None
    ) -> None:
        """各 context の公開回応と gate を分離し、実 API へ fallback しない。"""
        super().__init__(url, language)
        self.mode = mode
        self.preview: dict = {}
        self.flow_reads: list[tuple[str, str]] = []
        self.flow_gates: dict[tuple[str, str], ResponseGate] = {}
        self.all_gates: list[ResponseGate] = []
        self.preview_failure: tuple[int, str] | None = None
        self.marker = "original"
        self.numeric_wire: bytes | None = None
        self.numeric_payload: dict = {}
        if mode.startswith("numeric-"):
            assert numeric_wire is not None, "Numeric cases require Python-produced wire"
            self.numeric_payload = json.loads(numeric_wire)
            self.numeric_wire = numeric_wire
            owner = self.numeric_payload["identity"]["project_id"]
            record = project(owner)
            self.projects = [record]
            self.details = {owner: record}
            self.preference = owner
            if mode != "numeric-wire":
                # 原 wire case と分け、HTTP consumer の損傷/字面を Python 側だけで合成する。
                huge = "1" + "0" * 100
                clauses = {
                    "numeric-literals": (
                        '"type":"number","enum":[1.0,1e-7,-0,9007199254740992,9007199254740993]'
                    ),
                    "numeric-boolean": '"type":"number","enum":[1,true]',
                    "numeric-equal": '"type":"number","enum":[1,1.0]',
                    "numeric-zero": '"type":"number","enum":[0,-0.0]',
                    "numeric-integer-float": '"type":"integer","enum":[1.0]',
                    "numeric-int-range": (
                        '"type":"number","minimum":9007199254740993,"maximum":9007199254740992.0'
                    ),
                    "numeric-float-range": f'"type":"number","minimum":1e100,"maximum":{huge}',
                    "numeric-negative-range": f'"type":"number","minimum":-{huge},"maximum":-1e100',
                    "numeric-string-range": '"type":"string","min_length":2,"max_length":1',
                }[mode]
                literal = (
                    '{"contract_version":"skillmind.task-contract-draft/v1",' + clauses + "}"
                )
                changed = deepcopy(self.numeric_payload)
                changed["plan"]["task"]["value"]["parameter_contract"] = {
                    "__raw_numeric_contract__": True
                }
                self.numeric_wire = (
                    json.dumps(changed, ensure_ascii=False, separators=(",", ":"))
                    .replace(
                        '{"__raw_numeric_contract__":true}',
                        literal,
                    )
                    .encode("utf-8")
                )

    def catalog(self) -> dict:
        """Catalog の task_id と preview の照合を、別名 Task にも適用する。"""
        original = task_catalog()["tasks"][0]
        if self.numeric_payload:
            identity = self.numeric_payload["identity"]
            return {
                "tasks": [
                    {
                        **original,
                        **{
                            name: identity[name]
                            for name in (
                                "skill_id",
                                "skill_version_id",
                                "skill_key",
                                "version",
                                "task_key",
                                "task_id",
                            )
                        },
                        "title": "Original Python numeric contract",
                        "readiness": None,
                    }
                ]
            }
        return {
            "tasks": [
                {
                    **original,
                    "skill_id": "00000000-0000-4000-8000-000000000060",
                    "skill_key": "source-review",
                    "skill_name": "Original source review",
                    "task_key": key,
                    "task_id": identifier,
                    "title": f"Original {key}",
                    "readiness": None,
                }
                for key, identifier in (("review", TASK), ("compare", SECOND_TASK))
            ]
        }

    def hold_flow(self, project_id: str = PROJECT, key: str = "review") -> ResponseGate:
        """取消を無視する transport の回応を、採用済み UI を観察した後で解放する。"""
        gate = ResponseGate()
        self.flow_gates[(project_id, key)] = gate
        self.all_gates.append(gate)
        return gate

    async def respond(self, route: Route) -> None:
        """未知 URL/外部通信は共有 handler が記録して遮断する。"""
        address = urlsplit(route.request.url)
        suffix = address.path.removeprefix(self.prefix)
        parts = suffix.split("/")
        if f"{address.scheme}://{address.netloc}" != self.origin:
            await super().respond(route)
            return
        if len(parts) == 3 and parts[0] == "projects" and parts[2] == "tasks":
            assert route.request.method == "GET" and parts[1] in self.details
            self.calls.append(("GET", suffix, parse_qs(address.query), None))
            body = self.catalog()
            if self.mode == "invalid-target":
                body["tasks"][0]["task_id"] = "historical-invalid-id"
            await route.fulfill(json=body, headers={"Cache-Control": "no-store"})
            return
        if (
            len(parts) != 7
            or parts[0] != "projects"
            or parts[2] != "skill-versions"
            or parts[4] != "tasks"
            or parts[6] != "flow-preview"
        ):
            await super().respond(route)
            return
        assert route.request.method == "GET" and not address.query
        if self.numeric_wire is not None:
            identity = self.numeric_payload["identity"]
            assert (parts[1], parts[3], parts[5]) == (
                identity["project_id"],
                identity["skill_version_id"],
                identity["task_key"],
            )
            self.calls.append(("GET", suffix, {}, None))
            self.flow_reads.append((parts[1], parts[5]))
            await route.fulfill(
                body=self.numeric_wire,
                content_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
            return
        assert parts[1] in self.details and parts[3] == VERSION
        assert parts[5] in {"review", "compare"}
        assert self.preview, "Preview fixture must be loaded before explicit selection"
        self.calls.append(("GET", suffix, {}, None))
        self.flow_reads.append((parts[1], parts[5]))
        body = deepcopy(self.preview)
        target = next(item for item in self.catalog()["tasks"] if item["task_key"] == parts[5])
        for name in ("skill_id", "skill_version_id", "task_id", "task_key", "skill_key", "version"):
            body["identity"][name] = target[name]
        body["identity"]["project_id"] = parts[1]
        body["plan"]["task"]["value"]["key"] = parts[5]
        body["plan"]["task"]["value"]["objective"] = (
            f"{self.marker}:{parts[1]}:{parts[5]} — Original material <not-html>."
        )
        if self.mode == "not-declared":
            body.update(status="NOT_DECLARED", plan=None, blueprint_checksum=None, source_traces=[])
            body["readiness"]["assessment"] = None
        elif self.mode == "unassessed":
            body["readiness"]["assessment"] = None
        elif self.mode == "invalid":
            body["identity"]["task_id"] = SECOND_TASK
        elif self.mode == "source":
            body["plan"]["shared"]["questions"] = [
                {"key": "scope", "text": "Which scope <not-html>?", "required": True}
            ]
            body["source_traces"].extend(
                {
                    "target": reference,
                    "path": f"notes/item-{index}.md",
                    "line": 1,
                    "reason": 'Original <img src="https://invalid.example/trace"> stays text.',
                    "verification": "TEXT_SNAPSHOT",
                }
                for index, reference in enumerate(ITEM_REFERENCES)
            )
            body["source_traces"].extend(
                [
                    {
                        "target": "/tasks/1/success_criteria/0/text",
                        "path": "notes/criteria.md",
                        "line": 2,
                        "reason": "Exact descendant source",
                        "verification": "TEXT_SNAPSHOT",
                    },
                    {
                        "target": "/tasks/1",
                        "path": "notes/ancestor.md",
                        "line": 1,
                        "reason": "Ancestor is not item evidence",
                        "verification": "TEXT_SNAPSHOT",
                    },
                ]
            )
        failure = self.preview_failure
        gate = self.flow_gates.get((parts[1], parts[5]))
        if gate:
            gate.received.set()
            await asyncio.wait_for(gate.release.wait(), 45)
            failure = gate.failure or failure
        try:
            if failure:
                status, code = failure
                await route.fulfill(
                    status=status,
                    json={
                        "type": f"https://skillmind.local/problems/{code}",
                        "status": status,
                        "title": "Fixture error",
                        "code": code,
                        "detail": "Private server diagnostic must never be displayed",
                    },
                    headers={"Cache-Control": "no-store"},
                )
            else:
                await route.fulfill(json=body, headers={"Cache-Control": "no-store"})
        finally:
            if gate:
                gate.returned.set()


async def open_preview(page: Page, key: str = FIRST, *, keep_session: bool = True) -> None:
    """実 button を keyboard で開き、見出しへの focus 移動を確認する。"""
    button = page.locator(f'[data-flow-open="{key}"]')
    await expect(button).to_be_enabled()
    await button.focus()
    await page.keyboard.press("Enter")
    if keep_session:
        await expect(page.locator("#task-flow-heading")).to_be_focused()


async def adopted(page: Page, marker: str, project_id: str = PROJECT, key: str = "review") -> None:
    """HTTP 到達数ではなく、精確新 identity の原文が実 DOM に採用された事実を待つ。"""
    await expect(page.locator("[data-flow-objective]")).to_have_text(
        f"{marker}:{project_id}:{key} — Original material <not-html>."
    )
    await expect(page.locator("[data-task-flow-preview]")).to_have_count(1)
    await expect(page.locator("not-html")).to_have_count(0)


async def screenshot(page: Page, output: Path, name: str) -> None:
    """全体と原字号の viewport を分け、長い全文を縮小して可読と見なさない。"""
    await page.screenshot(path=str(output / f"{name}-full.png"), full_page=True)
    panel = page.locator("[data-task-flow-panel]")
    if await panel.count():
        await panel.evaluate("element => element.scrollIntoView({block:'start'})")
    await page.screenshot(path=str(output / f"{name}-viewport.png"))


async def notice_contrast(page: Page) -> None:
    """暗色 theme で白背景 fallback が注意書きを読めなくする退行を防ぐ。"""
    ratio = await page.locator(".taskFlowNotice").evaluate(r"""element => {
      const style = getComputedStyle(element);
      const luminance = color => {
        const channels = color.match(/[\d.]+/g).slice(0, 3).map(Number).map(v => {
          const value = v / 255;
          return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
        });
        return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
      };
      const values = [luminance(style.color), luminance(style.backgroundColor)].sort((a,b) => a-b);
      return (values[1] + .05) / (values[0] + .05);
    }""")
    assert ratio >= 4.5, f"Unreadable declaration notice: {ratio}"


async def settle_numeric_scroll(page: Page) -> None:
    """原生キーボード scroll の終了を待ち、途中の viewport を保存しない。"""
    await page.evaluate("""() => new Promise((resolve, reject) => {
      let previous = null;
      let stable = 0;
      const started = performance.now();
      const frame = () => {
        const values = [window.scrollX, window.scrollY];
        for (const element of document.querySelectorAll('[data-task-flow-preview] pre')) {
          values.push(element.scrollTop, element.getBoundingClientRect().top);
        }
        const current = JSON.stringify(values);
        stable = current === previous ? stable + 1 : 0;
        previous = current;
        if (stable >= 8) resolve();
        else if (performance.now() - started > 5000) reject(new Error('Scroll did not settle'));
        else requestAnimationFrame(frame);
      };
      requestAnimationFrame(frame);
    })""")


async def run_case(
    browser: Browser,
    url: str,
    output: Path,
    name: str,
    mode: str,
    language: str,
    width: int,
    numeric_wire: bytes | None = None,
) -> None:
    """独立 browser context で資格・遅延・三語・狭幅を検証し、全 gate を閉じる。"""
    api = FlowApi(url, language, mode, numeric_wire)
    context = await browser.new_context(viewport={"width": width, "height": 1000}, locale=language)
    errors: list[str] = []
    await context.route("**/*", api.route)
    await context.add_init_script("""(() => {
      const original = window.fetch.bind(window);
      window.fetch = (input, init) => original(input, init ? {...init, signal:undefined} : init);
      const now = performance.now.bind(performance); window.flowElapsed = 0;
      Object.defineProperty(performance, 'now', {value: () => now() + window.flowElapsed});
    })()""")
    page = await context.new_page()
    page.set_default_timeout(7000)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "response",
        lambda response: (
            errors.append(f"Static HTTP {response.status}: {response.url}")
            if response.status >= 400 and not urlsplit(response.url).path.startswith(api.prefix)
            else None
        ),
    )
    page.on(
        "console",
        lambda message: (
            errors.append(message.text)
            if message.type == "error" and "Failed to load resource" not in message.text
            else None
        ),
    )
    project_id = ARCHIVED if mode == "archived" else PROJECT
    if api.numeric_payload:
        project_id = api.numeric_payload["identity"]["project_id"]
    gate: ResponseGate | None = None
    try:
        await page.goto(f"{url}#/tasks?project={project_id}")
        await selected(page, project_id)
        await expect(page.locator("[data-flow-open]")).to_have_count(1 if api.numeric_wire else 2)
        labels = await messages(page, language)
        flow = labels["taskFlow"]
        api.preview = await page.evaluate(
            "async () => (await import('../fixtures/taskFlow.ts')).flowPreview()"
        )
        assert api.flow_reads == [], "Mount must not implicitly request a plan"
        if mode.startswith("numeric-"):
            identity = api.numeric_payload["identity"]
            await open_preview(page, f"{identity['skill_version_id']}::{identity['task_key']}")
            if mode in {"numeric-wire", "numeric-literals"}:
                panel = page.locator("[data-task-flow-preview]")
                await expect(panel).to_have_count(1)
                await panel.get_by_text(flow["originalContracts"], exact=True).click()
                values = panel.locator("pre")
                await expect(values).to_have_count(2)
                original = api.numeric_payload["plan"]["task"]["value"]
                if mode == "numeric-wire":
                    for index, key in enumerate(("parameter_contract", "result_contract")):
                        expected = json.dumps(original[key], ensure_ascii=False, indent=2)
                        await expect(values.nth(index)).to_have_text(expected, use_inner_text=True)
                else:
                    actual = await values.first.inner_text()
                    for literal in ("1.0", "1e-7", "-0", "9007199254740992", "9007199254740993"):
                        assert literal in actual, (literal, actual)
                    await expect(values.nth(1)).to_have_text(
                        json.dumps(original["result_contract"], ensure_ascii=False, indent=2),
                        use_inner_text=True,
                    )
                await expect(panel).not_to_contain_text('"raw":')
                await values.first.focus()
                await expect(values.first).to_be_focused()
                await page.keyboard.press("End")
                await settle_numeric_scroll(page)
                await values.first.evaluate(
                    "element => {element.scrollTop = 0; "
                    "element.closest('details').scrollIntoView("
                    "{block:'start', behavior:'instant'})}"
                )
                await settle_numeric_scroll(page)
                await expect(
                    panel.get_by_text(flow["originalContracts"], exact=True)
                ).to_be_visible()
                assert await values.first.evaluate("element => element.scrollTop === 0")
                await page.screenshot(path=str(output / f"{name}-numeric-viewport.png"))
                assert await values.first.evaluate(
                    "element => element.scrollWidth <= element.clientWidth + 1"
                )
            else:
                await expect(page.locator("[data-flow-error]")).to_have_text(
                    flow["failures"]["invalid"]
                )
                await expect(page.locator("[data-task-flow-preview]")).to_have_count(0)
            assert api.flow_reads == [(project_id, identity["task_key"])]
            await screenshot(page, output, name)
        elif mode == "invalid-target":
            await expect(page.locator(f'[data-flow-open="{FIRST}"]')).to_be_disabled()
            await expect(page.locator("[data-flow-invalid-target]")).to_have_text(
                flow["failures"]["invalid"]
            )
        elif mode in {"source", "archived", "not-declared", "unassessed"}:
            await open_preview(page)
            if mode == "not-declared":
                await expect(page.locator("[data-flow-not-declared]")).to_have_text(flow["missing"])
                await expect(page.locator("[data-flow-task], [data-flow-shared]")).to_have_count(0)
            else:
                await adopted(page, "original", project_id)
                panel = page.locator("[data-task-flow-preview]")
                for selector, label in (
                    ("[data-flow-task]", "taskScope"),
                    ("[data-flow-shared]", "sharedScope"),
                    ("[data-flow-readiness]", "readiness"),
                ):
                    await expect(panel.locator(selector)).to_contain_text(flow[label])
                await expect(
                    panel.locator("button, form, input, a, [role=progressbar]")
                ).to_have_count(0)
                await expect(page.locator("[data-task-flow-panel] .panelHeader")).to_contain_text(
                    "source-review"
                )
                if mode == "unassessed":
                    await expect(panel.locator("[data-flow-readiness]")).to_contain_text(
                        flow["unassessed"]
                    )
                    assert (
                        labels["workspace"]["noResourceNeeded"]
                        not in await page.locator(".taskCards").inner_text()
                    )
                sources = page.locator("[data-flow-sources]")
                await sources.locator("summary").focus()
                await page.keyboard.press("Enter")
                await expect(sources).to_have_attribute("open", "")
                await expect(sources).to_contain_text(flow["sourcesHint"])
                await expect(sources).to_contain_text("/tasks/0/objective")
                await expect(sources).to_contain_text(flow["verification"]["SOURCE_INDEX"])
                if mode == "source":
                    item = page.locator('[data-flow-item-sources="/tasks/1/success_criteria/0"]')
                    await item.locator("summary").focus()
                    await page.keyboard.press("Enter")
                    await expect(item).to_contain_text("notes/criteria.md:2")
                    await expect(item).not_to_contain_text("notes/ancestor.md")
                    absent = page.locator('[data-flow-item-sources="/guidance/required_rules/0"]')
                    await absent.locator("summary").click()
                    await expect(absent).to_contain_text(flow["noItemSources"])
                    for index, reference in enumerate(ITEM_REFERENCES):
                        if reference == "/questions/0":
                            await panel.get_by_text(flow["preferences"], exact=True).click()
                        entry = panel.locator(f'[data-flow-item-sources="{reference}"]')
                        await entry.locator("summary").focus()
                        await page.keyboard.press("Enter")
                        await expect(entry).to_contain_text(f"notes/item-{index}.md:1")
                        await expect(entry).to_contain_text(
                            '<img src="https://invalid.example/trace">'
                        )
                        await expect(entry).not_to_contain_text(
                            "Original note for a different Task."
                        )
                        await expect(entry).not_to_contain_text("Ancestor is not item evidence")
                        await entry.locator("summary").click()
                    await expect(panel.locator("img, iframe, script, not-html")).to_have_count(0)
            await notice_contrast(page)
            await screenshot(page, output, name)
            if mode == "source":
                await page.locator("[data-flow-shared]").evaluate(
                    "e => e.scrollIntoView({block:'start'})"
                )
                await page.screenshot(path=str(output / f"{name}-shared-viewport.png"))
            await page.locator("[data-flow-close]").focus()
            await page.keyboard.press("Escape")
            await expect(page.locator("[data-task-flow-panel]")).to_have_count(0)
            await expect(page.locator(f'[data-flow-open="{FIRST}"]')).to_be_focused()
        elif mode in {"403", "404", "409", "503", "401", "invalid", "refresh-failure"}:
            codes = {
                "403": "project_access_denied",
                "404": "task_flow_preview_not_found",
                "409": "task_flow_preview_invalid",
                "503": "task_flow_preview_unavailable",
                "401": "authentication_required",
            }
            if mode in codes:
                api.preview_failure = (int(mode), codes[mode])
            await open_preview(page, keep_session=mode != "401")
            if mode == "refresh-failure":
                await adopted(page, "original")
                api.preview_failure = (409, "task_flow_preview_invalid")
                await page.locator("[data-flow-refresh]").click()
            if mode == "401":
                await expect(page.locator('input[name="email"]')).to_be_visible()
            else:
                error_key = (
                    "unavailable"
                    if mode in {"403", "404"}
                    else "loadFailed"
                    if mode == "503"
                    else "invalid"
                )
                await expect(page.locator("[data-flow-error]")).to_have_text(
                    flow["failures"][error_key]
                )
                await expect(page.locator("[data-task-flow-preview]")).to_have_count(0)
            await screenshot(page, output, name)
        else:
            gate = api.hold_flow()
            if mode == "timeout":
                await page.clock.install()
            if mode == "same-tick":
                await page.locator(f'[data-flow-open="{FIRST}"]').evaluate(
                    "button => {button.click(); button.click()}"
                )
            else:
                await open_preview(page)
            await asyncio.wait_for(gate.received.wait(), 7)
            if mode == "same-tick":
                assert api.flow_reads == [(PROJECT, "review")]
            elif mode.startswith("close-"):
                await page.locator("[data-flow-close]").click()
                await expect(page.locator("[data-task-flow-panel]")).to_have_count(0)
            elif mode.startswith("task-"):
                api.marker = "adopted-new-task"
                await open_preview(page, SECOND)
                await adopted(page, api.marker, key="compare")
            elif mode.startswith("project-"):
                api.marker = "adopted-new-project"
                await menu(page)
                await page.locator(".sideNavProject select").select_option(NEXT_PROJECT)
                await selected(page, NEXT_PROJECT)
                await expect(page.locator("[data-task-flow-panel]")).to_have_count(0)
                await open_preview(page)
                await adopted(page, api.marker, NEXT_PROJECT)
            elif mode.startswith(("actor-", "session-")):
                await menu(page)
                await page.locator(".sidebarLogout").click()
                await expect(page.locator('input[name="email"]')).to_be_visible()
                if mode.startswith("actor-"):
                    api.actor = OTHER
                api.flow_gates.clear()
                api.marker = "adopted-new-session"
                await page.locator('input[name="email"]').fill(api.users[api.actor]["email"])
                await page.locator('input[name="password"]').fill(PASSWORD)
                await page.locator('.authCard button[type="submit"]').click()
                await expect(page.locator(".sidebarLogout")).to_have_count(1)
                await page.evaluate(
                    "fragment => {location.hash = fragment}", f"/tasks?project={PROJECT}"
                )
                await expect(page.locator("[data-flow-open]")).to_have_count(2)
                await expect(page.locator("[data-task-flow-panel]")).to_have_count(0)
                await open_preview(page)
                await adopted(page, api.marker)
            elif mode == "timeout":
                await page.clock.fast_forward(30_000)
                await expect(page.locator("[data-flow-error]")).to_have_text(
                    flow["failures"]["timeout"]
                )
            elif mode == "absolute-timeout":
                await page.evaluate("window.flowElapsed = 31_000")
            else:
                raise AssertionError(f"Unknown scenario: {mode}")
            if mode.endswith("401") or mode in {"timeout", "absolute-timeout"}:
                gate.failure = (401, "authentication_required")
            gate.release.set()
            await asyncio.wait_for(gate.returned.wait(), 7)
            await settle(page)
            await expect(page.locator('input[name="email"]')).to_have_count(0)
            if mode == "same-tick":
                await adopted(page, "original")
            elif mode.startswith("close-"):
                await expect(page.locator("[data-task-flow-panel]")).to_have_count(0)
            elif mode.startswith("task-"):
                await adopted(page, api.marker, key="compare")
            elif mode.startswith("project-"):
                await adopted(page, api.marker, NEXT_PROJECT)
            elif mode.startswith(("actor-", "session-")):
                await adopted(page, api.marker)
            else:
                await expect(page.locator("[data-flow-error]")).to_have_text(
                    flow["failures"]["timeout"]
                )
                await expect(page.locator("[data-task-flow-preview]")).to_have_count(0)
            if mode == "timeout":
                await page.clock.resume()
            await screenshot(page, output, name)
        await layout(page)
        await privacy(page)
        assert "Private server diagnostic" not in await page.locator("body").inner_text()
        assert not [
            call for call in api.calls if call[0] != "GET" and call[1].startswith("projects/")
        ]
        assert not api.unexpected, api.unexpected
        assert not api.failures, api.failures
        assert not errors, errors
    except Exception:
        print(
            json.dumps(
                {
                    "case": name,
                    "errors": errors,
                    "unexpected": api.unexpected,
                    "fixture_failures": api.failures,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        raise
    finally:
        for active in api.all_gates:
            active.release.set()
        await context.close()


async def main() -> None:
    """外置 artifact に新規保存し、自分の browser だけを必ず終了する。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--numeric-wire", type=Path)
    arguments = parser.parse_args()
    assert urlsplit(arguments.url).hostname in {"127.0.0.1", "localhost"}
    arguments.output.mkdir(parents=True, exist_ok=False)
    cases = [
        (mode, language, width)
        for mode in ("source", "not-declared", "unassessed", "archived")
        for language in ("zh", "ja", "en")
        for width in (390, 1440)
    ]
    cases.extend(
        (mode, "en", 390)
        for mode in (
            "403",
            "404",
            "409",
            "503",
            "401",
            "invalid",
            "invalid-target",
            "refresh-failure",
            "same-tick",
            "close-200",
            "close-401",
            "task-200",
            "task-401",
            "project-200",
            "project-401",
            "actor-200",
            "actor-401",
            "session-200",
            "session-401",
            "timeout",
            "absolute-timeout",
        )
    )
    cases.extend((mode, language, 390) for mode in ("409", "503") for language in ("zh", "ja"))
    completed = []
    numeric_wire = arguments.numeric_wire.read_bytes() if arguments.numeric_wire else None
    if numeric_wire is not None:
        cases.extend(
            (mode, language, width)
            for mode in ("numeric-wire", "numeric-literals")
            for language in ("zh", "ja", "en")
            for width in (390, 1440)
        )
        cases.extend(
            (mode, language, 390)
            for mode in (
                "numeric-boolean",
                "numeric-equal",
                "numeric-zero",
                "numeric-integer-float",
                "numeric-int-range",
                "numeric-float-range",
                "numeric-negative-range",
                "numeric-string-range",
            )
            for language in ("zh", "ja", "en")
        )
    else:
        print("Numeric wire was not supplied; numeric cases are not included", flush=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for mode, language, width in cases:
                name = f"{mode}-{language}-{width}"
                if arguments.case and name not in arguments.case:
                    continue
                await run_case(
                    browser,
                    arguments.url,
                    arguments.output,
                    name,
                    mode,
                    language,
                    width,
                    numeric_wire,
                )
                completed.append(name)
                print(f"PASS {len(completed)} {name}", flush=True)
        finally:
            await browser.close()
    (arguments.output / "results.json").write_text(
        json.dumps({"passed": completed}, indent=2) + "\n"
    )
    print(f"Completed {len(completed)} cases; browser closed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
