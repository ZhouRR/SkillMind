"""Apply 可能な effect capability の単一登録表 (計画 §20 R1)。

Proposal の検証、事前許可の可否、EffectExecution の Provider/version 決定は、いずれも
capability ごとに答えが違う。これを呼び出し側の `if capability == "issue.update/v1"` で
書き分けると、capability が増えるたびに 4 箇所を同時に直す必要が生じ、片方だけ緩む余地が残る。
ここを唯一の判断表とし、上位層は表を引くだけにする。

`preauthorizable=False` は「人手承認を迂回できない」ことを表す。repository への書き込みは
後戻り費用が高く、PR/branch という可評審の形で残しても**承認そのものは省けない** (§20.2)。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from projectmind.effects.domain import ChangeProposalDraft, ChangeProposalValidationError
from projectmind.effects.issue_update import (
    ISSUE_UPDATE_CAPABILITY,
    ISSUE_UPDATE_PROVIDER_VERSION,
    issue_update_scope_from_payload,
    validate_issue_update_proposal,
)
from projectmind.effects.repository_write import (
    REPOSITORY_WRITE_CAPABILITY,
    REPOSITORY_WRITE_PROVIDER_VERSION,
    REPOSITORY_WRITE_SVN_PROVIDER_VERSION,
    repository_write_scope_from_payload,
    validate_repository_write_proposal,
)


@dataclass(frozen=True, slots=True)
class EffectCapabilityDefinition:
    """一つの apply 可能 capability の検証規則と Provider 束縛。"""

    capability_version: str
    # Provider 名 → provider_version。同一 capability を複数 Provider が実装することがあり
    # (git / svn)、実装が違えば version も違う。EffectExecution には選ばれた Provider の
    # version を記録する。
    provider_versions: Mapping[str, str]
    preauthorizable: bool
    # 検証は「凍結 binding scope」と「Integration の非機密 config」の両方を見る。config 側にしか
    # 無い制約 (書き込み先 branch の追加制限など) を apply まで持ち越すと、承認画面には落とせない
    # 提案が並んでしまう。
    validate: Callable[
        [ChangeProposalDraft, Mapping[str, Any], Mapping[str, Any]], dict[str, Any]
    ]
    requested_scope: Callable[[Mapping[str, Any]], dict[str, Any]]

    @property
    def providers(self) -> frozenset[str]:
        """この capability を実装している Provider 名の集合。"""

        return frozenset(self.provider_versions)


EFFECT_CAPABILITIES: Mapping[str, EffectCapabilityDefinition] = {
    ISSUE_UPDATE_CAPABILITY: EffectCapabilityDefinition(
        capability_version=ISSUE_UPDATE_CAPABILITY,
        provider_versions={"redmine": ISSUE_UPDATE_PROVIDER_VERSION},
        # 低 risk・精確 scope の field 更新に限り、ADMIN の明示 policy で事前許可できる。
        preauthorizable=True,
        validate=lambda draft, scope, config: validate_issue_update_proposal(
            draft, binding_scope=scope
        ),
        requested_scope=issue_update_scope_from_payload,
    ),
    REPOSITORY_WRITE_CAPABILITY: EffectCapabilityDefinition(
        capability_version=REPOSITORY_WRITE_CAPABILITY,
        provider_versions={
            "git": REPOSITORY_WRITE_PROVIDER_VERSION,
            "svn": REPOSITORY_WRITE_SVN_PROVIDER_VERSION,
        },
        # 代码変更は常に人手承認を要する (§20.2)。事前許可の対象に**しない**。
        preauthorizable=False,
        validate=lambda draft, scope, config: validate_repository_write_proposal(
            draft, binding_scope=scope, integration_config=config
        ),
        requested_scope=repository_write_scope_from_payload,
    ),
}


def resolve_effect_capability(capability_version: str) -> EffectCapabilityDefinition:
    """Apply chain へ入れてよい capability だけを解決する。未登録は提案段階で閉じる。"""

    definition = EFFECT_CAPABILITIES.get(capability_version)
    if definition is None:
        raise ChangeProposalValidationError("Effect Provider is not enabled for apply")
    return definition
