"""生成 module 版の lifecycle と公開条件を検証する (計画 §24 M1/D2/D6)。

版は凍結する。ここで守るのは「配信できない版・静的検査に落ちた版を PUBLISHED と呼べない」ことと、
「一度止めた版は復活しない」こと——どちらも緩めると、何が配信中なのかが読めなくなる。
"""

from __future__ import annotations

import pytest

from skillmind.modules import (
    MODULE_CONTENT_SECURITY_POLICY,
    ModuleVersionError,
    ModuleVersionStatus,
    plan_module_transition,
    require_publishable,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ModuleVersionStatus.DRAFT, ModuleVersionStatus.BUILT),
        (ModuleVersionStatus.DRAFT, ModuleVersionStatus.DISABLED),
        (ModuleVersionStatus.BUILT, ModuleVersionStatus.PUBLISHED),
        (ModuleVersionStatus.BUILT, ModuleVersionStatus.DISABLED),
        (ModuleVersionStatus.PUBLISHED, ModuleVersionStatus.DISABLED),
    ],
)
def test_allowed_transitions(
    current: ModuleVersionStatus, target: ModuleVersionStatus
) -> None:
    """正当な遷移を許す。DISABLED へはどの段階からでも落とせる (運行時降級 D6)。"""

    assert plan_module_transition(current=current, target=target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ModuleVersionStatus.DRAFT, ModuleVersionStatus.PUBLISHED),
        (ModuleVersionStatus.DISABLED, ModuleVersionStatus.PUBLISHED),
        (ModuleVersionStatus.DISABLED, ModuleVersionStatus.BUILT),
        (ModuleVersionStatus.PUBLISHED, ModuleVersionStatus.BUILT),
    ],
)
def test_rejected_transitions(
    current: ModuleVersionStatus, target: ModuleVersionStatus
) -> None:
    """build を飛ばした公開と、停止した版の復活を拒否する。

    直したものは新しい版として作り直す——源が変われば hash も変わり、別行になる。
    """

    with pytest.raises(ModuleVersionError):
        plan_module_transition(current=current, target=target)


def test_publishing_requires_a_built_version_with_a_bundle() -> None:
    """配信できない版を「公開済み」と呼ばない。"""

    with pytest.raises(ModuleVersionError):
        require_publishable(
            status=ModuleVersionStatus.BUILT, bundle_hash=None, static_rejected=False
        )
    with pytest.raises(ModuleVersionError):
        require_publishable(
            status=ModuleVersionStatus.DRAFT, bundle_hash="sha256:x", static_rejected=False
        )


def test_a_statically_rejected_version_can_never_be_published() -> None:
    """静的検査に落ちた版は公開経路へ入れない (計画 §24 D5)。"""

    with pytest.raises(ModuleVersionError):
        require_publishable(
            status=ModuleVersionStatus.BUILT, bundle_hash="sha256:x", static_rejected=True
        )


def test_publishable_version_passes() -> None:
    """三条件が揃った版は公開できる。"""

    require_publishable(
        status=ModuleVersionStatus.BUILT, bundle_hash="sha256:x", static_rejected=False
    )


def test_frozen_csp_forces_an_opaque_origin_and_blocks_network() -> None:
    """配信 CSP が D2 の前提を満たすことを固定する。

    D2 は独立 Origin の代わりにこの header で opaque origin を強制する設計であり、`sandbox` が
    落ちれば直接導航時に応用 Origin 上で会話 Cookie ごと走る。`connect-src 'none'` は module が
    自前で網絡へ出ないこと(すべて Host API 経由)の保証。どちらも版と一緒に凍結する。
    """

    assert MODULE_CONTENT_SECURITY_POLICY.startswith("sandbox allow-scripts;")
    assert "allow-same-origin" not in MODULE_CONTENT_SECURITY_POLICY
    assert "connect-src 'none'" in MODULE_CONTENT_SECURITY_POLICY
    assert "default-src 'none'" in MODULE_CONTENT_SECURITY_POLICY
