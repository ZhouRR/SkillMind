"""生成 FrontendModule の版 lifecycle と降級規則 (計画 §24 M1/D6 / `docs/07` §13)。

版は**凍結**する。source/lockfile/bundle のどれか一つでも変われば別の版で、既存行は書き換えない
——「同じ版なのに中身が違う」を作らないため。回退は ProjectComposition が指す SkillVersion を
戻すことで起き、版そのものは動かさない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

# 現行 Module Host API の版。bundle と host の協定が変わったら上げる。
MODULE_API_VERSION = "skillmind.module-api/v1"

# 配信時に必ず付ける CSP。D2 はこの header で opaque origin を強制する設計であり、落ちた瞬間に
# 隔離が消える。値を版と一緒に凍結し、配信側が勝手に緩められないようにする。
MODULE_CONTENT_SECURITY_POLICY = (
    "sandbox allow-scripts; default-src 'none'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'none'; "
    "frame-ancestors 'self'"
)


class ModuleVersionStatus(StrEnum):
    """生成 module 版の lifecycle。"""

    DRAFT = "DRAFT"
    BUILT = "BUILT"
    PUBLISHED = "PUBLISHED"
    DISABLED = "DISABLED"


# 静的検査に落ちた版は BUILT へ進めない。DISABLED は運行時の降級 (D6) と手動停止の両方が入る。
ALLOWED_MODULE_TRANSITIONS: dict[ModuleVersionStatus, frozenset[ModuleVersionStatus]] = {
    ModuleVersionStatus.DRAFT: frozenset(
        {ModuleVersionStatus.BUILT, ModuleVersionStatus.DISABLED}
    ),
    ModuleVersionStatus.BUILT: frozenset(
        {ModuleVersionStatus.PUBLISHED, ModuleVersionStatus.DISABLED}
    ),
    ModuleVersionStatus.PUBLISHED: frozenset({ModuleVersionStatus.DISABLED}),
    # 一度止めた版は復活させない。原因が消えたかを platform 側で判定できないため、
    # 直したものは新しい版として作り直す (源が変われば hash も変わり、別行になる)。
    ModuleVersionStatus.DISABLED: frozenset(),
}


class ModuleVersionError(ValueError):
    """版の状態遷移または凍結条件に反することを表す。"""


@dataclass(frozen=True, slots=True)
class ModuleVersionRecord:
    """生成 module 版の公開投影。bundle 本体は object storage が持つ。"""

    module_version_id: UUID
    skill_version_id: UUID
    module_api_version: str
    status: ModuleVersionStatus
    source_hash: str
    lockfile_hash: str | None
    bundle_hash: str | None
    content_security_policy: str
    static_report: dict[str, Any]
    build_report: dict[str, Any]
    created_by: UUID
    disabled_at: datetime | None
    disabled_reason: str | None
    created_at: datetime
    updated_at: datetime


def plan_module_transition(
    *, current: ModuleVersionStatus, target: ModuleVersionStatus
) -> ModuleVersionStatus:
    """状態遷移の唯一の検証点。許されない遷移は例外にする。"""

    if target not in ALLOWED_MODULE_TRANSITIONS[current]:
        raise ModuleVersionError(
            f"Module version transition is not allowed: {current} -> {target}"
        )
    return target


def require_publishable(
    *, status: ModuleVersionStatus, bundle_hash: str | None, static_rejected: bool
) -> None:
    """公開してよい版かを判定する。

    三つとも「無いなら公開しない」側へ倒す。配信できない版や静的検査に落ちた版を PUBLISHED と
    呼べてしまうと、`docs/07` §13 の「回退しても Result/Evidence は無傷」という前提より前に、
    そもそも何が配信中なのかが読めなくなる。
    """

    if status is not ModuleVersionStatus.BUILT:
        raise ModuleVersionError("Only a built module version can be published")
    if not bundle_hash:
        raise ModuleVersionError("Module version has no bundle to serve")
    if static_rejected:
        raise ModuleVersionError("Module version did not pass static rejection checks")
