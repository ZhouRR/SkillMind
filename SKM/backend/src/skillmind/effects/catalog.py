"""Apply 可能な effect capability の単一登録表 (計画 §20 R1)。

Proposal の検証、事前許可の可否、EffectExecution の Provider/version 決定は、いずれも
capability ごとに答えが違う。これを呼び出し側の `if capability == "issue.update/v1"` で
書き分けると、capability が増えるたびに 4 箇所を同時に直す必要が生じ、片方だけ緩む余地が残る。
ここを唯一の判断表とし、上位層は表を引くだけにする。

`preauthorizable` は Project policy、`run_auto_approvable` は開始時の Run 限定同意を表す。
repository は Project 事前許可の対象外。Git は新 Run の固定同意により精確な承認記録を
作成できるが、SVN や旧 Run へこの許可を広げない。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from skillmind.documents.library import DOCUMENT_WRITE_CAPABILITY
from skillmind.effects.database_write import (
    DATABASE_WRITE_CAPABILITY,
    DATABASE_WRITE_PROVIDER_VERSION,
    database_write_scope_from_payload,
    validate_database_write_proposal,
)
from skillmind.effects.document_write import (
    DOCUMENT_WRITE_PROVIDER_VERSION,
    document_write_scope_from_payload,
    validate_document_write_proposal,
)
from skillmind.effects.domain import ChangeProposalDraft, ChangeProposalValidationError
from skillmind.effects.issue_update import (
    ISSUE_UPDATE_CAPABILITY,
    ISSUE_UPDATE_PROVIDER_VERSION,
    issue_update_scope_from_payload,
    validate_issue_update_proposal,
)
from skillmind.effects.mcp_call import (
    MCP_CALL,
    MCP_PROVIDER_VERSION,
    mcp_call_scope,
    validate_mcp_proposal,
)
from skillmind.effects.repository_write import (
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
    validate: Callable[[ChangeProposalDraft, Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]
    requested_scope: Callable[[Mapping[str, Any]], dict[str, Any]]
    # 共有 actor/批准/lease の段階検査を実装した Provider だけを贯穿監督する。
    staged_authorization: bool = False
    # 開始者の現在権限を各段階で再検証できる保存 Provider だけを Run 限定同意に含める。
    run_auto_approvable: bool = False
    staged_providers: frozenset[str] = frozenset()
    run_auto_approvable_providers: frozenset[str] = frozenset()

    def supports_run_approval(self, provider: str | None) -> bool:
        """Run 限定同意を段階認可済みの Provider にだけ許可する。"""
        return self.run_auto_approvable or provider in self.run_auto_approvable_providers

    def supports_supervision(self, provider: str | None) -> bool:
        """同じ capability の未対応 Provider まで段階認可を広げない。"""
        return self.staged_authorization or provider in self.staged_providers

    @property
    def providers(self) -> frozenset[str]:
        """この capability を実装している Provider 名の集合。"""

        return frozenset(self.provider_versions)


EFFECT_CAPABILITIES: Mapping[str, EffectCapabilityDefinition] = {
    MCP_CALL: EffectCapabilityDefinition(
        capability_version=MCP_CALL,
        provider_versions={"mcp": MCP_PROVIDER_VERSION},
        preauthorizable=False,
        validate=validate_mcp_proposal,
        requested_scope=mcp_call_scope,
        staged_authorization=True,
        run_auto_approvable_providers=frozenset({"mcp"}),
    ),
    DOCUMENT_WRITE_CAPABILITY: EffectCapabilityDefinition(
        capability_version=DOCUMENT_WRITE_CAPABILITY,
        provider_versions={"project-library": DOCUMENT_WRITE_PROVIDER_VERSION},
        preauthorizable=False,
        validate=lambda draft, scope, config: validate_document_write_proposal(
            draft, binding_scope=scope
        ),
        requested_scope=document_write_scope_from_payload,
        staged_authorization=True,
        run_auto_approvable=True,
    ),
    DATABASE_WRITE_CAPABILITY: EffectCapabilityDefinition(
        capability_version=DATABASE_WRITE_CAPABILITY,
        provider_versions={"postgres": DATABASE_WRITE_PROVIDER_VERSION},
        preauthorizable=False,
        validate=lambda draft, scope, config: validate_database_write_proposal(
            draft, binding_scope=scope
        ),
        requested_scope=database_write_scope_from_payload,
        staged_authorization=True,
        run_auto_approvable=True,
    ),
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
        # Project policy で常時許可しない。Git の Run 限定同意は別途検証する。
        preauthorizable=False,
        validate=lambda draft, scope, config: validate_repository_write_proposal(
            draft, binding_scope=scope, integration_config=config
        ),
        requested_scope=repository_write_scope_from_payload,
        staged_providers=frozenset({"git"}),
        run_auto_approvable_providers=frozenset({"git"}),
    ),
}


def resolve_effect_capability(capability_version: str) -> EffectCapabilityDefinition:
    """Apply chain へ入れてよい capability だけを解決する。未登録は提案段階で閉じる。"""

    definition = EFFECT_CAPABILITIES.get(capability_version)
    if definition is None:
        raise ChangeProposalValidationError("Effect Provider is not enabled for apply")
    return definition
