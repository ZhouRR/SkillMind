"""実 Workspace で入力文書と保存先を別々に選び、原 token の mock POST を確認する。"""

from __future__ import annotations

import argparse
import asyncio
import json
from itertools import product
from urllib.parse import urlsplit

from check_document_sources import DocumentApiFixture, select_scope
from check_projects import messages
from check_run_submission import confirmed
from playwright.async_api import async_playwright, expect

LIBRARY = "project-library:documents"


async def check(url: str) -> None:
    """能力可用性は合成する。実 DB/MinIO/モデルへ接続せず、選択 UI と送信だけを検証する。"""

    address = urlsplit(url)
    if address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Browser fixture URL must use loopback")
    async with async_playwright() as api:
        browser = await api.chromium.launch()
        try:
            for language, theme, width in product(
                ("zh", "ja", "en"), ("light", "dark"), (390, 1440)
            ):
                fixture = DocumentApiFixture(f"{address.scheme}://{address.netloc}", True)
                fixture.catalog["tasks"][0]["readiness"]["requirements"].append(
                    {
                        "key": "outputs",
                        "kind": "document",
                        "access": "write",
                        "required": True,
                        "status": "AVAILABLE",
                        "reason": "Synthetic provider",
                        "capabilities": ["document.write/v1"],
                        "selection_guidance": None,
                        "candidates": [
                            {
                                "key": LIBRARY,
                                "kind": "document",
                                "provider": "project-library",
                                "label": "Project document library",
                            }
                        ],
                    }
                )
                context = await browser.new_context(viewport={"width": width, "height": 1000})
                await context.add_init_script(f"localStorage.setItem('skillmind.theme', '{theme}')")
                await context.route("**/*", fixture.route)
                page = await context.new_page()
                errors = []
                page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))
                try:
                    await page.goto(url)
                    # 専用 harness は App の theme effect を持たないため、実 CSS の属性を明示する。
                    await page.evaluate(
                        "theme => document.documentElement.dataset.theme = theme", theme
                    )
                    await expect(page.locator("html")).to_have_attribute("data-theme", theme)
                    await page.evaluate(
                        "language => window.updateSubmissionTestContext({language})", language
                    )
                    await page.locator(".runLauncher > button").click()
                    await page.get_by_label("Objective").fill("Confirm separate input and output")
                    sources = await select_scope(page, "ALL")
                    output = page.locator('label[title="outputs"] select')
                    await expect(output).to_have_value("")
                    await page.locator('.runForm button[type="submit"]').click()
                    assert not fixture.posts
                    await output.select_option(LIBRARY)
                    await expect(page.locator(".documentSourceField")).to_have_count(1)
                    labels = (await messages(page, language))["workspace"]["documentSelection"]
                    await expect(page.locator('[title="outputs"]')).to_contain_text(
                        labels["library"]
                    )
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    await page.locator('.runForm button[type="submit"]').click()
                    await confirmed(page)
                    assert len(fixture.posts) == 1
                    body = json.loads(fixture.posts[0]["body"])
                    assert body["sources"] == {**sources, "outputs": LIBRARY}
                    assert not errors and not fixture.unexpected, (errors, fixture.unexpected)
                    print(f"{language}/{theme}/{width}: passed", flush=True)
                finally:
                    await context.close()
        finally:
            await browser.close()


def main() -> None:
    """既存 loopback Vite だけを対象にする。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    asyncio.run(check(parser.parse_args().url))


if __name__ == "__main__":
    main()
