"""通用報告と Markdown 抜粋を実 App + mock API で読み、外部接続を検知する。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from check_artifacts import ARTIFACT
from check_projects import PROJECT, layout, messages
from check_workspace_reports import WorkspaceApi
from playwright.async_api import async_playwright, expect

EXCERPT = """# 対象文書の確認

**2 冊**のテスト仕様書を確認しました。

| 対象 | 確認した内容 | 状態 |
| --- | --- | --- |
| 送信処理 | 入力値・操作順序・期待結果 | 確認済み |
| 履歴照会 | 検索条件・表示項目 | 要確認 |

- 入力値と期待結果の対応を確認
- 実アプリケーションでの実行は対象外

```text
document_id: example-001
```

<script>window.reportInjection = true</script>
<style>body { display: none }</style>
![diagram](https://example.invalid/private.png)
[external](https://example.invalid/)
"""


class ReadingApi(WorkspaceApi):
    """既存結果 fixture に Markdown と原 Evidence の参照だけを設定する。"""

    def __init__(self, url: str, language: str) -> None:
        super().__init__(url, language)
        evidence = self.body["evidence"][0]
        evidence["excerpt"] = EXCERPT
        evidence["source_locator"]["name"] = "送信仕様.xlsx"
        self.body["result"]["summary"] = (
            "2 冊のテスト仕様書を確認しました。**期待結果の補足が必要**な箇所があります。"
        )
        self.body["result"]["data"].update(
            status="PARTIAL",
            deliverables=[
                {
                    "key": "document",
                    "kind": "report",
                    "artifact_ref": ARTIFACT,
                    "title": "テスト仕様書の確認結果",
                    "description": "文書の構成と手順・期待結果の整合性を確認しました。",
                    "content": (
                        "| 文書 | 対象範囲 |\n| --- | --- |\n"
                        "| 送信処理 | 入力・送信操作 |\n| 履歴照会 | 検索・結果表示 |"
                    ),
                    "evidence_refs": [evidence["evidence_ref"]],
                }
            ],
            findings=[
                {
                    "key": "finding",
                    "title": "期待結果の確認が必要",
                    "severity": "warning",
                    "detail": (
                        "**履歴照会**の結果項目について、判定条件が明示されていません。\n\n"
                        "- 正常系の表示項目\n- 対象が存在しない場合の表示"
                    ),
                    "evidence_refs": [evidence["evidence_ref"]],
                }
            ],
            limitations=["実アプリケーションでの実行および送達確認は行っていません。"],
            open_questions=[],
            effects=[{"proposal_ref": "cp_fixture", "status": "APPLIED", "summary": "保存を確認"}],
        )


async def check(url: str, output: Path) -> None:
    """三語・両テーマ・狭幅で原文切替、lazy 表示と安全性を確認する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            for language in ("ja", "zh", "en"):
                for theme in ("light", "dark"):
                    api = ReadingApi(url, language)
                    context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                    await context.add_init_script(
                        f"localStorage.setItem('skillmind.theme', '{theme}')"
                    )
                    await context.route("**/*", api.route)
                    page = await context.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))
                    try:
                        await page.goto(
                            f"{url}#/workspace?project={PROJECT}&module=00000000-0000-4000-8000-000000000501"
                        )
                        labels = await messages(page, language)
                        await page.get_by_role(
                            "tab", name=labels["workspace"]["queue"]["reports"], exact=True
                        ).click()
                        await expect(page.locator(".reportSummary h3")).to_have_text(
                            labels["runResult"]["reportTitle"]
                        )
                        await expect(page.locator(".reportSummary strong")).to_contain_text(
                            "期待結果の補足が必要"
                        )
                        await expect(page.locator(".outcomePartial")).to_contain_text(
                            labels["runResult"]["completionStates"]["PARTIAL"]
                        )
                        await expect(page.locator(".resultReportBody table")).not_to_be_visible()
                        await page.locator(".deliverableContent > summary").click()
                        await expect(page.locator(".resultReportBody table")).to_be_visible()
                        assert (
                            await page.locator(".workspaceReports").evaluate(
                                "e => e.getBoundingClientRect().width"
                            )
                            > 1000
                        )
                        assert (
                            await page.locator(".deliverableContent").evaluate(
                                "e => e.getBoundingClientRect().height"
                            )
                            < 400
                        )
                        await expect(
                            page.get_by_text(labels["runResult"]["noOpenQuestions"], exact=True)
                        ).to_have_count(0)
                        await expect(page.locator(".outcomeEffects")).not_to_have_attribute(
                            "open", ""
                        )
                        assert await page.locator(".reportSummary").evaluate(
                            "e => e.getBoundingClientRect().top"
                        ) < await page.locator(".resultActions").evaluate(
                            "e => e.getBoundingClientRect().top"
                        )
                        await expect(page.locator(".evidenceCard .readingMarkdown")).to_have_count(
                            0
                        )
                        await expect(page.locator(".outcomeReportFrame")).to_have_count(0)
                        assert (
                            await page.locator(".runLauncher > button").evaluate(
                                "e => e.getBoundingClientRect().width"
                            )
                            < 400
                        )
                        for width in (1366, 1920, 390):
                            await page.set_viewport_size({"width": width, "height": 1000})
                            await layout(page)
                        await page.set_viewport_size({"width": 1440, "height": 1000})
                        await page.screenshot(
                            path=str(output / f"report-{language}-{theme}.png"), full_page=True
                        )
                        await (
                            page.locator(".outcomeCard")
                            .first.get_by_role(
                                "button", name=labels["runResult"]["viewExcerpt"], exact=True
                            )
                            .click()
                        )
                        drawer = page.get_by_role(
                            "dialog", name=labels["runResult"]["evidenceTitle"], exact=True
                        )
                        preview = drawer.locator(".readingMarkdown")
                        await expect(drawer.locator(".evidenceCard > summary strong")).to_have_text(
                            "送信仕様.xlsx"
                        )
                        await expect(drawer.locator(".evidenceMetadata dl")).not_to_be_visible()
                        await drawer.locator(".evidenceMetadata > summary").click()
                        await expect(drawer.locator(".evidenceMetadata dl")).to_be_visible()
                        await drawer.locator(".evidenceMetadata > summary").click()
                        await expect(
                            preview.get_by_role("heading", name="対象文書の確認")
                        ).to_be_visible()
                        await expect(preview.locator("table")).to_be_visible()
                        await expect(
                            preview.locator("script, style, img, iframe, a, input")
                        ).to_have_count(0)
                        assert await page.evaluate("window.reportInjection === undefined")
                        await drawer.get_by_role(
                            "button", name=labels["runResult"]["excerptSource"], exact=True
                        ).click()
                        assert await drawer.locator(".excerptSource").text_content() == EXCERPT
                        await drawer.get_by_role(
                            "button", name=labels["runResult"]["artifacts"]["preview"], exact=True
                        ).click()
                        await expect(preview.locator("table")).to_be_visible()
                        await drawer.locator(".evidenceCard > summary").click()
                        await expect(preview).to_have_count(0)
                        await drawer.locator(".evidenceCard > summary").click()
                        await expect(preview.locator("table")).to_be_visible()
                        for width in (1366, 1920, 390):
                            await page.set_viewport_size({"width": width, "height": 1000})
                            await layout(page)
                        await page.set_viewport_size({"width": 1440, "height": 1000})
                        await page.screenshot(path=str(output / f"excerpt-{language}-{theme}.png"))
                        await page.keyboard.press("Escape")
                        await expect(drawer).not_to_be_visible()
                        await (
                            page.locator(".outcomeCard")
                            .first.get_by_role(
                                "button",
                                name=labels["runResult"]["artifacts"]["preview"],
                                exact=True,
                            )
                            .click()
                        )
                        await expect(
                            page.get_by_role("dialog").get_by_role(
                                "heading", name="Desktop login report"
                            )
                        ).to_be_visible()
                        await expect(page.locator(".artifactMarkdownPreview")).to_be_visible()
                        await expect(page.locator("iframe.runReportPreview")).to_have_count(0)
                        await page.keyboard.press("Escape")
                        read_count = api.preview_requests
                        api.body["result"]["data"]["deliverables"][0]["artifact_ref"] = (
                            "art_missing"
                        )
                        api.body["result"]["data"]["deliverables"][0]["title"] = (
                            "Unmatched deliverable"
                        )
                        await page.locator(".reportToolbar button").click()
                        await expect(page.locator(".outcomeCardHeading h5").first).to_have_text(
                            "Unmatched deliverable"
                        )
                        await (
                            page.locator(".outcomeCard")
                            .first.get_by_role(
                                "button",
                                name=labels["runResult"]["artifacts"]["preview"],
                                exact=True,
                            )
                            .click()
                        )
                        await expect(
                            page.get_by_text(
                                labels["runResult"]["artifacts"]["previewUnavailable"], exact=True
                            )
                        ).to_be_visible()
                        await expect(page.locator(".modalOverlay:visible")).to_have_count(0)
                        assert api.preview_requests == read_count == 1
                        assert not errors and not api.failures and not api.unexpected, (
                            errors,
                            api.failures,
                            api.unexpected,
                        )
                        print(f"PASS report/excerpt {language} {theme}", flush=True)
                    finally:
                        await context.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    target = urlsplit(args.url)
    if (
        target.scheme != "http"
        or target.hostname not in {"127.0.0.1", "localhost"}
        or not target.path.endswith("/tests/browser/projects.html")
    ):
        parser.error("Only loopback projects.html is allowed")
    asyncio.run(check(args.url, args.output))
