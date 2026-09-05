"""生成 FrontendModule の静的拒絶検査と依存白名单を検証する (計画 §24 M2 / `docs/07` §11.2)。

この層が担うのは「危ないものを build 前に落とす」ことだけで、通ったことは安全の証明ではない
(実行時 iframe・`CSP: sandbox`・backend 権限が独立に効いている前提)。したがってここで固定すべきは
**拒絶項が一つも欠けていないこと**——文書の箇条書きと検査の対応が崩れると、「文書上は禁止だが
検査していない」状態が静かに生まれる。
"""

from __future__ import annotations

import pytest

from projectmind.modules import (
    ALLOWED_DEPENDENCIES,
    ModuleSourceFile,
    analyze_module_sources,
    validate_dependencies,
)


def _analyze(code: str) -> tuple[str, ...]:
    """一つの source を検査し、見つかった code の集合を返す。"""

    report = analyze_module_sources((ModuleSourceFile(path="Module.tsx", content=code),))
    return tuple(finding.code for finding in report.findings)


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("const x = eval(payload)", "dynamic_code_evaluation"),
        ("const f = new Function('return 1')", "dynamic_code_evaluation"),
        ("node.innerHTML = value", "dynamic_code_evaluation"),
        ("<div dangerouslySetInnerHTML={{__html: v}} />", "dynamic_code_evaluation"),
        ("await fetch('/api/v1/runs')", "direct_network_access"),
        ("const s = new WebSocket(url)", "direct_network_access"),
        ("const e = new EventSource(url)", "direct_network_access"),
        ("const r = new XMLHttpRequest()", "direct_network_access"),
        ("navigator.sendBeacon(url, body)", "direct_network_access"),
        ("const c = document.cookie", "host_context_access"),
        ("window.parent.document.title = 'x'", "host_context_access"),
        ("window.top.location.href", "host_context_access"),
        ("parent.postMessage(data, '*')", "host_context_access"),
        ("const logo = 'https://cdn.example.com/a.png'", "external_resource_url"),
        ("const m = await import('./' + name)", "dynamic_import"),
        ("const lib = require('side-effect')", "dynamic_import"),
        ("localStorage.setItem('result', JSON.stringify(result))", "persistent_storage_write"),
        ("sessionStorage.setItem('evidence', body)", "persistent_storage_write"),
        ("indexedDB.open('projectmind')", "persistent_storage_write"),
    ],
)
def test_every_documented_rejection_item_is_detected(snippet: str, expected: str) -> None:
    """`docs/07` §11.2 の各箇条書きが実際に検出されることを確認する。

    ここが欠けると「文書上は禁止だが検査していない」状態になり、しかも通ってしまうので気付けない。
    """

    assert expected in _analyze(snippet)


def test_a_plain_presentational_module_passes() -> None:
    """許可された書き方だけの module は通る。全部落とすなら検査の意味がない。"""

    report = analyze_module_sources(
        (
            ModuleSourceFile(
                path="Module.tsx",
                content=(
                    "import { Metric } from '@projectmind/ui'\n"
                    "export function View({ data }: { data: Readonly<Record<string, number>> }) {\n"
                    "  return <Metric items={Object.entries(data)} />\n"
                    "}\n"
                ),
            ),
        )
    )

    assert report.rejected is False
    assert report.findings == ()


def test_findings_point_at_the_offending_line() -> None:
    """指摘は path/line/抜粋を伴う。生成し直させるとき「どこが」を model へ返せる必要がある。"""

    report = analyze_module_sources(
        (
            ModuleSourceFile(
                path="src/View.tsx",
                content="const ok = 1\nconst bad = document.cookie\n",
            ),
        )
    )

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.path == "src/View.tsx"
    assert finding.line == 2
    assert "document.cookie" in finding.excerpt


def test_comment_lines_do_not_trigger_rejection() -> None:
    """行 comment は実行されない。説明文の中の `fetch(` で生成をやり直させるのは無駄。"""

    assert _analyze("// never call fetch( here") == ()


def test_rejection_blocks_the_build_without_a_warning_tier() -> None:
    """「危ないが通す」段階を設けない。許すとその状態が既定になる。"""

    report = analyze_module_sources(
        (ModuleSourceFile(path="a.tsx", content="eval(payload)"),)
    )

    assert report.rejected is True


def test_dependency_outside_the_allowlist_is_refused() -> None:
    """白名单に無い package は拒否する (計画 §24 D5)。"""

    report = validate_dependencies({"dependencies": {"lodash": "^4.0.0"}})

    assert [item.code for item in report.findings] == ["dependency_not_allowed"]


def test_allowlisted_dependencies_pass() -> None:
    """白名单内の registry 指定はそのまま通る。"""

    report = validate_dependencies(
        {"dependencies": {name: "1.0.0" for name in sorted(ALLOWED_DEPENDENCIES)}}
    )

    assert report.rejected is False


@pytest.mark.parametrize(
    "specifier",
    ["git+https://example.com/react.git", "github:someone/react", "file:../react", "link:../react"],
)
def test_non_registry_specifier_is_refused_even_for_an_allowlisted_name(specifier: str) -> None:
    """名前が白名单内でも、指す先が registry 外なら中身は別物になり得る。"""

    report = validate_dependencies({"dependencies": {"react": specifier}})

    assert [item.code for item in report.findings] == ["dependency_specifier_not_allowed"]


@pytest.mark.parametrize("script", ["preinstall", "install", "postinstall", "prepare"])
def test_lifecycle_scripts_are_refused(script: str) -> None:
    """lifecycle script は install 時点で任意コードを走らせる。凍結 lockfile では防げない。"""

    report = validate_dependencies({"scripts": {script: "node evil.js"}})

    assert [item.code for item in report.findings] == ["lifecycle_script_not_allowed"]


def test_ordinary_scripts_are_allowed() -> None:
    """build/test のような通常 script は許す。"""

    report = validate_dependencies({"scripts": {"build": "vite build", "test": "vitest run"}})

    assert report.rejected is False
