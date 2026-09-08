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
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "one H1"):
                self.document(source)

    def test_unclosed_fence_is_rejected_even_when_empty(self) -> None:
        """末尾の閉じ忘れによる本文のコード化を検知する。"""

        for source in ("# Page\n\n```", "# Page\n\n```json\n{}\n"):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "unclosed fence"):
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

        self.document("# Page\n\n> ```json\n> {}\n> ```\n")

    def test_missing_file_and_anchor_are_rejected(self) -> None:
        """存在する文書へリンクしても、無い章を見逃さない。"""

        document = self.document("# Page\n")
        anchors = {self.page: document.anchors}
        for href, message in (("missing.md", "missing link"), ("#missing", "missing anchor")):
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


class DocumentationBuildTests(unittest.TestCase):
    """実文書からの build が安定し、公開範囲を広げないことを確認する。"""

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


if __name__ == "__main__":
    unittest.main()
