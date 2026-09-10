"""実 TasksPage の時区/preview 確認を全面 HTTP mock で検証する。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from check_run_submission import OTHER_PROJECT, PROJECT, ROOT, ApiFixture, task_catalog
from playwright.async_api import Error, Page, Route, async_playwright, expect

CONFIRM = {
    "zh": "我已确认当前规则的以上计划时刻和 UTC offset。",
    "ja": "現在の規則の予定時刻と UTC offset を確認しました。",
    "en": "I have confirmed these planned times and UTC offsets for the current rule.",
}
OPENAPI = json.loads((ROOT.parent / "contracts/openapi/skillmind-api.v1.json").read_text())
PREVIEW_LIMIT = OPENAPI["components"]["schemas"]["SchedulePreviewResponse"]["properties"][
    "occurrences"
]["maxItems"]


def occurrences(definition: dict) -> list[str]:
    """固定した未来の暦だけで合法候補を作り、実 scheduler は起動しない。"""

    if definition["kind"] == "ONCE":
        assert datetime.fromisoformat(definition["run_at"]).tzinfo is not None
        return [definition["run_at"]]
    hour = int(definition["cron_expression"].split()[1])
    first = datetime(2027, 1, 1, hour, tzinfo=ZoneInfo(definition["timezone"]))
    values = [first + timedelta(days=day) for day in range(PREVIEW_LIMIT)]
    if definition.get("end_at"):
        end = datetime.fromisoformat(definition["end_at"])
        assert end.tzinfo is not None
        values = [value for value in values if value <= end]
    return [
        value.astimezone(UTC).isoformat()
        for value in values[: definition.get("max_runs") or PREVIEW_LIMIT]
    ]


class ScheduleApi(ApiFixture):
    """preview と保存を分離し、遅い応答でも元の要求を上書きしない。"""

    def __init__(self, origin: str) -> None:
        super().__init__(origin, [])
        self.catalog = task_catalog()
        self.catalog["tasks"][0]["readiness"]["requirements"] = []
        self.preview_actions: list[str] = []
        self.save_actions: list[str] = []
        self.previews: list[dict] = []
        self.saves: list[dict] = []
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.preview_received = asyncio.Event()
        self.save_received = asyncio.Event()

    async def route(self, route: Route) -> None:
        """対象 Project の既知 API 以外は既存 fail-closed fixture に委ねる。"""

        request = route.request
        parsed = urlsplit(request.url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            await super().route(route)
            return
        base = "/skillmind/api/v1/projects/"
        if parsed.path.startswith(base):
            project, _, resource = parsed.path[len(base) :].partition("/")
            assert project in {PROJECT, OTHER_PROJECT}, project
            if request.method == "GET" and resource == "tasks":
                await route.fulfill(json=self.catalog)
                return
            if request.method == "GET" and resource == "schedules":
                await route.fulfill(json={"schedules": [], "total": 0, "limit": 100, "offset": 0})
                return
            if request.method == "POST" and resource in {"schedules", "schedules/preview"}:
                preview = resource.endswith("preview")
                records = self.previews if preview else self.saves
                actions = self.preview_actions if preview else self.save_actions
                body = request.post_data_json
                records.append(
                    {"body": body, "project": project, "csrf": request.headers.get("x-csrf-token")}
                )
                action = actions.pop(0) if actions else "success"
                (self.preview_received if preview else self.save_received).set()
                definition = body["definition"]
                candidate = occurrences(definition)
                assert candidate
                response = (
                    {"occurrences": candidate}
                    if preview
                    else {
                        "cron_expression": None,
                        "run_at": None,
                        "end_at": None,
                        "max_runs": None,
                        **{key: value for key, value in body.items() if key != "definition"},
                        **definition,
                        "schedule_id": str(uuid4()),
                        "project_id": project,
                        "status": "ACTIVE",
                        "row_version": 1,
                        "run_count": 0,
                        "missed_count": 0,
                        "next_run_at": candidate[0],
                        "last_run_at": None,
                        "last_run_id": None,
                        "last_outcome": None,
                        "last_error": None,
                        "created_by": str(uuid4()),
                        "created_at": "2026-09-09T00:00:00Z",
                        "updated_at": "2026-09-09T00:00:00Z",
                    }
                )
                try:
                    if action == "hold":
                        await self.release.wait()
                    if action == "drop":
                        await route.abort("connectionreset")
                    elif action in {"401", "403", "404", "422", "500"}:
                        status = int(action)
                        await route.fulfill(
                            status=status, json={"status": status, "code": "fixture_refused"}
                        )
                    else:
                        if action == "empty":
                            response = {"occurrences": []}
                        elif action == "naive":
                            response = {"occurrences": ["2027-01-01T03:00:00"]}
                        elif action == "invalid-date":
                            response = {"occurrences": ["2027-02-29T03:00:00Z"]}
                        elif action == "unordered":
                            response = {"occurrences": list(reversed(candidate))}
                        await route.fulfill(
                            status=201 if not preview or action == "201" else 200, json=response
                        )
                except Error:
                    # 正当な abort/close は commit の取り消しを意味しない。
                    pass
                finally:
                    if action == "hold":
                        self.finished.set()
                return
        await super().route(route)


async def opened(page: Page, url: str, language: str = "en") -> None:
    """既存 mount-only entry から実 Task Center を開く。"""

    await page.goto(url)
    await page.evaluate(
        "next => window.updateSubmissionTestContext(next)",
        {"screen": "tasks", "language": language},
    )
    await page.locator(".taskCard .formRow button").first.click()
    await expect(page.locator("[data-schedule-form]")).to_be_visible()


async def previewed(page: Page) -> None:
    """表示だけでは送信できないことを確認し、明示 preview を一回行う。"""

    await page.locator("[data-schedule-preview]").click()
    await expect(page.locator("[data-schedule-confirm]")).to_be_visible()
    await expect(page.locator('[data-schedule-form] button[type="submit"]')).to_be_disabled()


async def submit_event(page: Page, count: int = 1) -> None:
    """disabled 属性を迂回しても同期門禁が守られることを検証する。"""

    await page.locator("[data-schedule-form]").evaluate(
        "(form, count) => { for(let i=0;i<count;i++) "
        "form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})) }",
        count,
    )


async def settle(page: Page) -> None:
    """描画と fetch callback の二周期だけを待ち、実 deadline と混同しない。"""

    await page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )


async def exercise(
    page: Page, api: ScheduleApi, url: str, case: str, language: str, output: Path, width: int
) -> None:
    """各 case は独立 context の原要求数と frozen definition を検査する。"""

    await opened(page, url, language)
    form = page.locator("[data-schedule-form]")
    save = form.locator('button[type="submit"]')
    confirm = page.locator("[data-schedule-confirm]")
    if case == "language":
        await expect(page.locator("[data-schedule-input-timezone]")).to_contain_text("UTC")
        await page.locator('[name="timezone"]').fill("Asia/Tokyo")
        await submit_event(page)
        assert not api.previews and not api.saves
        await previewed(page)
        await expect(page.get_by_role("checkbox", name=CONFIRM[language])).to_be_visible()
        await expect(page.locator(".schedulePreview li")).to_have_count(5)
        await expect(page.locator(".schedulePreview li").first).to_contain_text(
            "03:00 UTC+09:00 · Asia/Tokyo"
        )
        await submit_event(page)
        assert not api.saves
        await confirm.focus()
        await page.keyboard.press("Space")
        await expect(confirm).to_be_checked()
        await expect(save).to_be_enabled()
        await expect(form.locator('[role="alert"]')).to_have_count(0)
        await page.locator('[name="name"]').fill("Explicit schedule")
        assert len(api.previews) == 1
        await expect(confirm).to_be_checked()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await save.scroll_into_view_if_needed()
        await page.screenshot(path=str(output / f"{language}-{width}-preview.png"), full_page=True)
        await submit_event(page, 2)
        await expect(page.get_by_role("dialog")).to_have_count(0)
        assert len(api.saves) == 1
        assert api.saves[0]["body"]["definition"] == api.previews[0]["body"]["definition"]
        assert api.saves[0]["body"]["name"] == "Explicit schedule"
    elif case.startswith(("gap-", "fold-")):
        field = case.split("-")[1]
        if field == "run":
            await page.locator('[name="kind"]').select_option("ONCE")
        name = "run_at" if field == "run" else "end_at"
        await page.locator('[name="timezone"]').fill("Asia/Tokyo")
        value = "2027-03-14T02:30" if case.startswith("gap") else "2027-11-07T01:30"
        await page.locator(f'[name="{name}"]').fill(value)
        await expect(page.locator("[data-schedule-preview]")).to_be_disabled()
        if case.startswith("gap"):
            await expect(
                page.locator(f'[data-schedule-date="{name}"] [role="alert"]')
            ).to_be_visible()
            await submit_event(page)
            assert not api.previews and not api.saves
        else:
            choices = page.locator("[data-schedule-offset]")
            await expect(choices).to_have_value("")
            await expect(choices.locator("option")).to_have_count(3)
            await expect(choices).to_contain_text("UTC-04:00")
            await expect(choices).to_contain_text("UTC-05:00")
            await choices.select_option("2027-11-07T06:30:00.000Z")
            await previewed(page)
            assert api.previews[0]["body"]["definition"][name] == "2027-11-07T06:30:00.000Z"
            await confirm.check()
            await page.screenshot(path=str(output / f"{case}.png"), full_page=True)
            await save.click()
            await expect(page.get_by_role("dialog")).to_have_count(0)
            assert len(api.saves) == 1
    elif case == "same-tick-edit-submit":
        await previewed(page)
        await confirm.check()
        await form.evaluate("""form => {
          const input = form.querySelector('[name="cron_expression"]');
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
          setter.call(input, '0 4 * * *');
          input.dispatchEvent(new Event('input', {bubbles: true}));
          form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
        }""")
        await settle(page)
        await expect(confirm).to_have_count(0)
        assert len(api.previews) == 1 and not api.saves
    elif case == "definition-aba":
        await previewed(page)
        await confirm.check()
        await page.locator('[name="cron_expression"]').fill("0 4 * * *")
        await page.locator('[name="cron_expression"]').fill("0 3 * * *")
        await expect(confirm).to_have_count(0)
        await submit_event(page)
        assert len(api.previews) == 1 and not api.saves
        await previewed(page)
        await confirm.check()
        await save.click()
        await expect(page.get_by_role("dialog")).to_have_count(0)
        assert len(api.previews) == 2 and len(api.saves) == 1
    elif case == "preview-double":
        await page.locator("[data-schedule-preview]").evaluate(
            "button => {button.click();button.click()}"
        )
        await expect(confirm).to_be_visible()
        assert len(api.previews) == 1 and not api.saves
    elif case == "late-preview-aba":
        api.preview_actions = ["hold"]
        await page.locator("[data-schedule-preview]").click()
        await expect(page.locator("[data-schedule-preview]")).to_be_disabled()
        await page.locator('[name="cron_expression"]').fill("0 4 * * *")
        await page.locator('[name="cron_expression"]').fill("0 3 * * *")
        await previewed(page)
        await confirm.check()
        api.release.set()
        await asyncio.wait_for(api.finished.wait(), 5)
        await settle(page)
        await expect(confirm).to_be_checked()
        assert len(api.previews) == 2 and not api.saves
        await save.click()
        await expect(page.get_by_role("dialog")).to_have_count(0)
        assert len(api.saves) == 1
    elif case.startswith("preview-"):
        action = case.removeprefix("preview-")
        api.preview_actions = ["hold" if action == "timeout" else action]
        start = time.monotonic()
        await page.locator("[data-schedule-preview]").click()
        await expect(form.locator('[role="alert"]')).to_be_visible(timeout=35000)
        if action == "timeout":
            assert time.monotonic() - start >= 30
            print(f"  actual preview deadline: {time.monotonic() - start:.3f}s", flush=True)
            api.release.set()
            await asyncio.wait_for(api.finished.wait(), 5)
            await settle(page)
        await expect(confirm).to_have_count(0)
        await submit_event(page)
        assert len(api.previews) == 1 and not api.saves
        await previewed(page)
        assert len(api.previews) == 2 and not api.saves
    elif case.startswith("save-"):
        action = case.removeprefix("save-")
        api.save_actions = ["hold" if action == "timeout" else action]
        await previewed(page)
        await confirm.check()
        start = time.monotonic()
        await submit_event(page, 2)
        failure = page.locator("[data-schedule-save-failure]")
        await expect(failure).to_be_visible(timeout=35000)
        if action == "timeout":
            assert time.monotonic() - start >= 30
            print(f"  actual save deadline: {time.monotonic() - start:.3f}s", flush=True)
            api.release.set()
            await asyncio.wait_for(api.finished.wait(), 5)
            await settle(page)
            await expect(failure).to_be_visible()
        await submit_event(page, 2)
        assert len(api.saves) == 1
        if action == "422":
            await confirm.check()
            await save.click()
            await expect(page.get_by_role("dialog")).to_have_count(0)
            assert len(api.saves) == 2
        else:
            await expect(save).to_be_disabled()
            await page.screenshot(path=str(output / f"{case}.png"), full_page=True)
    elif case.startswith(("context-", "close-")):
        _, phase, change = case.split("-")
        if phase == "save":
            api.save_actions = ["hold"]
            await previewed(page)
            await confirm.check()
            await save.click()
        else:
            api.preview_actions = ["hold"]
            await page.locator("[data-schedule-preview]").click()
        await asyncio.wait_for(
            (api.save_received if phase == "save" else api.preview_received).wait(), 5
        )
        await expect(save).to_be_disabled()
        if change == "close":
            await page.keyboard.press("Escape")
            await expect(page.get_by_role("dialog")).to_have_count(0)
            await page.locator(".taskCard .formRow button").first.click()
        else:
            next_context = {
                "project": {"projectId": OTHER_PROJECT},
                "actor": {"actorId": "00000000-0000-4000-8000-000000000099"},
                "csrf": {"csrfToken": "d" * 32},
                "aba": {"projectId": OTHER_PROJECT},
            }[change]
            await page.evaluate("next => window.updateSubmissionTestContext(next)", next_context)
            if change != "csrf":
                await expect(page.get_by_role("dialog")).to_have_count(0)
                if change == "aba":
                    await page.locator(".taskCard .formRow button").first.wait_for()
                    await page.evaluate(
                        "projectId => window.updateSubmissionTestContext({projectId})", PROJECT
                    )
                await page.locator(".taskCard .formRow button").first.click()
        await expect(confirm).to_have_count(0)
        api.release.set()
        await asyncio.wait_for(api.finished.wait(), 5)
        await settle(page)
        await expect(page.get_by_role("dialog")).to_be_visible()
        await expect(confirm).to_have_count(0)
        await expect(save).to_be_disabled()
        await submit_event(page)
        assert len(api.saves) == (1 if phase == "save" else 0)
    else:
        raise AssertionError(case)
    assert not api.posts
    for record in api.previews + api.saves:
        assert record["csrf"] == "c" * 32


async def check(url: str, output: Path, only: str | None) -> None:
    """実 30 秒 timeout を含む独立 case。静的資産以外の通信は全拒否する。"""

    address = urlsplit(url)
    if address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Browser fixture URL must use loopback")
    output.mkdir(parents=True, exist_ok=True)
    cases = [
        ("language", language, width) for language in ("zh", "ja", "en") for width in (390, 1440)
    ]
    names = [
        "gap-run",
        "gap-end",
        "fold-run",
        "fold-end",
        "definition-aba",
        "same-tick-edit-submit",
        "preview-double",
        "late-preview-aba",
    ]
    names += [
        "preview-" + value
        for value in (
            "empty",
            "naive",
            "invalid-date",
            "unordered",
            "201",
            "500",
            "drop",
            "timeout",
        )
    ]
    names += ["save-" + value for value in ("500", "drop", "timeout", "422", "403")]
    names += [
        f"context-{phase}-{change}"
        for phase in ("preview", "save")
        for change in ("project", "actor", "csrf", "close", "aba")
    ]
    cases += [(name, "en", 390) for name in names]
    completed = 0
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            for case, language, width in cases:
                label = f"{case}-{language}-{width}"
                if only and only not in label:
                    continue
                zone = "America/New_York" if case.startswith(("gap", "fold")) else "UTC"
                context = await browser.new_context(
                    viewport={"width": width, "height": 1000}, timezone_id=zone
                )
                # abort を無視する遅い transport でも、UI owner が旧成功を捨てる必要がある。
                await context.add_init_script("""(() => {
                  const fetchOriginal = window.fetch.bind(window);
                  window.fetch = (input, init = {}) => fetchOriginal(input,
                    String(input).includes('/schedules') ? {...init, signal: undefined} : init);
                })()""")
                page = await context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                page.on(
                    "console",
                    lambda message, errors=errors: (
                        errors.append(message.text)
                        if message.type == "error" and "Failed to load resource" not in message.text
                        else None
                    ),
                )
                api = ScheduleApi(f"{address.scheme}://{address.netloc}")
                await context.route("**/*", api.route)
                try:
                    await exercise(page, api, url, case, language, output, width)
                    assert not api.unexpected and not errors, (api.unexpected, errors)
                    completed += 1
                    print(f"{label}: passed", flush=True)
                except BaseException:
                    print(
                        json.dumps(
                            {
                                "case": label,
                                "previews": api.previews,
                                "saves": api.saves,
                                "unexpected": api.unexpected,
                                "errors": errors,
                            }
                        ),
                        flush=True,
                    )
                    await page.screenshot(path=str(output / f"FAILED-{label}.png"), full_page=True)
                    raise
                finally:
                    api.release.set()
                    await context.close()
        finally:
            await browser.close()
    print(f"schedule times: {completed}/{len(cases) if not only else completed} passed", flush=True)


def main() -> None:
    """外置 screenshot と既存 loopback entry だけを受け取る。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only")
    args = parser.parse_args()
    asyncio.run(check(args.url, args.output, args.only))


if __name__ == "__main__":
    main()
