"""正式 Markdown の参照を検証し、自己完結した閲覧版を生成する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from jsonschema import Draft202012Validator
from markdown_it import MarkdownIt
from markdown_it.rules_block.fence import fence
from markdown_it.rules_block.state_block import StateBlock
from markdown_it.token import Token

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
OUTPUT = DOCS / "index.html"
TEMPLATE = Path(__file__).with_name("docs-viewer.html")
GROUPS = {
    "overview": "开始阅读",
    "planning": "实施计划",
    "design": "设计规范",
    "development": "开发指南",
    "operations": "部署与运维",
    "acceptance": "业务验收",
    "history": "历史归档",
    "code": "工程入口",
}
FIRST_PAGES = [
    "docs/README.md",
    "docs/overview/product.md",
    "docs/overview/architecture.md",
    "docs/overview/glossary.md",
    "docs/planning/roadmap.md",
    "docs/design/README.md",
    "docs/design/domain-model.md",
    "docs/design/authentication.md",
    "docs/design/login-protection.md",
    "docs/design/user-lifecycle.md",
    "docs/design/secret-storage.md",
    "docs/design/skill-contract.md",
    "docs/design/skill-interpretation.md",
    "docs/design/run-creation.md",
    "docs/design/resource-snapshots.md",
    "docs/design/agent-runtime.md",
    "docs/design/run-supervision.md",
    "docs/design/run-budgets.md",
    "docs/design/repository-effects.md",
    "docs/design/task-scheduling.md",
    "docs/design/subagents.md",
    "docs/design/workspace.md",
    "docs/design/task-flow.md",
    "docs/design/generated-modules.md",
    "docs/development/local-development.md",
    "docs/development/change-guide.md",
    "docs/development/contract-workflow.md",
    "docs/development/api-usage.md",
    "docs/development/documentation.md",
    "docs/operations/quickstart.md",
    "docs/operations/deployment.md",
    "docs/operations/backup-recovery.md",
    "docs/operations/runbook.md",
    "docs/acceptance/jaf-quality.md",
    "docs/acceptance/jaf-benchmark.md",
]


@dataclass
class Document:
    """参照検査と表示で共有する一文書の解析結果。"""

    path: Path
    source: str
    tokens: list[Token]
    title: str
    headings: list[dict[str, str]]
    anchors: set[str]


class HtmlLinks(HTMLParser):
    """既存の自己完結 HTML 図のローカル参照を取得する。"""

    def __init__(self) -> None:
        """リンクと anchor を別々に収集する。"""

        super().__init__()
        self.links: list[str] = []
        self.anchors: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """リンク・画像・ID だけを検査対象にする。"""

        values = dict(attrs)
        if values.get("id"):
            self.anchors.add(str(values["id"]))
        key = "href" if tag == "a" else "src" if tag in {"img", "iframe", "script"} else ""
        if key and values.get(key):
            self.links.append(str(values[key]))


def source_paths() -> list[Path]:
    """文書だけを列挙し、Skill 入力・設定・依存 directory は読まない。"""

    paths = list(DOCS.rglob("*.md"))
    paths += [ROOT / "README.md", ROOT / "PJM/README.md", ROOT / "PJM/AGENTS.md"]
    paths += sorted((ROOT / "PJM").glob("*/README.md"))
    order = {name: index for index, name in enumerate(FIRST_PAGES)}
    return sorted(
        set(paths),
        key=lambda path: (
            order.get(path.relative_to(ROOT).as_posix(), len(order)),
            path.relative_to(ROOT).as_posix(),
        ),
    )


def inline_text(token: Token) -> str:
    """書式記号を含まない見出し文字列を生成する。"""

    return "".join(
        child.content for child in token.children or [] if child.type in {"text", "code_inline"}
    )


def checked_fence(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    """引用や list の indent を保持した parser 状態で閉じ fence を確認する。"""

    matched = fence(state, start, end, silent)
    if matched and not silent:
        token = state.tokens[-1]
        last = state.line - 1
        # 原文の行だけを見ると引用の「>」が残り、正しい閉じ fence を拒絶してしまう。
        closing = state.src[state.bMarks[last] + state.tShift[last] : state.eMarks[last]]
        token.meta["closed"] = bool(
            last > start
            and not state.is_code_block(last)
            and state.sCount[last] >= state.blkIndent
            and re.fullmatch(
                re.escape(token.markup[0]) + "{" + str(len(token.markup)) + r",}[ \t]*",
                closing,
            )
        )
    return matched


def parse_document(path: Path, parser: MarkdownIt) -> Document:
    """GitHub 形式の heading ID とコードブロックの閉じを検証する。"""

    source = path.read_text(encoding="utf-8-sig")
    parser.block.ruler.at("fence", checked_fence)
    tokens = parser.parse(source)
    anchors: set[str] = set()
    headings: list[dict[str, str]] = []
    titles: list[str] = []
    levels: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            levels.append((int(token.tag[1:]), token.map[0] + 1 if token.map else 1))
            title = inline_text(tokens[index + 1])
            base = re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-")
            slug, suffix = base, 0
            while slug in anchors:
                suffix += 1
                slug = f"{base}-{suffix}"
            anchors.add(slug)
            token.attrSet("id", slug)
            if token.tag == "h1":
                titles.append(title)
            if token.tag in {"h2", "h3"}:
                headings.append({"title": title, "anchor": slug, "level": token.tag})
        if token.type == "fence" and token.map and not token.meta.get("closed"):
            raise ValueError(f"{path.relative_to(ROOT)}:{token.map[0] + 1}: unclosed fence")
    if len(titles) != 1:
        raise ValueError(f"{path.relative_to(ROOT)}: expected one H1, got {len(titles)}")
    if not path.is_relative_to(DOCS / "history"):
        previous = 0
        for level, line in levels:
            if level > previous + 1:
                raise ValueError(
                    f"{path.relative_to(ROOT)}:{line}: "
                    f"heading level jumps from H{previous} to H{level}"
                )
            previous = level
    return Document(path, source, tokens, titles[0], headings, anchors)


def local_target(origin: Path, href: str) -> tuple[Path, str] | None:
    """外部 URL を取得せず、相対参照だけを実 path に解決する。"""

    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        return None
    path = (origin.parent / unquote(parts.path)).resolve() if parts.path else origin
    return path, unquote(parts.fragment)


def validate_link(origin: Path, href: str, anchors: dict[Path, set[str]]) -> None:
    """ローカル file と Markdown/HTML anchor の実在を確認する。"""

    target = local_target(origin, href)
    if target is None:
        return
    path, anchor = target
    if path == OUTPUT:
        # 初回 build でも、自己完結版への deep link は元 Markdown の索引で検査する。
        if not anchor or anchor == "main":
            return
        page, separator, section = anchor.partition("::")
        viewer_target = (ROOT / page).resolve()
        if not separator or viewer_target not in anchors or viewer_target.suffix != ".md":
            raise ValueError(f"{origin.relative_to(ROOT)}: missing viewer page {href}")
        if section and section not in anchors[viewer_target]:
            raise ValueError(f"{origin.relative_to(ROOT)}: missing viewer anchor {href}")
        return
    if not path.exists():
        raise ValueError(f"{origin.relative_to(ROOT)}: missing link {href}")
    if anchor and path in anchors and anchor not in anchors[path]:
        raise ValueError(f"{origin.relative_to(ROOT)}: missing anchor {href}")


def page_url(path: Path, anchor: str = "") -> str:
    """file:// でも同じ遷移を使えるよう hash に文書と章を保存する。"""

    return "#" + quote(path.relative_to(ROOT).as_posix(), safe="/") + "::" + quote(anchor)


def prepare_links(document: Document, anchors: dict[Path, set[str]], pages: set[Path]) -> None:
    """元 Markdown の参照を検証後、閲覧版専用の参照へ変換する。"""

    for token in document.tokens:
        for child in token.children or []:
            attribute = (
                "href" if child.type == "link_open" else "src" if child.type == "image" else ""
            )
            href = child.attrGet(attribute) if attribute else None
            if not href:
                continue
            validate_link(document.path, href, anchors)
            target = local_target(document.path, href)
            if target:
                path, anchor = target
                if path in pages:
                    value = page_url(path, anchor)
                else:
                    value = quote(Path(os.path.relpath(path, DOCS)).as_posix(), safe="/")
                    parts = urlsplit(href)
                    value += ("?" + parts.query) if parts.query else ""
                    value += ("#" + quote(anchor)) if anchor else ""
                child.attrSet(attribute, value)


def validate_examples(documents: list[Document]) -> int:
    """説明中の ViewSpec JSON を本番と同じ Schema で検証する。"""

    schema = json.loads((ROOT / "PJM/contracts/view-spec/v1alpha1.schema.json").read_text())
    validator = Draft202012Validator(schema)
    count = 0
    for document in documents:
        if document.path.is_relative_to(DOCS / "history"):
            continue  # 当時の例は現行契約に書き換えない。
        for token in document.tokens:
            if token.type == "fence" and token.info == "json" and '"view_version"' in token.content:
                validator.validate(json.loads(token.content))
                count += 1
    return count


def search_sections(document: Document) -> list[dict[str, str]]:
    """本文を見出し単位で索引化し、検索語のある章へ直接案内する。"""

    lines = document.source.splitlines(keepends=True)
    headings = [
        (token, inline_text(document.tokens[index + 1]))
        for index, token in enumerate(document.tokens)
        if token.type == "heading_open" and token.map
    ]
    sections = []
    for index, (token, title) in enumerate(headings):
        start = token.map[0] if index else 0
        end = headings[index + 1][0].map[0] if index + 1 < len(headings) else len(lines)
        # H4 以下も同じ parser の ID を使い、目次の省略や重複見出しで着地点を失わない。
        sections.append(
            {
                "title": title,
                "anchor": token.attrGet("id") or "",
                "text": "".join(lines[start:end]).strip(),
            }
        )
    return sections


def build() -> tuple[str, int, int]:
    """全検証が成功した場合だけ決定的な HTML を組み立てる。"""

    parser = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    documents = [parse_document(path, parser) for path in source_paths()]
    anchors = {document.path: document.anchors for document in documents}
    html_sources: dict[Path, HtmlLinks] = {}
    for path in sorted(DOCS.rglob("*.html")):
        if path == OUTPUT:
            continue
        parsed = HtmlLinks()
        parsed.feed(path.read_text(encoding="utf-8-sig"))
        html_sources[path] = parsed
        anchors[path] = parsed.anchors
    for path, parsed in html_sources.items():
        for link in parsed.links:
            validate_link(path, link, anchors)
    count = validate_examples(documents)
    pages = set(document.path for document in documents)
    rendered = []
    for document in documents:
        prepare_links(document, anchors, pages)
        relative = document.path.relative_to(ROOT).as_posix()
        group = relative.split("/")[1] if relative.startswith("docs/") else "code"
        if group == "README.md":
            group = "overview"
        body = parser.renderer.render(document.tokens, parser.options, {})
        body = body.replace("<table>", '<div class="table-scroll" tabindex="0"><table>')
        body = body.replace("</table>", "</table></div>")
        rendered.append(
            {
                "id": relative,
                "title": document.title,
                "group": group,
                "html": body,
                "sections": search_sections(document),
                "toc": document.headings,
                "source": Path(os.path.relpath(document.path, DOCS)).as_posix(),
                "checksum": hashlib.sha256(document.source.encode()).hexdigest(),
            }
        )
    payload = json.dumps({"groups": GROUPS, "pages": rendered}, ensure_ascii=False)
    # Markdown 中の </script> を JSON script element の終端として解釈させない。
    payload = payload.replace("<", "\\u003c").replace("&", "\\u0026")
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count("__DOCS_PAYLOAD__") != 1:
        raise ValueError("viewer template requires exactly one payload marker")
    return template.replace("__DOCS_PAYLOAD__", payload), len(documents), count


def main() -> int:
    """check は検証のみ、通常実行は生成物だけを更新する。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate without writing files")
    args = parser.parse_args()
    try:
        content, count, examples = build()
        if args.check:
            if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != content:
                raise ValueError("docs/index.html is stale; run python3 scripts/build_docs.py")
        else:
            OUTPUT.write_text(content, encoding="utf-8", newline="\n")
    except (ValueError, OSError) as error:
        print(f"Documentation check failed: {error}")
        return 1
    print(
        f"Documentation {'check' if args.check else 'build'} passed: "
        f"{count} Markdown files, {examples} ViewSpec example(s), local links and anchors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
