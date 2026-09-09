"""文書閲覧版のリンク検証・安全な変換・再現性を守る。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
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
        identity = priority.index("docs/design/domain-model.md")
        self.assertEqual(
            priority[identity : identity + 7],
            [
                "docs/design/domain-model.md",
                "docs/design/project-lifecycle.md",
                "docs/design/document-lifecycle.md",
                "docs/design/authentication.md",
                "docs/design/login-protection.md",
                "docs/design/user-lifecycle.md",
                "docs/design/secret-storage.md",
            ],
        )
        runtime = priority.index("docs/design/agent-runtime.md")
        self.assertEqual(
            priority[runtime : runtime + 5],
            [
                "docs/design/agent-runtime.md",
                "docs/design/user-interactions.md",
                "docs/design/results-evaluation.md",
                "docs/design/run-supervision.md",
                "docs/design/run-budgets.md",
            ],
        )
        acceptance = priority.index("docs/acceptance/jaf-quality.md")
        self.assertEqual(
            priority[acceptance : acceptance + 2],
            ["docs/acceptance/jaf-quality.md", "docs/acceptance/jaf-benchmark.md"],
        )
        operations = priority.index("docs/operations/quickstart.md")
        self.assertEqual(
            priority[operations : operations + 4],
            [
                "docs/operations/quickstart.md",
                "docs/operations/deployment.md",
                "docs/operations/backup-recovery.md",
                "docs/operations/runbook.md",
            ],
        )
        for relative in priority:
            with self.subTest(document=relative):
                self.assertTrue((build_docs.ROOT / relative).is_file())

    def test_operations_legacy_sections_link_to_single_command_source(self) -> None:
        """旧章から正本へ到達でき、破壊的手順を旧手冊へ重複させない。"""

        path = build_docs.DOCS / "operations/runbook.md"
        parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
        document = build_docs.parse_document(path, parser)
        sections = {item["anchor"]: item["text"] for item in build_docs.search_sections(document)}
        for anchor, target in (
            ("环境文件与配置边界", "deployment.md#环境文件与配置边界"),
            ("2-配備前-backup", "backup-recovery.md#配备前备份"),
            ("一致恢复点包含什么", "backup-recovery.md#一致恢复点包含什么"),
            ("取得并检查数据库备份", "backup-recovery.md#取得并检查数据库备份"),
            ("3-migration-と配備", "deployment.md"),
            ("迁移与回退审查", "deployment.md#迁移与回退审查"),
            ("会话协议切换检查", "deployment.md#会话协议切换检查"),
            ("启动与放行", "deployment.md#启动与放行"),
            ("5-database-restore", "backup-recovery.md#数据库恢复"),
            ("恢复前的停止条件", "backup-recovery.md#恢复前的停止条件"),
            ("替换数据库", "backup-recovery.md#替换数据库"),
            ("恢复后验证", "backup-recovery.md#恢复后验证"),
            ("6-application-version-rollback", "backup-recovery.md#应用版本回退"),
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, document.anchors)
                self.assertIn(f"({target})", sections[anchor])
        self.assertNotIn("dropdb --force", document.source)
        self.assertNotIn("pg_dump --username", document.source)
        self.assertNotIn("alembic current", document.source)

    def test_project_lifecycle_has_one_source_and_code_handoffs(self) -> None:
        """Project の旧入口と実装案内を同じ正本へつなぎ、削除コマンドを複製しない。"""

        parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
        target = build_docs.DOCS / "design/project-lifecycle.md"
        document = build_docs.parse_document(target, parser)
        for anchor in (
            "一个例子归档不是停止或删除",
            "项目身份与成员资格",
            "项目选择与失效链接",
            "归档的实际边界",
            "并发修改不能只看有无行锁",
            "删除与数据保留",
            "删除门禁的修正要求",
            "开发接续与验收",
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, document.anchors)
        for relative in (
            "docs/design/domain-model.md",
            "docs/design/authentication.md",
            "docs/design/workspace.md",
            "docs/operations/runbook.md",
            "PJM/backend/README.md",
            "PJM/contracts/README.md",
            "PJM/web/README.md",
        ):
            entry = build_docs.parse_document(build_docs.ROOT / relative, parser)
            with self.subTest(entry=relative):
                self.assertIn("project-lifecycle.md", entry.source)
        self.assertFalse(
            any(token.type == "fence" and token.info == "bash" for token in document.tokens)
        )
        for relative in (
            "../../PJM/backend/README.md#project-とメンバーの管理を追う",
            "../../PJM/web/README.md#project-の切替と管理を追う",
            "../../PJM/contracts/README.md#project-と-membership-の契約を読む",
        ):
            self.assertIn(f"({relative})", document.source)

    def test_document_assets_have_a_separate_source_and_code_handoffs(self) -> None:
        """資産保存と Run 快照を別の正本へ案内し、清理コマンドを導入しない。"""

        parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
        document = build_docs.parse_document(
            build_docs.DOCS / "design/document-lifecycle.md", parser
        )
        for anchor in (
            "一个例子列表消失不等于清理完成",
            "身份目录与公开面",
            "上传的三个边界",
            "保存可靠性的修正要求",
            "读取下载与预览",
            "删除与历史引用",
            "引用保护与清理的修正要求",
            "页面与结果未知",
            "开发接续与验收",
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, document.anchors)
        for relative in (
            "docs/design/README.md",
            "docs/design/domain-model.md",
            "docs/design/project-lifecycle.md",
            "docs/design/resource-snapshots.md",
            "docs/design/workspace.md",
            "docs/development/change-guide.md",
            "docs/operations/runbook.md",
            "docs/operations/backup-recovery.md",
            "PJM/backend/README.md",
            "PJM/contracts/README.md",
            "PJM/web/README.md",
        ):
            entry = build_docs.parse_document(build_docs.ROOT / relative, parser)
            with self.subTest(entry=relative):
                self.assertIn("document-lifecycle.md", entry.source)
        for target in (
            "../../PJM/backend/README.md#project-文書の保存と清理を追う",
            "../../PJM/contracts/README.md#project-文書の保存と読取を読む",
            "../../PJM/web/README.md#project-文書の管理を追う",
        ):
            self.assertIn(f"({target})", document.source)
        self.assertFalse(
            any(token.type == "fence" and token.info == "bash" for token in document.tokens)
        )

    def test_user_lifecycle_legacy_sections_link_to_design_source(self) -> None:
        """会話の旧章を保ち、管理 API の説明を二箇所へ複製しない。"""

        path = build_docs.DOCS / "design/authentication.md"
        parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
        document = build_docs.parse_document(path, parser)
        sections = {item["anchor"]: item["text"] for item in build_docs.search_sections(document)}
        for anchor, target in (
            ("用户生命周期与管理事务", "user-lifecycle.md"),
            ("用户操作与公开面", "user-lifecycle.md#用户操作与目标公开面"),
            ("事务与并发", "user-lifecycle.md#事务与并发"),
            ("生效与界面", "user-lifecycle.md#生效与界面"),
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, document.anchors)
                self.assertIn(f"({target})", sections[anchor])
        self.assertNotIn("POST /users/", document.source)
        self.assertNotIn("GET /users/", document.source)

    def test_runtime_interaction_anchor_leads_to_its_single_source(self) -> None:
        """旧 Runtime の引用を保ち、質問・回答・評価の正本と契約へ接続する。"""

        parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
        runtime = build_docs.parse_document(build_docs.DOCS / "design/agent-runtime.md", parser)
        sections = {item["anchor"]: item["text"] for item in build_docs.search_sections(runtime)}
        self.assertIn("8-用户交互协议", runtime.anchors)
        self.assertIn("(user-interactions.md)", sections["8-用户交互协议"])
        self.assertNotIn("交互必须包含", sections["8-用户交互协议"])

        interactions = build_docs.parse_document(
            build_docs.DOCS / "design/user-interactions.md", parser
        )
        for boundary in (
            "先分清三种人工参与",
            "三个提交边界",
            "普通提问不能代替外部批准",
            "答复界面与结果未知",
            "验收条件",
        ):
            with self.subTest(boundary=boundary):
                self.assertIn(boundary, interactions.anchors)
        for contract in (
            "tools/interaction.request/v1/request.schema.json",
            "runs/interaction-response/v1/request.schema.json",
            "runs/interaction-response/v1/response.schema.json",
        ):
            with self.subTest(contract=contract):
                self.assertIn(f"(../../PJM/contracts/{contract})", interactions.source)

    def test_result_legacy_sections_lead_to_the_same_design(self) -> None:
        """旧 Runtime/Workspace の章を保持し、原結果と修訂の規則を一つに集約する。"""

        parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
        for relative, anchor, target in (
            ("design/agent-runtime.md", "10-结果与输出验证", "results-evaluation.md"),
            (
                "design/workspace.md",
                "73-evaluation",
                "results-evaluation.md#修订指向哪份原值",
            ),
        ):
            with self.subTest(document=relative):
                document = build_docs.parse_document(build_docs.DOCS / relative, parser)
                sections = {
                    item["anchor"]: item["text"] for item in build_docs.search_sections(document)
                }
                self.assertIn(f"({target})", sections[anchor])

        result = build_docs.parse_document(build_docs.DOCS / "design/results-evaluation.md", parser)
        for anchor in (
            "先分清四种事实",
            "结果校验的实际保证",
            "引用可信性的修正要求",
            "修订指向哪份原值",
            "提交未知与界面责任",
            "验收条件",
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, result.anchors)

    def test_evaluation_design_example_matches_the_request_schema(self) -> None:
        """文書の修訂例にも公開契約を適用し、原値や actor を入力へ混入させない。"""

        parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
        document = build_docs.parse_document(
            build_docs.DOCS / "design/results-evaluation.md", parser
        )
        schema = json.loads(
            (build_docs.ROOT / "PJM/contracts/evaluations/v1/create-request.schema.json").read_text(
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
                self.assertEqual(example["revisions"][0]["pointer"], "/summary")

    def test_user_contract_index_covers_existing_schemas(self) -> None:
        """機械用の管理 Schema と人が読む入口を対応させ、追加時の案内漏れを防ぐ。"""

        contracts = build_docs.ROOT / "PJM/contracts"
        source = (contracts / "README.md").read_text(encoding="utf-8")
        schemas = sorted((contracts / "users/v1").glob("*.schema.json"))
        self.assertTrue(schemas)
        for schema in schemas:
            with self.subTest(schema=schema.name):
                self.assertIn(f"({schema.relative_to(contracts).as_posix()})", source)
        self.assertIn("(../../docs/development/contract-workflow.md#遇到未接齐的交付链)", source)

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
