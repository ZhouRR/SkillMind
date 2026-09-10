"""文書閲覧版のリンク検証・安全な変換・再現性を守る。"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError
from markdown_it import MarkdownIt

from scripts import build_docs


class DocumentParsingTests(unittest.TestCase):
    """小さな一時文書で parser と参照境界の異常系を確認する。"""

    def setUp(self) -> None:
        """実文書を変更せず、一時 root だけを検査対象にする。"""

        directory = tempfile.TemporaryDirectory(prefix="skillmind-doc-test-")
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

    def test_nested_documents_use_the_same_heading_rules(self) -> None:
        """配置先にかかわらず見出し階層の検査を省略しない。"""

        nested = self.docs / "design" / "nested.md"
        nested.parent.mkdir()
        nested.write_text("# Page\n\n### Skipped section\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "heading level jumps"):
            build_docs.parse_document(nested, self.parser)

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

    def test_legacy_viewer_link_checks_the_consolidated_target(self) -> None:
        """旧 page の章名を捨てても、新しい実在章への検査を省略しない。"""

        root_readme = self.root / "SKM/README.md"
        anchors = {root_readme: {"backend", "web"}}
        for module in ("backend", "web"):
            with self.subTest(module=module):
                build_docs.validate_link(
                    self.page, f"index.html#SKM/{module}/README.md::旧章", anchors
                )
        with self.assertRaisesRegex(ValueError, "missing viewer anchor"):
            build_docs.validate_link(self.page, "index.html#SKM/contracts/README.md::旧章", anchors)

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
        self.assertEqual([item["level"] for item in sections], ["h1", "h2", "h3", "h4"])
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
    """正本の網羅性・単一責任・公開範囲と再現性を確認する。"""

    def parse(self, relative: str) -> build_docs.Document:
        """実在する正式文書を同じ parser 設定で読み取る。"""

        parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
        return build_docs.parse_document(build_docs.ROOT / relative, parser)

    def test_reading_order_contains_unique_existing_documents(self) -> None:
        """入口と責務の隣接順だけを固定し、見出しの整理は妨げない。"""

        priority = build_docs.FIRST_PAGES
        paths = [path.relative_to(build_docs.ROOT).as_posix() for path in build_docs.source_paths()]
        self.assertEqual(len(priority), len(set(priority)))
        self.assertEqual(paths[: len(priority)], priority)
        self.assertEqual(priority[0], "docs/README.md")
        chains = (
            ("overview/product", "overview/architecture", "overview/glossary"),
            ("design/authentication", "design/login-protection", "design/user-lifecycle"),
            ("design/run-creation", "design/resource-snapshots", "design/agent-runtime"),
            ("operations/quickstart", "operations/deployment", "operations/backup-recovery"),
        )
        for chain in chains:
            with self.subTest(chain=chain):
                positions = [priority.index(f"docs/{item}.md") for item in chain]
                self.assertEqual(positions, sorted(positions))
        for relative in priority:
            with self.subTest(document=relative):
                self.assertTrue((build_docs.ROOT / relative).is_file())

    def test_source_list_covers_only_docs_and_central_code_entries(self) -> None:
        """docs は全件を対象にし、実行資産や module README は埋め込まない。"""

        expected = set(build_docs.DOCS.rglob("*.md")) | {
            build_docs.ROOT / relative
            for relative in ("README.md", "SKM/README.md", "SKM/AGENTS.md")
        }
        self.assertEqual(set(build_docs.source_paths()), expected)
        for directory in ("history", "acceptance"):
            self.assertFalse(any((build_docs.DOCS / directory).rglob("*.*")))

    def test_current_documents_do_not_reintroduce_removed_material(self) -> None:
        """削除した専用説明と archive 参照を現行の正本へ戻さない。"""

        for path in build_docs.source_paths():
            with self.subTest(document=path.relative_to(build_docs.ROOT)):
                source = path.read_text(encoding="utf-8-sig")
                self.assertIsNone(re.search("jaf", source, re.IGNORECASE), "Removed topic found")
                self.assertIsNone(
                    re.search(
                        r"(?:\.\./|docs/)(?:history|acceptance)/|delivery-history\.md", source
                    ),
                    "Removed archive link found",
                )

    def test_design_index_covers_every_domain_source(self) -> None:
        """領域の正本を総当たりで案内し、独立設計の孤立を防ぐ。"""

        source = self.parse("docs/design/README.md").source
        designs = sorted((build_docs.DOCS / "design").glob("*.md"))
        self.assertGreater(len(designs), 1)
        for path in designs:
            if path.name == "README.md":
                continue
            with self.subTest(document=path.name):
                self.assertIn(path.name, source)

    def test_roadmap_uses_current_report_with_all_work_items(self) -> None:
        """進捗・優先表・首版範囲・受入条件を保ち、割合や優先順位は固定しない。"""

        document = self.parse("docs/planning/roadmap.md")
        self.assertTrue(
            {"当前执行状态", "开发任务", "首版目标与推进顺序", "首版验收与停止条件"}
            <= document.anchors
        )
        tasks = next(
            section for section in build_docs.search_sections(document)
            if section["anchor"] == "开发任务"
        )
        rows = re.findall(
            r"^\| (\d+) \| \[(R\d{2}) [^\]]+\]\([^)]+\) \| (\d+)% \| ([^|]+) \| ([^|]+) \|$",
            tasks["text"],
            re.MULTILINE,
        )
        self.assertEqual(len(rows), 13)
        self.assertEqual([int(row[0]) for row in rows], list(range(1, 14)))
        self.assertEqual({row[1] for row in rows}, {f"R{number:02d}" for number in range(1, 14)})
        for _, identifier, percentage, status, scope in rows:
            with self.subTest(item=identifier):
                self.assertTrue(0 <= int(percentage) <= 100)
                self.assertTrue(status.strip())
                self.assertTrue(scope.strip())
        self.assertEqual(sum(token.type == "table_open" for token in document.tokens), 1)
        self.assertNotIn("delivery-history", document.source)

    def test_design_headings_use_topics_without_legacy_numbers(self) -> None:
        """整理後の章を旧番号へ戻さず、安定したテーマ名で案内する。"""

        targets = [
            *sorted((build_docs.DOCS / "design").glob("*.md")),
            build_docs.DOCS / "planning/roadmap.md",
        ]
        for path in targets:
            document = self.parse(path.relative_to(build_docs.ROOT).as_posix())
            for section in build_docs.search_sections(document):
                if section["level"] == "h1":
                    continue
                with self.subTest(document=path.name, section=section["title"]):
                    self.assertNotRegex(section["title"], r"^\d+(?:\.\d+)*\.?\s+")
        self.assertIn("开发任务", self.parse("docs/planning/roadmap.md").anchors)

    def test_adjacent_responsibilities_link_to_their_single_sources(self) -> None:
        """会話・入力・結果と運用の分担を、旧空見出しなしで辿れるようにする。"""

        handoffs = {
            "docs/design/authentication.md": ("user-lifecycle.md",),
            "docs/design/agent-runtime.md": ("user-interactions.md", "results-evaluation.md"),
            "docs/design/resource-snapshots.md": ("document-lifecycle.md",),
            "docs/design/project-lifecycle.md": ("document-lifecycle.md",),
            "docs/operations/runbook.md": ("deployment.md", "backup-recovery.md"),
            "SKM/README.md": (
                "project-lifecycle.md",
                "document-lifecycle.md",
                "user-lifecycle.md",
                "contract-workflow.md",
            ),
        }
        for entry, targets in handoffs.items():
            source = self.parse(entry).source
            for target in targets:
                with self.subTest(entry=entry, target=target):
                    self.assertIn(target, source)
        runbook = self.parse("docs/operations/runbook.md").source
        for command in ("dropdb --force", "pg_dump --username", "alembic current"):
            self.assertNotIn(command, runbook)
        self.assertIn("dropdb --force", self.parse("docs/operations/backup-recovery.md").source)
        self.assertIn("alembic current", self.parse("docs/operations/deployment.md").source)

    def test_evaluation_design_example_matches_the_request_schema(self) -> None:
        """修訂例にも実契約を適用し、原値や actor を入力へ混入させない。"""

        document = self.parse("docs/design/results-evaluation.md")
        schema = json.loads(
            (build_docs.ROOT / "SKM/contracts/evaluations/v1/create-request.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        examples = [
            json.loads(token.content)
            for token in document.tokens
            if token.type == "fence" and token.info == "json"
        ]
        self.assertTrue(examples)
        for example in examples:
            with self.subTest(example=example):
                validator.validate(example)
                self.assertTrue(example["revisions"])
                self.assertTrue(example["revisions"][0]["pointer"].startswith("/"))

    def test_view_spec_examples_reject_invalid_shape_and_json(self) -> None:
        """実 Schema の正例に加え、型違反と壊れた JSON も確実に拒否する。"""

        documents = [
            self.parse(path.relative_to(build_docs.ROOT).as_posix())
            for path in build_docs.source_paths()
        ]
        self.assertGreater(build_docs.validate_examples(documents), 0)
        example = next(
            token
            for document in documents
            for token in document.tokens
            if token.type == "fence" and token.info == "json" and '"view_version"' in token.content
        )
        example.content = '{"view_version": "invalid"}'
        with self.assertRaises(ValidationError):
            build_docs.validate_examples(documents)
        example.content = '{"view_version":'
        with self.assertRaises(json.JSONDecodeError):
            build_docs.validate_examples(documents)

    def test_code_navigation_uses_one_readme_and_preserves_package_metadata(self) -> None:
        """module 説明を中心へ集約し、package の必須 metadata は維持する。"""

        readme = self.parse("SKM/README.md")
        for module in ("backend", "web", "contracts", "scripts", "skills", "images"):
            with self.subTest(module=module):
                self.assertIn(module, readme.anchors)
                self.assertEqual(
                    build_docs.LEGACY_PAGE_ALIASES[f"SKM/{module}/README.md"],
                    {"page": "SKM/README.md", "anchor": module},
                )
        backend = build_docs.ROOT / "SKM/backend"
        self.assertIn("(../README.md#backend)", (backend / "README.md").read_text())
        self.assertIn('readme = "README.md"', (backend / "pyproject.toml").read_text())
        self.assertIn("backend/README.md", (backend / "Dockerfile").read_text())
        self.assertTrue(list((build_docs.ROOT / "SKM/contracts/users/v1").glob("*.schema.json")))
        self.assertIn("(contracts/users/v1/)", readme.source)
        self.assertIn(
            "(../../SKM/contracts/users/v1/)", self.parse("docs/design/user-lifecycle.md").source
        )

    def test_invalid_legacy_target_fails_the_build(self) -> None:
        """統合先の不存在を unknown-page の黙った fallback にしない。"""

        with (
            patch.object(
                build_docs,
                "LEGACY_PAGE_ALIASES",
                {"SKM/web/README.md": {"page": "SKM/README.md", "anchor": "missing"}},
            ),
            self.assertRaisesRegex(ValueError, "invalid legacy page target"),
        ):
            build_docs.build()

    def test_build_is_deterministic_and_embeds_all_actual_sections(self) -> None:
        """全見出しの level/ID を実 HTML と一致させ、実行 Skill や設定を含めない。"""

        first, count, examples = build_docs.build()
        second, _, _ = build_docs.build()
        self.assertEqual(first, second)
        marker = '<script type="application/json" id="docs-data">'
        payload = json.loads(first.split(marker, 1)[1].split("</script>", 1)[0])
        self.assertEqual(count, len(payload["pages"]))
        self.assertEqual(payload["aliases"], build_docs.LEGACY_PAGE_ALIASES)
        self.assertGreater(examples, 0)
        expected_ids = {
            path.relative_to(build_docs.ROOT).as_posix() for path in build_docs.source_paths()
        }
        self.assertEqual({page["id"] for page in payload["pages"]}, expected_ids)
        self.assertEqual(len(expected_ids), len(payload["pages"]))
        for page in payload["pages"]:
            with self.subTest(page=page["id"]):
                self.assertIn(page["group"], payload["groups"])
                document = self.parse(page["id"])
                self.assertEqual(page["sections"], build_docs.search_sections(document))
                self.assertEqual(
                    {section["anchor"] for section in page["sections"]}, document.anchors
                )
                for section in page["sections"]:
                    self.assertRegex(section["level"], r"^h[1-6]$")
                    self.assertIn(f'<{section["level"]} id="{section["anchor"]}"', page["html"])


if __name__ == "__main__":
    unittest.main()
