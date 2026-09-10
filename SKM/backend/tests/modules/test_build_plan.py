"""生成 module の build 前提が fail-closed であることを検証する (計画 §24 D4/M2b)。

守るのは一点——**内部 npm mirror が無いときに公網 registry へ退避しない**こと。退避経路は
消したい供給鎖の面そのものなので、判定の誤りは片側だけが許される: 通してよいものを拒む方向は
生成し直せば済むが、逆は build 時に任意の code を引き込む。
"""

from __future__ import annotations

import pytest

from skillmind.modules import (
    ModuleBuildRefusedError,
    plan_module_build,
)

# mirror だけを指す最小の凍結 lockfile。
_INTERNAL_LOCKFILE = """
lockfileVersion: '9.0'
packages:
  /react@19.0.0:
    resolution: {tarball: https://npm.internal.example/react/-/react-19.0.0.tgz}
"""


def test_build_is_refused_when_no_registry_is_configured() -> None:
    """未設定は「公網で良い」ではない。D4 の既定はここで閉じること。"""

    with pytest.raises(ModuleBuildRefusedError) as error:
        plan_module_build(registry_url=None, lockfile=_INTERNAL_LOCKFILE)

    assert error.value.code == "registry_not_configured"


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_registry_is_treated_as_missing(value: str) -> None:
    """空文字の環境変数を「設定済み」と読まない。既定値の空欄がそのまま渡ってくる。"""

    with pytest.raises(ModuleBuildRefusedError) as error:
        plan_module_build(registry_url=value, lockfile=_INTERNAL_LOCKFILE)

    assert error.value.code == "registry_not_configured"


@pytest.mark.parametrize(
    "url",
    [
        "https://registry.npmjs.org",
        "https://registry.npmjs.org/",
        "http://registry.yarnpkg.com",
        "https://REGISTRY.NPMJS.ORG",
    ],
)
def test_a_public_registry_is_refused_even_when_configured_explicitly(url: str) -> None:
    """明示的に公網を指しても拒む。設定ミスと意図的な退避を区別できないため。"""

    with pytest.raises(ModuleBuildRefusedError) as error:
        plan_module_build(registry_url=url, lockfile=_INTERNAL_LOCKFILE)

    assert error.value.code == "registry_is_public"


@pytest.mark.parametrize("url", ["npm.internal.example", "file:///srv/npm", "ftp://mirror/npm"])
def test_a_non_http_registry_is_refused(url: str) -> None:
    """絶対 http(s) URL でなければ、何処へ取りに行くかが読めない。"""

    with pytest.raises(ModuleBuildRefusedError) as error:
        plan_module_build(registry_url=url, lockfile=_INTERNAL_LOCKFILE)

    assert error.value.code == "registry_url_invalid"


def test_a_lockfile_resolving_to_the_public_registry_is_refused() -> None:
    """**設定だけを見ても足りない。**

    mirror を正しく設定していても、lockfile の `resolved` が公網を名指ししていれば取得先は
    そちらになる。設定だけ検査して通すと「mirror を設定したのに公網から引く」を素通りさせる。
    """

    lockfile = """
lockfileVersion: '9.0'
packages:
  /react@19.0.0:
    resolution: {tarball: https://npm.internal.example/react/-/react-19.0.0.tgz}
  /react-dom@19.0.0:
    resolution: {tarball: https://registry.npmjs.org/react-dom/-/react-dom-19.0.0.tgz}
"""

    with pytest.raises(ModuleBuildRefusedError) as error:
        plan_module_build(registry_url="https://npm.internal.example", lockfile=lockfile)

    assert error.value.code == "lockfile_resolves_to_public_registry"


@pytest.mark.parametrize(
    "source",
    [
        "git+https://github.com/acme/pkg.git#v1",
        "git://github.com/acme/pkg.git",
        "github:acme/pkg",
        "file:../local-pkg",
        "link:../workspace-pkg",
    ],
)
def test_a_lockfile_fetching_outside_the_registry_is_refused(source: str) -> None:
    """registry 以外の取得元は、凍結 lockfile を迂回して任意の code を引き込める。"""

    lockfile = f"""
lockfileVersion: '9.0'
packages:
  /pkg@1.0.0:
    resolution: {{tarball: {source}}}
"""

    with pytest.raises(ModuleBuildRefusedError) as error:
        plan_module_build(registry_url="https://npm.internal.example", lockfile=lockfile)

    assert error.value.code == "lockfile_has_non_registry_source"


def test_an_internal_mirror_with_a_consistent_lockfile_is_allowed() -> None:
    """条件が揃った構成だけが計画を得る。"""

    plan = plan_module_build(
        registry_url="https://npm.internal.example/repository/npm-proxy",
        lockfile=_INTERNAL_LOCKFILE,
    )

    assert plan.registry_url == "https://npm.internal.example/repository/npm-proxy"


def test_refusal_carries_a_stable_code_for_operators() -> None:
    """運用側が「何を用意すれば通るか」を機械的に読めるようにする。"""

    with pytest.raises(ModuleBuildRefusedError) as error:
        plan_module_build(registry_url=None, lockfile="")

    assert error.value.code == "registry_not_configured"
    assert "internal npm registry" in str(error.value)
