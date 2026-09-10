"""Approved effect を実行する Provider registry と port を定義する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from skillmind.effects.domain import ClaimedEffectExecution, EffectProviderResult


class EffectProvider(Protocol):
    """Agent Tool gateway から隔離された approved-effect Provider port。"""

    async def apply(
        self, execution: ClaimedEffectExecution, *, credential: str | None
    ) -> EffectProviderResult:
        """Approved snapshot を idempotent に適用し、before/after read-back を返す。"""

        ...


@dataclass(frozen=True, slots=True)
class EffectProviderDefinition:
    """Versioned capability、Provider、implementation の固定 registration。"""

    capability_version: str
    provider: str
    provider_version: str
    implementation: EffectProvider
    requires_secret: bool


class EffectProviderRegistry:
    """Worker 起動時に固定した effect Provider だけを解決する registry。"""

    def __init__(self, definitions: tuple[EffectProviderDefinition, ...]) -> None:
        """Capability/provider pair の重複を拒否して registry を固定する。"""

        indexed: dict[tuple[str, str], EffectProviderDefinition] = {}
        for definition in definitions:
            key = (definition.capability_version, definition.provider)
            if key in indexed:
                raise ValueError("Effect Provider definition is duplicated")
            indexed[key] = definition
        self._definitions = indexed

    @property
    def write_capabilities(self) -> frozenset[str]:
        """Readiness/publish wiring と共有する登録済み write capability set を返す。"""

        return frozenset(key[0] for key in self._definitions)

    def resolve(self, *, capability_version: str, provider: str) -> EffectProviderDefinition:
        """未登録 pair を Provider 呼び出し前に拒否する。"""

        definition = self._definitions.get((capability_version, provider))
        if definition is None:
            raise LookupError("Effect Provider is not registered")
        return definition

    def snapshot(self) -> Mapping[tuple[str, str], EffectProviderDefinition]:
        """Test/diagnostic 用に immutable 扱いの registration copy を返す。"""

        return dict(self._definitions)
