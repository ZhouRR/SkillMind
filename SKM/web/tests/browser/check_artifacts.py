"""実 App の添付索引・有界取得・原 context 隔離を全面 mock HTTP で検証する。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from check_accounts import OTHER, PASSWORD, ResponseGate, self_revoke
from check_projects import NEXT_PROJECT, PROJECT, RUN, messages, privacy, settle
from check_result_references import ResultApi
from playwright.async_api import Browser, Page, Route, async_playwright, expect

ARTIFACT = "art_" + "a" * 32
EXTRA = "art_" + "b" * 32
NEXT_RUN = "00000000-0000-4000-8000-000000000099"
CONTENT = 'Original bytes 日本語 中文\n<img data-artifact-injection src="https://artifact.invalid/probe">\n'
PRIVATE = "Artifact fixture internal storage detail must not display"
HEADERS = {
    "Content-Type": "text/plain; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Disposition": "attachment; filename=report.txt",
}
FAILURES = {
    "denied": (403, "forbidden", "denied"),
    "missing": (404, "artifact_not_found", "notFound"),
    "run-missing": (404, "run_not_found", "denied"),
    "invalid-content": (409, "artifact_content_invalid", "contentInvalid"),
    "storage": (503, "artifact_storage_unavailable", "storageUnavailable"),
    "partial": (206, "", "loadFailed"),
    "hash": (200, "", "contentInvalid"),
    "size": (200, "", "contentInvalid"),
    "mime": (200, "", "loadFailed"),
}
INVALID_INDEX = (
    "index-project",
    "index-run",
    "index-extra",
    "index-missing",
    "index-duplicate",
    "index-path",
    "index-mime",
)
LATE = (
    "cancel-late",
    "cancel-late-401",
    "project-late",
    "run-late",
    "actor-late",
    "session-late",
    "timeout",
    "delayed-timer-401",
    "delayed-timer-403",
)


def metadata(project_id: str = PROJECT, run_id: str = RUN, reference: str = ARTIFACT) -> dict:
    """元の UTF-8 byte と一致する十 field の公開回执を作る。"""

    return {
        "artifact_ref": reference,
        "project_id": project_id,
        "run_id": run_id,
        "tool_call_id": "00000000-0000-4000-8000-000000000040",
        "evidence_ref": "ev_artifact_fixture",
        "path": "output/原始报告.txt" if reference == ARTIFACT else "output/unreferenced.txt",
        "size_bytes": len(CONTENT.encode()),
        "mime_type": "text/plain",
        "checksum": "sha256:" + hashlib.sha256(CONTENT.encode()).hexdigest(),
        "created_at": "2026-09-10T00:00:00Z",
    }


class ArtifactApi(ResultApi):
    """実 App fixture を再利用し、索引と byte の応答だけを本ケースが所有する。"""

    def __init__(self, url: str, language: str, mode: str) -> None:
        """保存時チェックと今回の索引を分離し、旧結果から公開記録を捏造しない。"""

        self.run_project = PROJECT
        super().__init__(url, language, "contract")
        self.mode = mode
        self.gate = ResponseGate()
        self.content_calls: list[tuple[str, str, str]] = []
        self.index_calls: list[tuple[str, str]] = []
        result = self.body["result"]
        result["artifact_refs"] = [ARTIFACT]
        result["data"]["artifact_refs"] = [ARTIFACT]
        result["data"]["deliverables"] = [
            {
                "key": "report",
                "kind": "artifact",
                "title": "Original artifact claim",
                "artifact_ref": ARTIFACT,
            }
        ]
        checks = result["validation"]["reference_checks"]
        if mode not in ("v1", "legacy", "v1-conflict"):
            checks.update(
                version="skillmind.result-reference-checks/v2",
                artifacts="RUN_OWNERSHIP_AND_CONTENT",
            )
            result["validation"]["artifact_refs_valid"] = True
        if mode == "legacy":
            result["validation"].pop("reference_checks", None)
            result["artifact_refs"] += ["art_unknown", "https://artifact.invalid/not-an-index"]
        if mode == "v1-conflict":
            result["validation"]["artifact_refs_valid"] = True
        if mode == "v2-missing-flag":
            result["validation"].pop("artifact_refs_valid", None)

    def run(self) -> dict:
        """Project 切替では概要も同じ現在 scope に置き、誤った旧 fixture を返さない。"""

        return {**super().run(), "project_id": self.run_project}

    async def respond(self, route: Route) -> None:
        """不明 request は共有拒否へ渡し、元 ID を固定して body だけ遅延する。"""

        request = route.request
        address = urlsplit(request.url)
        suffix = address.path.removeprefix(self.prefix)
        parts = suffix.split("/")
        if f"{address.scheme}://{address.netloc}" != self.origin or request.method != "GET":
            await super().respond(route)
            return
        if len(parts) == 2 and parts[0] == "runs" and parts[1] == NEXT_RUN:
            await route.fulfill(json={**self.run(), "run_id": NEXT_RUN})
            return
        if suffix == f"runs/{NEXT_RUN}/events":
            # 新しい Run の SSE も原 identity を持たせ、元 Run のイベントを流用しない。
            event = {
                "run_id": NEXT_RUN,
                "run_attempt_id": None,
                "agent_session_id": None,
                "sequence": 1,
                "event_type": "RUN_SNAPSHOT",
                "occurred_at": "2026-09-10T00:00:00Z",
                "payload": {"status": "SUCCEEDED", "row_version": 4},
                "trace_id": None,
            }
            await route.fulfill(
                content_type="text/event-stream",
                body="event: run.snapshot\ndata: " + json.dumps(event) + "\n\n",
            )
            return
        if len(parts) == 5 and parts[0] == "projects" and parts[2] == "runs":
            project_id, run_id, action = parts[1], parts[3], parts[4]
            if action == "detail":
                body = deepcopy(self.body)
                body.update(project_id=project_id, run_id=run_id)
                if project_id != PROJECT or run_id != RUN:
                    # 別 Run に元 Project の凍結文書を借用する不正 fixture を作らない。
                    body.update(document_snapshots=[], selected_sources={})
                await route.fulfill(json=body)
                return
            if action == "evaluations":
                await route.fulfill(json={"evaluations": []})
                return
            if action == "artifacts":
                self.index_calls.append((project_id, run_id))
                items = [metadata(project_id, run_id), metadata(project_id, run_id, EXTRA)]
                if self.mode == "legacy":
                    items = []
                if self.mode == "index-project":
                    items[0]["project_id"] = NEXT_PROJECT
                if self.mode == "index-run":
                    items[0]["run_id"] = NEXT_RUN
                if self.mode == "index-extra":
                    items[0]["internal"] = PRIVATE
                if self.mode == "index-missing":
                    del items[0]["checksum"]
                if self.mode == "index-duplicate":
                    items[1] = deepcopy(items[0])
                if self.mode == "index-path":
                    items[0]["path"] = "output/../private.txt"
                if self.mode == "index-mime":
                    items[0]["mime_type"] = "text/html"
                await route.fulfill(json=items)
                return
        if (
            len(parts) == 7
            and parts[0] == "projects"
            and parts[2] == "runs"
            and parts[4] == "artifacts"
            and parts[6] == "content"
        ):
            self.content_calls.append((parts[1], parts[3], parts[5]))
            assert parts[5] in (ARTIFACT, EXTRA)
            if self.mode in LATE:
                self.gate.received.set()
                await asyncio.wait_for(self.gate.release.wait(), 45)
            status, code, _ = FAILURES.get(self.mode, (200, "", ""))
            if self.mode in LATE and self.mode != "cancel-late":
                status, code = (
                    (403, "forbidden")
                    if self.mode == "delayed-timer-403"
                    else (401, "authentication_required")
                )
            headers = dict(HEADERS)
            content = CONTENT
            if self.mode == "hash":
                content = CONTENT.replace("Original", "Modified")
            if self.mode == "size":
                content += "changed"
            if self.mode == "mime":
                headers["Content-Type"] = "text/html"
            if status >= 400:
                await route.fulfill(
                    status=status, json={"status": status, "code": code, "detail": PRIVATE}
                )
            else:
                await route.fulfill(status=status, headers=headers, body=content)
            self.gate.returned.set()
            return
        await super().respond(route)


async def install_transport(page: Page, mode: str) -> None:
    """実 Blob URL と取得の回数を観測し、abort 無視/無限 stream を明示的に注入する。"""

    await page.add_init_script("""(() => {
      window.artifactUrls = {created: [], revoked: []};
      const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
      URL.createObjectURL = blob => {
        const url = create(blob); window.artifactUrls.created.push(url); return url;
      };
      URL.revokeObjectURL = url => { window.artifactUrls.revoked.push(url); revoke(url); };
    })();""")
    if mode in LATE:
        await page.add_init_script("""(() => {
          const send = window.fetch.bind(window);
          window.fetch = (input, init) => String(input).includes('/artifacts/')
            ? send(input, {...init, signal: undefined}) : send(input, init);
        })();""")
    if mode.startswith(("stream-", "headers-")):
        await page.add_init_script(
            """(mode => {
          const send = window.fetch.bind(window);
          window.artifactStream = {requests: 0, pulls: 0, cancelled: 0};
          window.fetch = (input, init) => {
            if (!String(input).includes('/artifacts/') || !String(input).endsWith('/content')) {
              return send(input, init);
            }
            window.artifactStream.requests++;
            const body = new ReadableStream({
              pull(controller) {
                window.artifactStream.pulls++; if (mode.startsWith('headers-')) return;
                controller.enqueue(new Uint8Array(400000).fill(65)); },
              cancel() { window.artifactStream.cancelled++; }
            }, {highWaterMark: 0});
            const headers = """
            + json.dumps(HEADERS)
            + """;
            if (mode === 'stream-lying') headers['Content-Length'] = '1';
            return Promise.resolve(new Response(body, {headers,
              status: mode === 'headers-401' ? 401 : mode === 'headers-403' ? 403 : 200}));
          };
        })("""
            + json.dumps(mode)
            + ");"
        )


async def scenario(
    browser: Browser, url: str, mode: str, language: str, width: int, output: Path
) -> None:
    """本番 App で索引から取得し、保存 byte と現在 context の副作用だけを受理する。"""

    api = ArtifactApi(url, language, mode)
    context = await browser.new_context(
        viewport={"width": width, "height": 1000}, locale=language, accept_downloads=True
    )
    await context.route("**/*", api.route)
    page = await context.new_page()
    errors: list[str] = []
    downloads = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("download", lambda download: downloads.append(download))
    await install_transport(page, mode)
    name = f"{mode}-{language}-{width}"
    try:
        await page.goto(f"{url}#/workspace?project={PROJECT}&run={RUN}")
        catalog = await messages(page, language)
        labels = catalog["runResult"]["artifacts"]
        panel = page.locator(".runArtifacts")
        if mode in ("v1-conflict", "v2-missing-flag"):
            await expect(page.locator(f'.runFacts dd[title="{RUN}"]')).to_be_visible()
            await page.get_by_role(
                "tab", name=catalog["workspace"]["tabResult"], exact=True
            ).click()
            await expect(
                page.get_by_text(
                    catalog["interactionResponse"]["failures"]["loadFailed"], exact=True
                ).first
            ).to_be_visible()
            await expect(panel).to_have_count(0)
            assert not api.index_calls
        else:
            await expect(panel).to_be_visible()
            if mode in INVALID_INDEX:
                await expect(panel.get_by_role("alert")).to_have_text(
                    labels["failures"]["loadFailed"]
                )
                await expect(panel.locator(".artifactList button")).to_have_count(0)
            elif mode == "legacy":
                await expect(panel.get_by_text(labels["empty"], exact=True)).to_be_visible()
                await expect(panel.locator(".artifactUnmatched")).to_contain_text("art_unknown")
                await expect(panel.locator("a, .artifactList button")).to_have_count(0)
                await expect(page.locator(".resultValidationScope")).to_contain_text(
                    catalog["runResult"]["referenceChecks"]["legacy"]
                )
            else:
                buttons = panel.get_by_role("button", name=labels["download"], exact=True)
                await expect(buttons).to_have_count(2)
                await expect(panel.get_by_text(labels["referenced"], exact=True)).to_be_visible()
                await expect(panel.get_by_text(labels["unreferenced"], exact=True)).to_be_visible()
                key = "artifacts" if mode == "v1" else "artifactsVerified"
                await expect(page.locator(".resultValidationScope")).to_contain_text(
                    catalog["runResult"]["referenceChecks"][key]
                )
                if mode == "timeout":
                    await page.clock.install()
                if mode == "same-tick":
                    await buttons.evaluate_all(
                        "buttons => { buttons[0].click(); buttons[0].click(); buttons[1].click(); }"
                    )
                else:
                    await buttons.first.focus()
                    await page.keyboard.press("Enter")
                if mode in LATE:
                    await asyncio.wait_for(api.gate.received.wait(), 5)
                    if mode.startswith("cancel-late"):
                        await panel.get_by_role("button", name=labels["cancel"], exact=True).click()
                        await expect(panel.locator(".artifactDownload")).to_have_count(0)
                    elif mode == "project-late":
                        api.run_project = NEXT_PROJECT
                        await page.evaluate(
                            "target => location.hash = target",
                            f"/workspace?project={NEXT_PROJECT}&run={NEXT_RUN}",
                        )
                        await expect(
                            page.locator(".workspace .runFacts dd.mono")
                        ).to_have_attribute("title", NEXT_RUN)
                        await expect(panel.locator(".artifactDownload")).to_have_count(0)
                        await expect(buttons).to_have_count(2)
                        assert api.index_calls[-1] == (NEXT_PROJECT, NEXT_RUN)
                    elif mode == "run-late":
                        await page.evaluate(
                            "target => location.hash = target",
                            f"/workspace?project={PROJECT}&run={NEXT_RUN}",
                        )
                        await expect(
                            page.locator(".workspace .runFacts dd.mono")
                        ).to_have_attribute("title", NEXT_RUN)
                        await expect(panel.locator(".artifactDownload")).to_have_count(0)
                        await expect(buttons).to_have_count(2)
                        assert api.index_calls[-1] == (PROJECT, NEXT_RUN)
                    elif mode in ("actor-late", "session-late"):
                        await page.evaluate("location.hash = '/accounts'")
                        await expect(page.locator("[data-account-own]")).to_be_visible()
                        await self_revoke(page, api, catalog["account"])
                        if mode == "actor-late":
                            api.actor = OTHER
                        await page.locator('input[name="email"]').fill(
                            api.users[api.actor]["email"]
                        )
                        await page.locator('input[name="password"]').fill(PASSWORD)
                        await page.locator('button[type="submit"]').click()
                        await expect(page.locator("[data-account-own]")).to_be_visible()
                    elif mode.startswith("delayed-timer"):
                        await page.evaluate("""() => {
                          const elapsed = performance.now() + 31000;
                          Object.defineProperty(performance, 'now', {
                            configurable: true, value: () => elapsed,
                          });
                        }""")
                    else:
                        await page.clock.run_for(30_001)
                        await expect(panel.locator(".artifactDownload")).to_contain_text(
                            labels["failures"]["timeout"]
                        )
                    api.gate.release.set()
                    await asyncio.wait_for(api.gate.returned.wait(), 5)
                    await settle(page)
                    await expect(page.locator('input[name="email"]')).to_have_count(0)
                    if mode.startswith("delayed-timer"):
                        await expect(panel.locator(".artifactDownload")).to_contain_text(
                            labels["failures"]["timeout"]
                        )
                    assert not downloads
                elif mode == "headers-401":
                    await expect(page.locator('input[name="email"]')).to_be_visible()
                elif mode in FAILURES or mode.startswith(("stream-", "headers-")):
                    failure = (
                        FAILURES[mode][2]
                        if mode in FAILURES
                        else "denied"
                        if mode == "headers-403"
                        else "tooLarge"
                    )
                    await expect(panel.locator(".artifactDownload")).to_contain_text(
                        labels["failures"][failure]
                    )
                else:
                    await expect(panel.locator(".artifactDownload")).to_contain_text(
                        labels["delivered"]
                    )
                    assert len(downloads) == 1
                    location = await downloads[0].path()
                    assert location and Path(location).read_bytes() == CONTENT.encode()
                    assert downloads[0].suggested_filename == "原始报告.txt"
                    assert api.content_calls == [(PROJECT, RUN, ARTIFACT)]
                    await panel.get_by_role("button", name=labels["close"], exact=True).click()
                    await expect(buttons.first).to_be_enabled()
                    urls = await page.evaluate("window.artifactUrls")
                    assert len(urls["created"]) == 1 and set(urls["created"]) <= set(
                        urls["revoked"]
                    )
                    await page.reload()
                    await expect(page.locator(".artifactList > li")).to_have_count(2)
                    assert api.content_calls == [(PROJECT, RUN, ARTIFACT)]
                if mode.startswith(("stream-", "headers-")):
                    assert not api.content_calls
                    assert await page.evaluate("window.artifactStream") == {
                        "requests": 1,
                        "pulls": 0 if mode.startswith("headers-") else 3,
                        "cancelled": 1,
                    }
            if mode not in ("success", "v1", "same-tick"):
                assert not downloads
                assert not (await page.evaluate("window.artifactUrls"))["created"]
        await settle(page)
        await privacy(page)
        assert PRIVATE not in await page.locator("body").inner_text()
        assert await page.locator("img[data-artifact-injection]").count() == 0
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert len(context.pages) == 1
        assert not errors and not api.unexpected and not api.failures, (
            errors,
            api.unexpected,
            api.failures,
        )
        if mode in ("success", "v1", "legacy"):
            await panel.screenshot(path=str(output / f"{name}-artifacts.png"))
        await page.screenshot(path=str(output / f"{name}.png"), full_page=True)
        print(f"PASS {name}", flush=True)
    except Exception:
        await page.screenshot(path=str(output / f"FAILED-{name}.png"), full_page=True)
        print("Fixture failures:", errors, api.unexpected, api.failures, flush=True)
        raise
    finally:
        api.gate.release.set()
        api.release.set()
        await context.close()


async def check(url: str, output: Path, cases: list[str] | None = None) -> None:
    """各 case を独立 browser context で実行し、自分の browser を必ず閉じる。"""

    output.mkdir(parents=True, exist_ok=False)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            if cases:
                for mode in cases:
                    await scenario(browser, url, mode, "zh", 390, output)
                return
            for language in ("zh", "ja", "en"):
                for width in (390, 1440):
                    for mode in ("success", "v1", "legacy"):
                        await scenario(browser, url, mode, language, width, output)
                for mode in ("denied", "missing", "invalid-content", "storage"):
                    await scenario(browser, url, mode, language, 390, output)
            for mode in (
                *INVALID_INDEX,
                "partial",
                "hash",
                "size",
                "mime",
                "run-missing",
                "same-tick",
                "stream-absent",
                "stream-lying",
                "headers-401",
                "headers-403",
                *LATE,
                "v1-conflict",
                "v2-missing-flag",
            ):
                await scenario(browser, url, mode, "zh", 390, output)
        finally:
            await browser.close()


def main() -> None:
    """明示された新規 loopback harness と外部出力先以外では実行しない。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--case", action="append", choices=LATE)
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
