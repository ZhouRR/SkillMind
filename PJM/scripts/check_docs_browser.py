"""現行文書の layout・全章・実リンクを、業務システムへ接続せず検証する。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import quote, unquote

from playwright.async_api import Page, Route, async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
MODULES = ("backend", "web", "contracts", "scripts", "skills", "images")
HANDOFFS = (
    ("docs/README.md", "", "docs/overview/product.md"),
    ("docs/overview/product.md", "", "docs/overview/architecture.md"),
    ("docs/design/README.md", "", "docs/design/domain-model.md"),
    ("PJM/README.md", "backend", "docs/design/run-creation.md"),
    ("PJM/README.md", "backend", "docs/design/document-lifecycle.md"),
    ("PJM/README.md", "web", "docs/design/workspace.md"),
    ("PJM/README.md", "contracts", "docs/development/contract-workflow.md"),
    ("PJM/README.md", "scripts", "docs/development/documentation.md"),
    ("PJM/README.md", "skills", "docs/design/skill-contract.md"),
    ("PJM/README.md", "images", "docs/operations/quickstart.md"),
    ("PJM/AGENTS.md", "作業前に読むもの", "docs/development/coding-rules.md"),
    ("docs/planning/roadmap.md", "", "docs/design/results-evaluation.md"),
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
      const heading = document.querySelector('#main :is(h2,h3,h4,h5,h6):focus');
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


def section_targets(pages: list[dict]) -> list[tuple[str, str]]:
    """実在する全章を対象にし、章名の複製や対象の静かな取りこぼしを避ける。"""

    return [
        (item["id"], section["anchor"])
        for item in pages
        for section in item["sections"]
        if section["level"] != "h1"
    ]


async def check_destination(page: Page, book: Path, document: str) -> None:
    """クリック先の文書と focus を確認し、存在しない章の先頭 fallback を拒否する。"""

    fragment = unquote(page.url.split("#", 1)[-1])
    target, anchor = fragment.split("::", 1)
    assert target == document, (target, document)
    await expect(page.locator("#main .page-footer")).to_contain_text(document)
    if anchor:
        await check_heading(page)
        await expect(page.locator("#main :is(h2,h3,h4,h5,h6):focus")).to_have_id(anchor)
    else:
        await expect(page.locator("#main")).to_be_focused()


async def check_handoffs(page: Page, book: Path, output: Path | None) -> None:
    """中心入口から目的設計への実クリックを、desktop と mobile で確認する。"""

    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        for source, anchor, target in HANDOFFS:
            await page.goto(page_url(book, source, anchor))
            scope = page.locator("#main")
            if anchor:
                await check_heading(page)
                scope = page.locator(f"#main h2[id='{anchor}']").locator(
                    f"xpath=following-sibling::*[preceding-sibling::h2[1][@id='{anchor}']]"
                )
            link = scope.locator(f'a[href^="#{quote(target, safe="/")}::"]').first
            await expect(link).to_have_count(1)
            await link.click()
            await check_destination(page, book, target)
            await check_layout(page, ("handoff", width, source, target))
        for name, document in (
            ("guide", "docs/README.md"),
            ("readme", "PJM/README.md"),
            ("runtime", "docs/design/agent-runtime.md"),
            ("plan", "docs/planning/roadmap.md"),
        ):
            await page.goto(page_url(book, document))
            await check_layout(page, ("screenshot", width, document))
            await screenshot(page, output, f"{name}-{width}")


async def check_consolidated_entries(page: Page, book: Path) -> None:
    """旧 module README の bookmark が現在の中心章に正規化されることを確認する。"""

    for width in (390, 1440):
        await page.set_viewport_size({"width": width, "height": 1000})
        for module in MODULES:
            await page.goto(page_url(book, f"PJM/{module}/README.md", "旧章"))
            await expect(page).to_have_url(page_url(book, "PJM/README.md", module))
            await check_heading(page)
            await page.reload()
            await check_heading(page)
            await check_layout(page, ("legacy bookmark", width, module))


async def check_navigation(page: Page, book: Path, pages: list[dict]) -> None:
    """検索、削除資料の非収録、履歴操作、keyboard と print の公開動作を確認する。"""

    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.goto(book.as_uri())
    await expect(page.locator("#main h1")).to_have_text("ProjectMind 文書ガイド")
    await expect(page.locator("#include-history")).to_have_count(0)
    assert not any(item["group"] in {"history", "acceptance"} for item in pages)
    assert not any("jaf" in json.dumps(item, ensure_ascii=False).lower() for item in pages)

    search = page.locator("#search")
    await search.fill("run-creation.md")
    first = page.locator("#navigation .search-hit > a").first
    assert "docs/design/run-creation.md::" in (await first.get_attribute("href") or "")
    await first.click()
    await check_destination(page, book, "docs/design/run-creation.md")

    await search.fill("jaf")
    await expect(page.locator("#navigation .search-hit")).to_have_count(0)
    await search.fill("<img data-docs-injection src=x>")
    await expect(page.locator("img[data-docs-injection]")).to_have_count(0)
    await search.fill("")
    await page.locator(".brand").click()
    await expect(page.locator("#main h1")).to_have_text("ProjectMind 文書ガイド")

    await page.goto(page_url(book, "docs/missing.md"))
    await expect(page.locator("#main h1")).to_have_text("文档不存在")
    await page.get_by_role("link", name="返回文档导航", exact=True).click()
    await expect(page.locator("#main h1")).to_have_text("ProjectMind 文書ガイド")

    await page.goto(page_url(book, "PJM/README.md", "backend"))
    await page.locator('#toc-links a[href="#PJM/README.md::web"]').click()
    await check_heading(page)
    await page.go_back()
    await expect(page).to_have_url(page_url(book, "PJM/README.md", "backend"))
    await check_heading(page)
    await page.go_forward()
    await check_heading(page)
    await expect(page).to_have_url(page_url(book, "PJM/README.md", "web"))
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.locator('#toc-links a[href="#PJM/README.md::web"]').click()
    await check_heading(page)

    await page.set_viewport_size({"width": 390, "height": 1000})
    await page.reload()
    await check_heading(page)
    await check_layout(page, "desktop to mobile native reload")
    menu = page.get_by_role("button", name="目录", exact=True)
    await menu.focus()
    await page.keyboard.press("Enter")
    await expect(search).to_be_focused()
    await page.keyboard.press("Escape")
    await expect(menu).to_be_focused()
    await expect(menu).to_have_attribute("aria-expanded", "false")

    await page.set_viewport_size({"width": 1440, "height": 1000})
    await page.evaluate("window.print = () => { window.docsPrinted = true; }")
    await page.get_by_role("button", name="打印本页", exact=True).click()
    assert await page.evaluate("window.docsPrinted === true")
    await page.emulate_media(media="print")
    await expect(page.locator(".sidebar")).to_be_hidden()
    await expect(page.locator(".topbar")).to_be_hidden()
    await expect(page.locator("#main h1")).to_be_visible()
    await page.emulate_media(media="screen")


async def check(book: Path, output: Path | None) -> None:
    """全ページと全章を検証し、ネットワーク依存や実行時 error を失敗にする。"""

    errors: list[str] = []
    requests: list[str] = []

    async def deny_network(route: Route) -> None:
        """HTTP(S) は到達前に遮断し、試行自体を記録する。"""

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
            )
            assert pages, "empty documentation bundle"
            targets = section_targets(pages)
            assert targets, "empty section coverage"
            for width in (1440, 390):
                await page.set_viewport_size({"width": width, "height": 1000})
                for item in pages:
                    await page.goto(page_url(book, item["id"]))
                    await expect(page.locator("#main h1")).to_have_text(item["title"])
                    await check_layout(page, (width, item["id"]))
                print(f"{width}px: {len(pages)} pages passed", flush=True)

            for width in (320, 390, 768, 1440):
                await page.set_viewport_size({"width": width, "height": 1000})
                for size in ("16px", "24px"):
                    for document, anchor in targets:
                        await page.goto(page_url(book, document, anchor))
                        # 同一 page の hash 移動にも、新 page の load にも指定文字サイズを適用する。
                        await page.evaluate(
                            "size => { document.body.style.fontSize = size; showPage(); }", size
                        )
                        await check_layout(page, (width, size, document, anchor))
                        await check_heading(page)
                        await page.reload()
                        await check_layout(page, ("native reload", width, document, anchor))
                        await check_heading(page)
                        await page.evaluate(
                            "size => { document.body.style.fontSize = size; showPage(); }", size
                        )
                        await check_layout(page, ("restored size", width, size, document, anchor))
                        await check_heading(page)
                    print(f"{width}px/{size}: {len(targets)} sections passed", flush=True)
            await page.evaluate("document.body.style.fontSize = ''")
            await check_consolidated_entries(page, book)
            await check_navigation(page, book, pages)
            await check_handoffs(page, book, output)
            assert not errors, errors
            assert not requests, requests
            print(
                json.dumps(
                    {
                        "pages": len(pages),
                        "page_layouts": len(pages) * 2,
                        "sections": len(targets),
                        "section_layouts": len(targets) * 8,
                        "native_reload_layouts": len(targets) * 8,
                        "restored_size_layouts": len(targets) * 8,
                        "handoffs": len(HANDOFFS) * 2,
                        "legacy_bookmarks": len(MODULES) * 2,
                        "javascript_errors": len(errors),
                        "external_requests": len(requests),
                    }
                ),
                flush=True,
            )
        finally:
            await browser.close()


def main() -> None:
    """引数で明示された screenshot 以外は書込まず、既存の book だけを読む。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", type=Path, default=ROOT / "docs/index.html")
    parser.add_argument(
        "--output", type=Path, help="Optional screenshot directory (files overwritten)"
    )
    args = parser.parse_args()
    book = args.book.resolve()
    if not book.is_file():
        parser.error("Documentation bundle not found; run scripts/build_docs.py first")
    output = args.output.resolve() if args.output else None
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
    asyncio.run(check(book, output))


if __name__ == "__main__":
    main()
