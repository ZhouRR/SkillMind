"""生成 module を build してよいかの事前判定 (計画 §24 D4/D5)。

D4 は「内部 npm mirror が提供されない環境では**構築を拒否し、公網 registry へ退避しない**」。
退避経路こそが消したい供給鎖の面なので、判定は fail-closed——分からないときは拒む。

**設定だけを見ても足りない。** 凍結 lockfile の `resolved` が公網 registry を指していれば、
mirror をどう設定していても取得先はそちらになる。つまり「mirror を設定したから安全」は成り立たず、
lockfile 側も見ないと退避は塞げない。ここが本 module の主眼。

なお「内部 mirror であること」は判定できない (内部 address は配備環境が持つ外部入力)。できるのは
**既知の公開 registry を拒む**ことと**未設定を拒む**ことで、D4 の「退避しない」はこの二つで足りる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from projectmind.modules.domain import ModuleVersionError

# 既知の公開 registry。ここへ向くなら、設定であれ lockfile であれ退避が起きている。
PUBLIC_REGISTRY_HOSTS = frozenset(
    {
        "registry.npmjs.org",
        "registry.npmjs.com",
        "registry.yarnpkg.com",
        "npm.pkg.github.com",
        "skimdb.npmjs.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
    }
)

# lockfile 内の取得元 URL。pnpm/npm/yarn いずれの書式でも `http(s)://…` の裸出現を拾えばよい。
_LOCKFILE_URL = re.compile(r"https?://[^\s\"'#,)\]]+", re.IGNORECASE)

# registry 経由でない取得元。凍結 lockfile を迂回して任意の code を引き込める。
_LOCKFILE_NON_REGISTRY = re.compile(
    r"\b(?:git\+(?:https?|ssh)|git|ssh)://|\b(?:github|gitlab|bitbucket|file|link|portal):",
    re.IGNORECASE,
)


class ModuleBuildRefusedError(ModuleVersionError):
    """build の前提が満たされていないことを表す。

    `code` は安定識別子で、運用側が「何を用意すれば通るか」を機械的に読めるようにする。
    """

    def __init__(self, code: str, message: str) -> None:
        """拒否理由の安定 code と人間可読な説明を保持する。"""

        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ModuleBuildPlan:
    """build を許可された構成。registry は必ず具体値を持つ。"""

    registry_url: str


def plan_module_build(*, registry_url: str | None, lockfile: str) -> ModuleBuildPlan:
    """mirror 設定と凍結 lockfile を検査し、build してよいときだけ計画を返す。

    どの拒否も例外にする。戻り値で「駄目でした」を返すと、呼び出し側が確認を忘れたときに
    そのまま build が走る——退避を消すという目的が、呼び出し側の注意力に依存してしまう。
    """

    resolved = (registry_url or "").strip()
    if not resolved:
        # 未設定は「公網で良い」ではない。D4 の既定はここで閉じること。
        raise ModuleBuildRefusedError(
            "registry_not_configured",
            "Module build requires an internal npm registry; it is not configured",
        )
    parts = urlsplit(resolved)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ModuleBuildRefusedError(
            "registry_url_invalid",
            "Module build registry must be an absolute http(s) URL",
        )
    if parts.hostname.lower() in PUBLIC_REGISTRY_HOSTS:
        raise ModuleBuildRefusedError(
            "registry_is_public",
            "Module build registry points at a public registry",
        )
    _reject_public_lockfile_sources(lockfile)
    return ModuleBuildPlan(registry_url=resolved)


def _reject_public_lockfile_sources(lockfile: str) -> None:
    """凍結 lockfile の取得元が公網や registry 外を指していないか確かめる。

    mirror を設定しても、lockfile が `resolved` で公網を名指ししていればそこから取る。
    設定だけ見て通すと「mirror を設定したのに公網から引く」状態を素通りさせる。
    """

    if _LOCKFILE_NON_REGISTRY.search(lockfile):
        raise ModuleBuildRefusedError(
            "lockfile_has_non_registry_source",
            "Module lockfile resolves a dependency outside the npm registry",
        )
    for match in _LOCKFILE_URL.finditer(lockfile):
        hostname = urlsplit(match.group(0)).hostname
        if hostname is not None and hostname.lower() in PUBLIC_REGISTRY_HOSTS:
            raise ModuleBuildRefusedError(
                "lockfile_resolves_to_public_registry",
                "Module lockfile resolves a dependency from a public registry",
            )


__all__ = [
    "PUBLIC_REGISTRY_HOSTS",
    "ModuleBuildPlan",
    "ModuleBuildRefusedError",
    "plan_module_build",
]
