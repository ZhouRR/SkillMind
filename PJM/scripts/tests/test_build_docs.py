"""文書閲覧版のリンク検証・安全な変換・再現性を守る。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from markdown_it import MarkdownIt

from scripts import build_docs


class DocumentParsingTests(unittest.TestCase):
    """小さな一時文書で parser と参照境界の異常系を確認する。"""

    def setUp(self) -> None:
        """実文書を変更せず、一時 root だけを検査対象にする。"""

        directory = tempfile.TemporaryDirectory(prefix="projectmind-doc-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.page = self.docs / "example.md"
        self.parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
        constants = patch.multiple(
            build_docs, ROOT=self.root, DOCS=self.docs, OUTPUT=self.docs / "index.html"
        )
        constants.start()
        self.addCleanup(constants.stop)

    def document(self, source: str) -> build_docs.Document:
        """試験用 Markdown を解析し、生成用 token を返す。"""

        self.page.write_text(source, encoding="utf-8")
        return build_docs.parse_document(self.page, self.parser)

    def test_chinese_and_duplicate_headings_have_stable_anchors(self) -> None:
        """中国語と重複見出しでも章を一意に参照できる。"""

        document = self.document("# 文档\n\n## 当前状态\n\n## 当前状态\n")
        self.assertEqual(document.anchors, {"文档", "当前状态", "当前状态-1"})

    def test_exactly_one_title_is_required(self) -> None:
        """見出しの無い文書と複数の主題を黙って索引化しない。"""

        for source in ("## No title\n", "# One\n\n# Two\n"):
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(ValueError, "one H1"),
            ):
                self.document(source)

    def test_unclosed_fence_is_rejected_even_when_empty(self) -> None:
        """末尾の閉じ忘れによる本文のコード化を検知する。"""

        for source in ("# Page\n\n```", "# Page\n\n```json\n{}\n"):
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(ValueError, "unclosed fence"),
            ):
                self.document(source)

    def test_skipped_heading_level_is_rejected(self) -> None:
        """現行設計の H1 から H3 への飛躍を防ぎ、章 navigation を階層化する。"""

        with self.assertRaisesRegex(ValueError, "heading level jumps from H1 to H3"):
            self.document("# Page\n\n### Orphan section\n")
        self.document("# Page\n\n## Section\n\n### Detail\n\n## Next section\n")

    def test_archived_heading_structure_is_preserved(self) -> None:
        """歴史の章番号・階層を新規約へ合わせて書換える必要を作らない。"""

        archive = self.docs / "history" / "old.md"
        archive.parent.mkdir()
        archive.write_text("# Old plan\n\n### Old section\n", encoding="utf-8")
        document = build_docs.parse_document(archive, self.parser)
        self.assertIn("old-section", document.anchors)

    def test_closed_fence_in_blockquote_is_supported(self) -> None:
        """引用内の正しいコードブロックを閉じ忘れと誤判定しない。"""

        for source in (
            "# Page\n\n> ```json\n> {}\n> ```\n",
            "# Page\n\n- ```text\n  example\n  ```\n",
            "# Page\n\n> > ~~~text\n> > example\n> > ~~~~\n",
            "# Page\n\n```\n```\n",
        ):
            with self.subTest(source=source):
                self.document(source)

    def test_implicit_fence_closure_is_rejected(self) -> None:
        """EOF、引用の終端、不正 indent による暗黙の閉じを許可しない。"""

        for source in (
            "# Page\n\n> ```\n> content\n\noutside\n",
            "# Page\n\n```\n    ```\n",
            "# Page\n\n~~~~\ncontent\n~~~\n",
            "# Page\n\n- ```\n  content\n\noutside\n",
        ):
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(ValueError, "unclosed fence"),
            ):
                self.document(source)

    def test_missing_file_and_anchor_are_rejected(self) -> None:
        """存在する文書へリンクしても、無い章を見逃さない。"""

        document = self.document("# Page\n")
        anchors = {self.page: document.anchors}
        for href, message in (
            ("missing.md", "missing link"),
            ("#missing", "missing anchor"),
        ):
            with self.subTest(href=href), self.assertRaisesRegex(ValueError, message):
                build_docs.validate_link(self.page, href, anchors)

    def test_external_link_requires_no_network(self) -> None:
        """オフライン検証で外部 URL を取得しない。"""

        build_docs.validate_link(self.page, "https://example.invalid/no-network", {})

    def test_viewer_links_are_checked_before_index_exists(self) -> None:
        """初回 build でも閲覧版の文書 ID と章を検証する。"""

        document = self.document("# Page\n\n## 当前\n")
        anchors = {self.page: document.anchors}
        build_docs.validate_link(
            self.page, "index.html#docs/example.md::%E5%BD%93%E5%89%8D", anchors
        )
        for href, message in (
            ("index.html#docs/missing.md::", "missing viewer page"),
            ("index.html#docs/example.md::missing", "missing viewer anchor"),
        ):
            with self.subTest(href=href), self.assertRaisesRegex(ValueError, message):
                build_docs.validate_link(self.page, href, anchors)

    def test_internal_navigation_and_code_links_keep_their_targets(self) -> None:
        """Markdown だけを hash route に変換し、コード参照は file path を保つ。"""

        code = self.root / "example.py"
        code.write_text("pass\n", encoding="utf-8")
        document = self.document("# Page\n\n[Here](#page) [Code](../example.py)\n")
        build_docs.prepare_links(document, {self.page: document.anchors}, {self.page})
        html = self.parser.renderer.render(document.tokens, self.parser.options, {})
        self.assertIn('href="#docs/example.md::page"', html)
        self.assertIn('href="../example.py"', html)

    def test_source_html_is_not_executable(self) -> None:
        """文書に含まれる HTML をブラウザの実行要素へ昇格させない。"""

        document = self.document('# Page\n\n<script>alert("unsafe")</script>\n')
        html = self.parser.renderer.render(document.tokens, self.parser.options, {})
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_search_sections_target_the_actual_heading(self) -> None:
        """長文の本文語とコード例を、その内容が属する章へ結び付ける。"""

        document = self.document(
            "# 文档\n\n前言\n\n## 冻结\n\n清单\n\n### 验证\n\n"
            '```json\n{"document_snapshots": []}\n```\n\n#### 深层\n\n独立回执\n'
        )
        sections = build_docs.search_sections(document)
        self.assertEqual([item["anchor"] for item in sections], ["文档", "冻结", "验证", "深层"])
        self.assertIn("前言", sections[0]["text"])
        self.assertNotIn("清单", sections[0]["text"])
        self.assertEqual(sections[2]["title"], "验证")
        self.assertIn('"document_snapshots"', sections[2]["text"])
        self.assertNotIn("独立回执", sections[2]["text"])
        self.assertTrue(sections[3]["text"].endswith("独立回执"))

    def test_search_sections_preserve_duplicate_ids_and_empty_sections(self) -> None:
        """重複見出しを再採番せず、本文が空でもタイトルから到達できる。"""

        document = self.document("# Page\n\n## Result\n\n## Result\n\nsecond\n")
        sections = build_docs.search_sections(document)
        self.assertEqual([item["anchor"] for item in sections], ["page", "result", "result-1"])
        self.assertEqual(sections[1]["text"], "## Result")
        self.assertIn("second", sections[2]["text"])

    def test_search_keeps_text_before_the_title(self) -> None:
        """H1 より前の説明も本文検索から落とさず、主題へのリンクに含める。"""

        document = self.document("前置说明\n\n# Page\n\n## Section\n\ncontent\n")
        sections = build_docs.search_sections(document)
        self.assertIn("前置说明", sections[0]["text"])
        self.assertEqual(sections[0]["anchor"], "page")


class DocumentationBuildTests(unittest.TestCase):
    """実文書からの build が安定し、公開範囲を広げないことを確認する。"""

    def test_reading_order_contains_only_unique_existing_documents(self) -> None:
        """文書の追加・移動で案内順に重複や存在しない入口を残さない。"""

        priority = build_docs.FIRST_PAGES
        paths = [path.relative_to(build_docs.ROOT).as_posix() for path in build_docs.source_paths()]
        self.assertEqual(len(priority), len(set(priority)))
        self.assertEqual(paths[: len(priority)], priority)
        self.assertEqual(
            priority[:4],
            [
                "docs/README.md",
                "docs/overview/product.md",
                "docs/overview/architecture.md",
                "docs/overview/glossary.md",
            ],
        )
        for relative in priority:
            with self.subTest(document=relative):
                self.assertTrue((build_docs.ROOT / relative).is_file())

    def test_build_is_deterministic_and_excludes_skill_source(self) -> None:
        """同じ文書から同じ成果物を作り、実行 Skill や設定を埋め込まない。"""

        first, count, examples = build_docs.build()
        second, _, _ = build_docs.build()
        self.assertEqual(first, second)
        marker = '<script type="application/json" id="docs-data">'
        payload = json.loads(first.split(marker, 1)[1].split("</script>", 1)[0])
        self.assertEqual(count, len(payload["pages"]))
        self.assertGreater(examples, 0)
        for page in payload["pages"]:
            self.assertTrue(page["id"].endswith(".md"))
            self.assertFalse(page["id"].endswith("/SKILL.md"))
            self.assertTrue(
                page["id"].startswith("docs/")
                or Path(page["id"]).name in {"README.md", "AGENTS.md"}
            )
            self.assertTrue(page["sections"])
            for section in page["sections"]:
                self.assertIn(f'id="{section["anchor"]}"', page["html"])


if __name__ == "__main__":
    unittest.main()
