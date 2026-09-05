"""生成 FrontendModule の静的拒絶検査と依存白名单 (`docs/07` §11.2 / 計画 §24 D5)。

**この検査は唯一の安全境界ではない**——`docs/07` §11.2 が明記するとおり、実行時の iframe、
`Content-Security-Policy: sandbox`、backend 権限がそれぞれ独立に効いている必要がある。ここが担うのは
「そもそも危ないものを build 前に落とす」層だけで、通ったことは安全の証明ではない。

判定は**構文ではなく字句**で行う。生成コードを構文解析するには TypeScript parser が要り、それ自体が
新しい依存かつ攻撃面になる。字句一致は過検出しうるが、その方向の誤りは「安全側に落ちる」だけで済む
——通してはいけないものを通す誤りとは非対称。過検出した場合は生成をやり直させる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# D5 の依存白名单。ここに無い package は build 前に拒否する。lockfile が凍結されていても、
# 白名单を持たないと「凍結済みの危険な依存」を受け入れてしまう。
ALLOWED_DEPENDENCIES = frozenset(
    {
        "react",
        "react-dom",
        "@projectmind/module-sdk",
        "@projectmind/ui",
    }
)

# package lifecycle script は install の時点で任意コードを走らせる。凍結 lockfile では防げない。
FORBIDDEN_PACKAGE_SCRIPTS = frozenset(
    {
        "preinstall",
        "install",
        "postinstall",
        "prepare",
        "prepublish",
        "prepublishOnly",
        "preuninstall",
        "uninstall",
        "postuninstall",
    }
)

# 依存指定のうち、registry 以外を指すもの。凍結 lockfile を迂回して任意の code を引き込める。
_NON_REGISTRY_SPECIFIER = re.compile(
    r"^(?:git\+|git:|github:|gitlab:|bitbucket:|file:|link:|https?:|ssh:)", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class ModuleSourceFile:
    """検査対象の生成ソース一件。"""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class ModuleFinding:
    """一件の拒絶理由。`code` は安定識別子で、画面と log の双方が使う。"""

    code: str
    path: str
    line: int
    excerpt: str


@dataclass(frozen=True, slots=True)
class ModuleStaticReport:
    """静的検査の結果。`rejected` が真なら build させない。"""

    findings: tuple[ModuleFinding, ...] = field(default_factory=tuple)

    @property
    def rejected(self) -> bool:
        """一件でも見つかれば拒絶。警告扱いの段階は設けない。

        「危ないが通す」を許すと、その状態が既定になる。生成はやり直せるので、拒絶の費用は低い。
        """

        return bool(self.findings)


# 各拒絶項の字句 pattern。`docs/07` §11.2 の箇条書きと 1:1 で対応させ、章側が増えたら
# ここも増やす——対応が崩れると「文書上は禁止だが検査していない」状態が静かに生まれる。
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # eval / new Function / 動的 script 注入
    ("dynamic_code_evaluation", re.compile(r"\beval\s*\(")),
    ("dynamic_code_evaluation", re.compile(r"\bnew\s+Function\s*\(")),
    ("dynamic_code_evaluation", re.compile(r"\.innerHTML\s*=")),
    ("dynamic_code_evaluation", re.compile(r"\bdocument\.write\s*\(")),
    ("dynamic_code_evaluation", re.compile(r"dangerouslySetInnerHTML")),
    # 直接 fetch / WebSocket / EventSource / XHR
    ("direct_network_access", re.compile(r"\bfetch\s*\(")),
    ("direct_network_access", re.compile(r"\bnew\s+WebSocket\s*\(")),
    ("direct_network_access", re.compile(r"\bnew\s+EventSource\s*\(")),
    ("direct_network_access", re.compile(r"\bnew\s+XMLHttpRequest\s*\(")),
    ("direct_network_access", re.compile(r"\bnavigator\.sendBeacon\s*\(")),
    # Cookie / 親 DOM / top navigation / opener
    ("host_context_access", re.compile(r"\bdocument\.cookie\b")),
    ("host_context_access", re.compile(r"\bwindow\.(?:parent|top|opener)\b")),
    ("host_context_access", re.compile(r"\bwindow\.location\s*=")),
    ("host_context_access", re.compile(r"\b(?:parent|top)\.postMessage\s*\(")),
    # 未登記の外部 URL (iframe / img / font / script / style)
    ("external_resource_url", re.compile(r"""["'`]https?://""")),
    ("external_resource_url", re.compile(r"""["'`]//[a-z0-9.-]+\.[a-z]{2,}""", re.IGNORECASE)),
    # 動的 import
    ("dynamic_import", re.compile(r"\bimport\s*\(")),
    ("dynamic_import", re.compile(r"\brequire\s*\(")),
    # 永続 storage への書き出し
    ("persistent_storage_write", re.compile(r"\b(?:localStorage|sessionStorage)\b")),
    ("persistent_storage_write", re.compile(r"\bindexedDB\b")),
    ("persistent_storage_write", re.compile(r"\bcaches\.open\s*\(")),
)


def analyze_module_sources(files: tuple[ModuleSourceFile, ...]) -> ModuleStaticReport:
    """生成ソースを `docs/07` §11.2 の拒絶項へ照合する。

    行単位で走査し、見つかった箇所を path/line/抜粋つきで返す。抜粋を返すのは、生成し直させる
    ときに「どこが引っ掛かったか」を model へ具体的に伝えられるようにするため。
    """

    findings: list[ModuleFinding] = []
    for source in files:
        for index, line in enumerate(source.content.splitlines(), start=1):
            stripped = line.strip()
            # 行 comment は実行されない。過検出しても安全側だが、説明文の中の `fetch(` で
            # 生成をやり直させるのは無駄が大きいので除く。block comment までは追わない
            # (字句検査の限界であり、実行時境界が別に効いている前提)。
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            for code, pattern in _RULES:
                match = pattern.search(line)
                if match is None:
                    continue
                findings.append(
                    ModuleFinding(
                        code=code,
                        path=source.path,
                        line=index,
                        excerpt=stripped[:200],
                    )
                )
    return ModuleStaticReport(findings=tuple(findings))


def validate_dependencies(manifest: dict[str, object]) -> ModuleStaticReport:
    """`package.json` 相当の宣言を白名单と lifecycle 禁止へ照合する (計画 §24 D5)。"""

    findings: list[ModuleFinding] = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        declared = manifest.get(section)
        if not isinstance(declared, dict):
            continue
        for name, specifier in declared.items():
            if name not in ALLOWED_DEPENDENCIES:
                findings.append(
                    ModuleFinding(
                        code="dependency_not_allowed",
                        path="package.json",
                        line=0,
                        excerpt=f"{section}.{name}",
                    )
                )
                continue
            if isinstance(specifier, str) and _NON_REGISTRY_SPECIFIER.search(specifier):
                # 白名单の名前でも、指す先が registry 外なら中身は別物になり得る。
                findings.append(
                    ModuleFinding(
                        code="dependency_specifier_not_allowed",
                        path="package.json",
                        line=0,
                        excerpt=f"{section}.{name}={specifier[:100]}",
                    )
                )
    scripts = manifest.get("scripts")
    if isinstance(scripts, dict):
        for name in scripts:
            if name in FORBIDDEN_PACKAGE_SCRIPTS:
                findings.append(
                    ModuleFinding(
                        code="lifecycle_script_not_allowed",
                        path="package.json",
                        line=0,
                        excerpt=f"scripts.{name}",
                    )
                )
    return ModuleStaticReport(findings=tuple(findings))
