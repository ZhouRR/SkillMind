"""RV 用 DB/文書庫の書込みと後置拡張を別々に制限する配備上限。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from skillmind.documents.library import DOCUMENT_WRITE_CAPABILITY
from skillmind.effects.catalog import EFFECT_CAPABILITIES
from skillmind.effects.database_write import DATABASE_WRITE_CAPABILITY
from skillmind.skills.resource_binding import is_deferred_execution_capability, is_write_capability

if TYPE_CHECKING:
    from skillmind.core.settings import Settings


def configured_execution_features(settings: Settings) -> ExecutionFeatures:
    """API/Worker/Interpreter の配備上限を一箇所で組む。文書 write は実環境検証まで閉じる。"""

    return ExecutionFeatures(
        deferred=settings.deferred_features_enabled,
        database_writes=settings.database_writes_enabled,
    )


class ExecutionFeatureDisabledError(ValueError):
    """保存済み要求でも現在の配備上限を超える実行は開始できない。"""


@dataclass(frozen=True, slots=True)
class ExecutionFeatures:
    """DB を有効化しても調度/子/他 Provider を暗黙に有効化しない固定方針。"""

    deferred: bool = False
    database_writes: bool = False
    # Provider 装配の完成までは Settings/API に公開しない内部の独立上限。
    document_writes: bool = False

    @property
    def effects_enabled(self) -> bool:
        """Effect dispatcher の起動可否。個々の対象は別途精確に検査する。"""
        return self.deferred or self.database_writes or self.document_writes

    @property
    def write_capabilities(self) -> frozenset[str]:
        """登録済みかつ配備で許可された外部 write 能力だけを返す。"""
        return frozenset(
            capability for capability in EFFECT_CAPABILITIES if self.capability_enabled(capability)
        )

    def capability_enabled(self, capability: str) -> bool:
        """Interpreter、Run permission、実 Tool 準備で同じ上限を使う。"""
        if capability == DATABASE_WRITE_CAPABILITY:
            return self.database_writes
        if capability == DOCUMENT_WRITE_CAPABILITY:
            return self.document_writes
        if capability == "change.propose/v1":
            return self.effects_enabled
        return self.deferred or not is_deferred_execution_capability(capability)

    def effect_enabled(self, capability: str, operation: str) -> bool:
        """DB は INSERT/UPDATE、文書庫は CREATE に限り、他 switch からの迂回を拒否する。"""
        return capability in self.write_capabilities and (
            capability != DATABASE_WRITE_CAPABILITY or operation in {"INSERT", "UPDATE"}
        ) and (
            capability != DOCUMENT_WRITE_CAPABILITY or operation == "CREATE"
        )

    def require_effect(self, capability: str, operation: str) -> None:
        """外部 I/O や approval/outbox 更新の前に精確対象を拒否する。"""
        if not self.effect_enabled(capability, operation):
            raise ExecutionFeatureDisabledError("This effect is not enabled in this deployment")

    def blueprint_enabled(self, blueprint: Mapping[str, Any]) -> bool:
        """apply intent の参照先と操作を起動前に調べ、DB 以外を一括開放しない。"""
        resources = {
            item.get("key"): item
            for item in blueprint.get("resource_requirements", [])
            if isinstance(item, Mapping)
        }
        for effect in blueprint.get("effect_intents", []):
            if not isinstance(effect, Mapping) or effect.get("mode") != "apply":
                continue
            resource = resources.get(effect.get("resource_key"), {})
            writes = [
                cap
                for cap in resource.get("capabilities", [])
                if isinstance(cap, str) and is_write_capability(cap)
            ]
            if not writes or not all(
                self.effect_enabled(cap, str(effect.get("operation", ""))) for cap in writes
            ):
                return False
        return True
