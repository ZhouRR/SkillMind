"""自己完結した文書を実 browser で検証する。アプリ・DB・外部 API は操作しない。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import Page, Route, async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
SECTION_TARGETS = (
    ("docs/design/resource-snapshots.md", "用一个例子理解冻结边界"),
    ("docs/design/resource-snapshots.md", "读取清单和资源摘要"),
    ("docs/design/task-scheduling.md", "时间输入与展示的边界"),
    ("docs/development/contract-workflow.md", "历史数据兼容不等于前后端版本兼容"),
    ("PJM/contracts/README.md", "run-文書契約を読む"),
    ("docs/planning/roadmap.md", "13-当前执行状态"),
    ("docs/operations/runbook.md", "环境文件与配置边界"),
    ("docs/operations/runbook.md", "一致恢复点包含什么"),
    ("docs/operations/runbook.md", "恢复前的停止条件"),
    ("docs/operations/runbook.md", "恢复后验证"),
    ("docs/design/resource-snapshots.md", "产物与访问"),
    ("docs/design/resource-snapshots.md", "一次准备的提交边界"),
    ("docs/design/resource-snapshots.md", "准备中断与再次使用"),
    ("docs/design/resource-snapshots.md", "跨根总量的修正口径待实现"),
    ("docs/design/agent-runtime.md", "74-从领取到模型启动的边界"),
    ("docs/design/agent-runtime.md", "75-取消超时与失去执行权"),
    ("docs/design/run-budgets.md", "现有计时器的覆盖范围"),
    ("docs/operations/runbook.md", "准备故障的只读分诊"),
    ("PJM/backend/README.md", "入力準備の接続を引き継ぐ"),
    ("docs/README.md", "目的から探す"),
    ("docs/design/task-flow.md", "计划身份与显示布局"),
    ("docs/design/task-flow.md", "从发布到历史重放"),
    ("docs/design/task-flow.md", "一个例子计划不等于执行事实"),
    ("docs/design/skill-contract.md", "发布与就绪的判断顺序"),
    ("docs/design/skill-contract.md", "111-版本内容与可见性"),
    ("docs/design/skill-contract.md", "112-可审计的重新启用与回滚"),
    ("docs/design/skill-interpretation.md", "从候选到项目任务的接线"),
)


def page_url(book: Path, document: str, anchor: str = "") -> str:
    """build と同じ文書 ID/章 anchor を file URL の hash に渡す。"""

    return f"{book.as_uri()}#{quote(document, safe='/')}::{quote(anchor, safe='')}"


async def settle(page: Page) -> None:
    """navigation 後の scroll/table 測定が完了するまで描画 frame を待つ。"""

    await page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )


async def check_layout(page: Page, label: object) -> None:
    """横長の表だけを scroll 領域にし、文書全体の画面外へのはみ出しを検出する。"""

    await settle(page)
    result = await page.evaluate("""() => ({
      width: document.documentElement.scrollWidth, viewport: innerWidth,
      labels: [...document.querySelectorAll('.topbar .actions a, .topbar .actions button')]
        .filter(control => control.getClientRects().length).map(control => {
          const range = document.createRange();
          range.selectNodeContents(control);
          return {text: control.textContent, lines: range.getClientRects().length};
        }),
      tables: [...document.querySelectorAll('#main .table-scroll')].map(region => ({
        overflow: region.querySelector('table').getBoundingClientRect().width
          > region.clientWidth + 1,
        hint: !region.previousElementSibling.hidden, focusable: region.tabIndex === 0
      }))
    })""")
    assert result["width"] <= result["viewport"], (label, result)
    assert all(item["lines"] == 1 for item in result["labels"]), (label, result)
    assert all(
        item["overflow"] == item["hint"] == item["focusable"] for item in result["tables"]
    ), (label, result)


async def check_heading(page: Page) -> None:
    """focus だけでなく、章の全見出しが固定 header の下に見えていることを確認する。"""

    await settle(page)
    result = await page.evaluate("""() => {
      const heading = document.querySelector('#main h2:focus, #main h3:focus');
      if (!heading) return null;
      const bar = document.querySelector('.topbar');
      const rect = heading.getBoundingClientRect();
      return {top: rect.top, bottom: rect.bottom, viewport: innerHeight,
        header: getComputedStyle(bar).position === 'sticky'
          ? bar.getBoundingClientRect().bottom : 0};
    }""")
    assert result is not None, (page.url, "no focused section heading")
    assert result["top"] >= result["header"] + 8, (page.url, result)
    assert result["bottom"] <= result["viewport"], (page.url, result)


async def screenshot(page: Page, output: Path | None, name: str) -> None:
    """明示された保存先だけに画面を保存し、既定実行で生成物を増やさない。"""

    if output is not None:
        await page.screenshot(path=str(output / f"{name}.png"))


async def check_search_and_navigation(page: Page, book: Path, output: Path | None) -> None:
    """章検索・履歴の分離・keyboard・戻る/進むを、同じ閲覧版の実操作で確認する。"""

    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(book.as_uri())
    await expect(page.locator("#main h1")).to_have_text("ProjectMind 文書ガイド")
    await screenshot(page, output, "guide-desktop")
    await page.locator("#main a", has_text="設計ガイド").click()
    await expect(page.locator("#main h1")).to_have_text("設計の読み順と責任分担")
    await page.locator("#main a", has_text="Run 作成・幂等").click()
    await expect(page.locator("#main h1")).to_have_text("Run 创建、重发与幂等")
    await page.go_back()
    await expect(page.locator("#main h1")).to_have_text("設計の読み順と責任分担")
    await page.go_forward()
    await expect(page.locator("#main h1")).to_have_text("Run 创建、重发与幂等")

    # 初回表示・brand・不明文書からの復帰を同じ目的別 guide に揃える。
    await page.locator(".brand").click()
    await expect(page.locator("#main h1")).to_have_text("ProjectMind 文書ガイド")
    await page.goto(page_url(book, "docs/not-a-document.md"))
    await expect(page.locator("#main h1")).to_have_text("文档不存在")
    await page.get_by_role("link", name="返回文档导航", exact=True).click()
    await expect(page.locator("#main h1")).to_have_text("ProjectMind 文書ガイド")

    await page.locator("#search").fill("准备世代")
    # 同じ本文語が複数章に現れるのは正常。件数を一件に制限せず、意図した章への移動を守る。
    target = page.locator('#navigation .search-section a[href*="resource-snapshots.md"]').filter(
        has_text="一次准备的提交边界"
    )
    await expect(target).to_have_count(1)
    assert not await page.locator('#navigation a[href*="/history/"]').count()
    await target.click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("一次准备的提交边界")
    await page.evaluate("window.scrollTo(0, 0)")
    await target.click()
    await check_heading(page)

    await page.locator("#search").fill("contract-workflow.md")
    first = page.locator("#navigation .search-hit > a").first
    assert (await first.get_attribute("href") or "").startswith(
        "#docs/development/contract-workflow.md::"
    )
    await first.click()
    await expect(page.locator("#main h1")).to_have_text("公开契约变更与联调")
    await screenshot(page, output, "contract-desktop")

    await page.locator("#search").fill("字段缺失默认成空集合")
    await page.locator("#navigation .search-section a").first.focus()
    await page.keyboard.press("Enter")
    await check_heading(page)
    await expect(page.locator("#navigation mark").first).to_have_text("字段缺失默认成空集合")
    result = await page.evaluate("""() => {
      const node = searchExcerpt({text:'<img src=x onerror=alert(1)> keyword'}, 'keyword');
      return {images:node.querySelectorAll('img').length,
        mark:node.querySelector('mark').textContent, text:node.textContent};
    }""")
    assert result["images"] == 0 and result["mark"] == "keyword" and "<img" in result["text"]
    await page.locator("#search").fill("<img src=x onerror=alert(1)>")
    assert not await page.locator("#navigation img, #navigation script").count()
    await expect(page.locator("#search-status")).to_have_text("0 篇匹配文档")
    await page.locator("#search").fill("2026")
    await page.locator("#include-history").check()
    assert await page.locator('#navigation a[href*="/history/"]').count() > 0
    await page.locator("#search").fill("")
    await page.locator("#include-history").uncheck()

    await page.goto(page_url(book, "docs/design/resource-snapshots.md", SECTION_TARGETS[0][1]))
    await check_heading(page)
    await screenshot(page, output, "resources-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.goto(page_url(book, "docs/design/resource-snapshots.md"))
    await page.locator(".inline-toc summary").focus()
    await page.keyboard.press("Enter")
    await page.locator(".inline-toc a", has_text="读取清单和资源摘要").focus()
    await page.keyboard.press("Enter")
    await check_heading(page)
    await check_layout(page, "resources mobile")
    await screenshot(page, output, "resources-mobile")
    await page.locator("#menu-button").click()
    await page.locator("#search").fill("字段缺失默认成空集合")
    await page.locator("#navigation .search-section a").first.focus()
    await page.keyboard.press("Enter")
    await check_heading(page)
    await expect(page.locator("#menu-button")).to_have_attribute("aria-expanded", "false")
    await page.locator("#menu-button").click()
    await expect(page.locator("#search")).to_be_focused()
    await page.keyboard.press("Escape")
    await expect(page.locator("#menu-button")).to_have_attribute("aria-expanded", "false")
    await expect(page.locator("#menu-button")).to_be_focused()

    await page.goto(page_url(book, "docs/design/task-scheduling.md", SECTION_TARGETS[2][1]))
    await check_heading(page)
    await screenshot(page, output, "schedule-mobile")
    await page.emulate_media(media="print")
    assert not await page.locator(".inline-toc").is_visible()
    assert not await page.locator(".sidebar").is_visible()
    await page.emulate_media(media="screen")
    for anchor in (
        "134-最近文档核对2026-09-08",
        "137-创建与调度设计续整2026-09-08",
        "1310-公开契约交接与章节检索2026-09-08",
        "1312-运维恢复与工作副本边界核对2026-09-08",
        "26-任务流程视图",
    ):
        await page.goto(page_url(book, "docs/planning/roadmap.md", anchor))
        await check_heading(page)
    for name in ("business-structure.html", "technical-architecture.html"):
        await page.goto((book.parent / "overview" / name).as_uri())
        await check_layout(page, name)

    # 復元点の比較表と破壊操作前の条件も、運用者が読む幅で視認できる証拠を残す。
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(page_url(book, "docs/operations/runbook.md", "一致恢复点包含什么"))
    await check_heading(page)
    await screenshot(page, output, "operations-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.goto(page_url(book, "docs/operations/runbook.md", "恢复前的停止条件"))
    await check_heading(page)
    await check_layout(page, "operations mobile")
    await screenshot(page, output, "operations-mobile")

    # 論理 path と実際の公開条件を混同しないよう、新しい準備 flow と中断表も抽看する。
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(page_url(book, "docs/design/resource-snapshots.md", "一次准备的提交边界"))
    await check_heading(page)
    await screenshot(page, output, "preparation-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.goto(page_url(book, "docs/design/resource-snapshots.md", "准备中断与再次使用"))
    await check_heading(page)
    await check_layout(page, "preparation mobile")
    await screenshot(page, output, "preparation-mobile")

    # 新しい入口から Flow の具体例まで、移動と実際の読み幅を確認する。
    await page.goto(page_url(book, "docs/README.md", "目的から探す"))
    await check_heading(page)
    await check_layout(page, "guide mobile")
    await screenshot(page, output, "guide-mobile")
    await page.locator("#main a", has_text="設計ガイド").click()
    await page.locator("#main a", has_text="Flow の具体例").click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("一个例子：计划不等于执行事实")  # noqa: RUF001
    await check_layout(page, "flow mobile")
    await screenshot(page, output, "flow-mobile")
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(page_url(book, "docs/design/task-flow.md", "计划身份与显示布局"))
    await check_heading(page)
    await screenshot(page, output, "flow-desktop")

    # 実装案内から開始条件、計時器、取消処理へ進む実際の読書経路を守る。
    await page.goto(page_url(book, "PJM/backend/README.md", "入力準備の接続を引き継ぐ"))
    await page.locator("#main a", has_text="計時器の正本").click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("现有计时器的覆盖范围")
    await screenshot(page, output, "timeouts-desktop")
    await page.locator("#main a", has_text="Runtime §7.5").click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("7.5 取消、超时与失去执行权")
    await page.goto(page_url(book, "docs/design/agent-runtime.md", "74-从领取到模型启动的边界"))
    await check_heading(page)
    await screenshot(page, output, "runtime-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await check_layout(page, "runtime mobile")
    # 同一 hash への goto は再移動しないため、窄幅で deep link を実際に読み直す。
    await page.reload()
    await check_heading(page)
    await screenshot(page, output, "runtime-mobile")
    await page.goto(page_url(book, "docs/operations/runbook.md", "准备故障的只读分诊"))
    await check_heading(page)
    await check_layout(page, "preparation triage mobile")
    await screenshot(page, output, "triage-mobile")

    # 実行資産の README から、公開状態と回退の設計へ迷わず進めることを守る。
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(page_url(book, "PJM/skills/README.md"))
    await page.locator("#main a", has_text="公開と就緒の判断順").click()
    await check_heading(page)
    await expect(page.locator("#main h3:focus")).to_have_text("发布与就绪的判断顺序")
    await screenshot(page, output, "skill-stages-desktop")
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.reload()
    await check_heading(page)
    await check_layout(page, "skill stages mobile")
    await screenshot(page, output, "skill-stages-mobile")
    await page.locator("#main a", has_text="升级回滚").click()
    await check_heading(page)
    await expect(page.locator("#main h2:focus")).to_have_text("11. 版本、回滚与评价")
    await page.goto(page_url(book, "docs/design/skill-contract.md", "111-版本内容与可见性"))
    await check_heading(page)
    await check_layout(page, "skill rollback mobile")
    await screenshot(page, output, "skill-rollback-mobile")
    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(page_url(book, "docs/design/skill-interpretation.md", "从候选到项目任务的接线"))
    await check_heading(page)
    await screenshot(page, output, "skill-lifecycle-desktop")


async def check(book: Path, output: Path | None) -> None:
    """全ページの layout と重要境界の章移動を検証し、外部接続の試行も失敗にする。"""

    errors: list[str] = []
    requests: list[str] = []

    async def deny_network(route: Route) -> None:
        """文書が CDN/API に依存していたら、到達前に拒否して検証を失敗させる。"""

        requests.append(route.request.url)
        await route.abort()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = await browser.new_context()
            await context.route(re.compile(r"^https?://"), deny_network)
            page = await context.new_page()
            page.set_default_timeout(10_000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(book.as_uri())
            pages = await page.evaluate(
                'JSON.parse(document.getElementById("docs-data").textContent).pages'
                ".map(page => ({id:page.id,title:page.title,group:page.group}))"
            )
            assert pages, "empty documentation bundle"
            for width in (1440, 390):
                await page.set_viewport_size({"width": width, "height": 1000})
                for item in pages:
                    await page.goto(page_url(book, item["id"]))
                    await expect(page.locator("#main h1")).to_have_text(item["title"])
                    await check_layout(page, (width, item["id"]))
                    assert await page.locator(".archive-notice").count() == (
                        1 if item["group"] == "history" else 0
                    )
                print(f"{width}px: {len(pages)} pages passed", flush=True)

            for width in (320, 390, 768, 1440):
                await page.set_viewport_size({"width": width, "height": 1000})
                for size in ("16px", "24px"):
                    await page.evaluate("size => { document.body.style.fontSize = size; }", size)
                    for document, anchor in SECTION_TARGETS:
                        await page.goto(page_url(book, document, anchor))
                        await check_layout(page, (width, size, document, anchor))
                        await check_heading(page)
                        await page.reload()
                        # showPage を呼び直す前に native reload 自体の位置を検査する。
                        await check_layout(page, ("native reload", width, document, anchor))
                        await check_heading(page)
                        # reload が既定文字サイズへ戻すので、拡大した状態を再適用して再表示する。
                        await page.evaluate(
                            "size => { document.body.style.fontSize = size; showPage(); }", size
                        )
                        await check_layout(page, ("refresh", width, size, document))
                        await check_heading(page)
            await page.evaluate("document.body.style.fontSize = ''")
            await check_search_and_navigation(page, book, output)
            assert not errors, errors
            assert not requests, requests
            print(
                json.dumps(
                    {
                        "pages": len(pages),
                        "page_layouts": len(pages) * 2,
                        "section_layouts": len(SECTION_TARGETS) * 8,
                        "native_reload_layouts": len(SECTION_TARGETS) * 8,
                        "refreshed_section_layouts": len(SECTION_TARGETS) * 8,
                        "navigation": "passed",
                        "javascript_errors": len(errors),
                        "external_requests": len(requests),
                    }
                ),
                flush=True,
            )
        finally:
            await browser.close()


def main() -> None:
    """既定の文書を読むだけで実行し、screenshot の保存は明示 option に限定する。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", type=Path, default=ROOT / "docs/index.html")
    parser.add_argument(
        "--output", type=Path, help="Optional screenshot directory (files overwritten)"
    )
    args = parser.parse_args()
    book = args.book.resolve()
    if not book.is_file():
        parser.error(f"Documentation bundle not found: {book}; run scripts/build_docs.py first")
    output = args.output.resolve() if args.output else None
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
    asyncio.run(check(book, output))


if __name__ == "__main__":
    main()
