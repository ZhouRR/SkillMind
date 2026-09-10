"""PC の概览・長い結果を実 App + 隔離 fixture で確認し、首画面を保存する。"""

# 中国語本文の約物も表示確認の対象なので ASCII へ置換しない。
# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from check_projects import PROJECT, RUN, layout, messages, settle
from check_result_references import CONTRACTS
from check_visual_style import VisualApi
from playwright.async_api import Route, async_playwright, expect

REPORT = (
    "本报告是隔离的界面测试内容，不代表真实项目结论。\n\n"
    "一、评审结论\n"
    "本次检查覆盖需求描述、验收条件与依赖关系。主要路径已有清晰定义，"
    "但异常恢复和权限变更仍需要补充可执行的验收步骤。\n\n"
    "二、建议的处理顺序\n"
    "优先明确失败后的恢复入口，再核对不同角色的数据可见范围。"
    "每项结论需要关联原始依据，避免将格式检查通过理解为业务验收完成。\n\n"
    "三、人工确认\n"
    "请逐项核对未决问题并记录评价。本次结果保留原值，后续修订作为独立的人工评价保存。"
)


class ReadingApi(VisualApi):
    """正常、空一覧と長文だけを模擬し、未知の API や実環境への通信を拒否する。"""

    def __init__(self, url: str, language: str, populated: bool) -> None:
        """公開契約の形を使い、製品 seed を追加せず表示用の内容を独立させる。"""
        super().__init__(url, language)
        self.populated = populated
        self.projects[0].update(name="产品研发工作区", key="product-review")
        self.details[PROJECT].update(self.projects[0])
        self.body["document_snapshots"] = []
        result = self.body["result"]
        result.update(summary="需求评审完成，仍有两项问题需要确认", needs_review=True)
        result["data"].update(
            summary=result["summary"],
            needs_review=True,
            status="PARTIAL",
            deliverables=[
                {"key": "report", "kind": "report", "title": "需求评审报告", "content": REPORT}
            ],
            findings=[
                {
                    "key": "recovery",
                    "title": "补充异常恢复的验收步骤",
                    "severity": "MEDIUM",
                    "detail": "明确中断后如何确认原执行状态。",
                    "evidence_refs": [self.body["evidence"][0]["evidence_ref"]],
                }
            ],
            limitations=["合成测试内容；没有访问真实资源，也没有进行真实业务验收。"],
        )
        self.original_result = deepcopy(result)

    async def respond(self, route: Route) -> None:
        """サーバーの絞り込みと同じ形式で一覧を返し、首頁件数を全件と偽らない。"""
        address = urlsplit(route.request.url)
        suffix = address.path.removeprefix(self.prefix)
        if (
            f"{address.scheme}://{address.netloc}" == self.origin
            and route.request.method == "GET"
            and suffix == f"projects/{PROJECT}/runs"
        ):
            query = parse_qs(address.query)
            template = json.loads((CONTRACTS / "examples/run-history.v1.json").read_text())
            rows = []
            if self.populated:
                pending = "status" in query
                for index, title in enumerate(
                    ("确认需求评审的资源范围", "批准本次变更提案")
                    if pending
                    else (
                        "需求评审完成，仍有两项问题需要确认",
                        "接口文档一致性检查",
                        "发布前检查清单整理",
                        "项目知识库整理",
                        "本周变更影响分析",
                    )
                ):
                    row = deepcopy(template["items"][0])
                    row.update(
                        run_id=RUN if index == 0 else f"00000000-0000-4000-8000-{310 + index:012}",
                        result_summary=title,
                        status=("WAITING_FOR_INPUT" if index == 0 else "WAITING_FOR_APPROVAL")
                        if pending
                        else "SUCCEEDED",
                    )
                    rows.append(row)
            await route.fulfill(
                json={
                    **template,
                    "items": rows,
                    "limit": int(query["limit"][0]),
                    "offset": int(query.get("offset", ["0"])[0]),
                }
            )
            return
        await super().respond(route)


async def check(url: str, output: Path) -> None:
    """PC 三尺寸・三語を主対象とし、手機は中文の退行だけ確認する。"""
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            cases = [
                (lang, width, height, True)
                for lang in ("zh", "ja", "en")
                for width, height in ((1366, 768), (1440, 900), (1920, 1080))
            ]
            cases += [("zh", 1440, 900, False), ("zh", 390, 844, True)]
            for language, width, height, populated in cases:
                api = ReadingApi(url, language, populated)
                context = await browser.new_context(
                    viewport={"width": width, "height": height},
                    locale=language,
                    reduced_motion="reduce",
                )
                await context.route("**/*", api.route)
                page = await context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                try:
                    await page.goto(f"{url}#/?project={PROJECT}")
                    labels = await messages(page, language)
                    await expect(page.locator(".homeHero h2")).to_have_text("产品研发工作区")
                    if populated:
                        await expect(page.locator(".homeRunItem")).to_have_count(5)
                        await expect(page.locator(".pendingItem")).to_have_count(2)
                        await expect(page.locator(".pendingItem").first).to_have_attribute(
                            "href", f"#/workspace?project={PROJECT}&run={RUN}"
                        )
                    else:
                        await expect(
                            page.get_by_text(labels["home"]["emptyNoRuns"])
                        ).to_be_visible()
                    for name in ("home", "workspace"):
                        if name == "workspace":
                            await page.goto(f"{url}#/workspace?project={PROJECT}&run={RUN}")
                            await expect(page.locator(".outcomeCard pre")).to_have_text(REPORT)
                            await expect(page.locator(".validationBrief")).to_be_visible()
                            await expect(page.locator(".resultValidationScope")).not_to_be_visible()
                            if width >= 1366:
                                body = await page.locator(".resultReportBody").bounding_box()
                                report = await page.locator(".outcomeCard pre").bounding_box()
                                assert body and body["width"] >= 850
                                assert report and report["y"] < 650
                                assert await page.locator(".outcomeCard pre").evaluate(
                                    "el => getComputedStyle(el).fontSize === '15px'"
                                )
                        await settle(page)
                        await page.evaluate("document.fonts.ready")
                        await layout(page)
                        await page.screenshot(
                            path=str(
                                output / f"{name}-{language}-{width}"
                                f"-{'filled' if populated else 'empty'}.png"
                            )
                        )
                        if name == "workspace":
                            checks = page.get_by_role(
                                "button", name=labels["runResult"]["reading"]["checks"], exact=True
                            )
                            await page.get_by_role(
                                "button", name=labels["runResult"]["viewExcerpt"], exact=True
                            ).click()
                            await expect(page.locator(".evidenceCard[open] pre")).to_have_text(
                                api.body["evidence"][0]["excerpt"]
                            )
                            await page.keyboard.press("Escape")
                            await page.evaluate("window.scrollTo(0, 0)")
                            await checks.click()
                            await expect(page.locator(".resultValidationScope")).to_be_visible()
                            await page.keyboard.press("Escape")
                            await expect(checks).to_be_focused()
                            evaluation = page.get_by_role(
                                "button", name=labels["runResult"]["manualEvaluation"], exact=True
                            )
                            await evaluation.click()
                            dialog = page.get_by_role(
                                "dialog", name=labels["runResult"]["manualEvaluation"], exact=True
                            )
                            comment = dialog.get_by_label(
                                labels["runResult"]["commentLabel"], exact=True
                            )
                            await comment.fill("Draft retained after closing")
                            await page.keyboard.press("Escape")
                            await expect(evaluation).to_be_focused()
                            await evaluation.click()
                            await expect(comment).to_have_value("Draft retained after closing")
                            # Tab/Shift+Tab を drawer 内で循環させ、背面の操作へ漏らさない。
                            close = dialog.get_by_role(
                                "button", name=labels["elements"]["close"], exact=True
                            )
                            await close.focus()
                            await page.keyboard.press("Shift+Tab")
                            await expect(
                                dialog.get_by_role(
                                    "button",
                                    name=labels["evaluation"]["refreshHistory"],
                                    exact=True,
                                )
                            ).to_be_focused()
                            await page.keyboard.press("Tab")
                            await expect(close).to_be_focused()
                            await layout(page)
                            if width == 1440 and language == "zh" and populated:
                                await dialog.locator(".modalBody").evaluate(
                                    "element => { element.scrollTop = 0 }"
                                )
                                await page.screenshot(
                                    path=str(output / "evaluation-drawer-1440.png")
                                )
                            await page.keyboard.press("Escape")
                        assert not api.unexpected and not api.failures and not errors, (
                            api.unexpected,
                            api.failures,
                            errors,
                        )
                        assert not api.mutations() and api.body["result"] == api.original_result
                        print(f"PASS {name}-{language}-{width}-{populated}", flush=True)
                finally:
                    await context.close()
        finally:
            await browser.close()


def main() -> None:
    """実環境 URL を受け付けず、所有する loopback の専用 harness のみを使う。"""
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
