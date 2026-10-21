"""SDK を読み込まずに Run の工具認可・監査 callback を共有する。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from skillmind.effects.inline import InlineEffectResult

ToolAuthorizationCallback = Callable[[str, Mapping[str, Any], str, str], Awaitable[None]]
ToolDenialCallback = Callable[[str, Mapping[str, Any], str, str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RunMcpRuntime:
    """エンジン専用 server factory と、SDK 非依存の原 Run callback。"""

    server_factory: Callable[[], Any]
    on_tool_authorized: ToolAuthorizationCallback | None = None
    on_tool_denied: ToolDenialCallback | None = None
    deferred_tool_names: frozenset[str] = frozenset()
    on_tool_attempt: Callable[[], None] | None = None
    on_inline_effect: (
        Callable[[Mapping[str, Any], str, str], Awaitable[InlineEffectResult | None]] | None
    ) = None
    on_deferred_validation: (
        Callable[[str, Mapping[str, Any], str, str], Awaitable[dict[str, Any] | None]] | None
    ) = None


