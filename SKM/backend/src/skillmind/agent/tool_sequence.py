"""確定した只読/ローカル工具を有界に直列実行する。外部変更や動的式を扱わない。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from skillmind.agent.evidence import EvidenceDraft
from skillmind.agent.tool_gateway import ProviderToolResult, RunToolContext, ToolProviderError
from skillmind.agent.tool_policy import capability_to_sdk_name
from skillmind.core.hashing import canonical_json, sha256_hex

if TYPE_CHECKING:
    from skillmind.agent.tool_gateway import ToolGateway

SEQUENCE_CAPABILITY = "tool.sequence/v1"
MAX_SEQUENCE_STEPS = 5


class ToolStepBudget:
    """外側と子呼出しで共用する一 Attempt の既存工具上限。予約は権限ではない。"""

    def __init__(self, limit: int) -> None:
        """上限を固定し、別 Run/Attempt に持ち越さない。"""
        self.limit = limit
        self.used = 0

    def consume(self) -> None:
        """await 前に一枠を消費し、再送や失敗でも自動返却しない。"""
        if self.used >= self.limit:
            raise PermissionError("Run tool step limit exceeded")
        self.used += 1


def matches_checks(value: Any, checks: list[dict[str, Any]]) -> bool:
    """JSON Pointer と JSON 型を含む完全一致だけを比較し、式評価を許さない。"""
    for check in checks:
        target = value
        pointer = check["pointer"]
        if not isinstance(pointer, str) or re.fullmatch(r"(?:/(?:[^~/]|~[01])*)*", pointer) is None:
            return False
        try:
            for raw in pointer.split("/")[1:] if pointer else ():
                token = raw.replace("~1", "/").replace("~0", "~")
                if isinstance(target, list):
                    if not token.isdecimal() or (token != "0" and token.startswith("0")):
                        return False
                    target = target[int(token)]
                elif isinstance(target, dict):
                    target = target[token]
                else:
                    return False
        except (KeyError, IndexError, ValueError):
            return False
        if canonical_json(target) != canonical_json(check["equals"]):
            return False
    return True


class ToolSequenceProvider:
    """元の Gateway へ子呼出しを戻す Run-scoped orchestration。Provider を直接呼ばない。"""

    def __init__(self, gateway: ToolGateway | None = None) -> None:
        """共有 catalog の instance には Run 状態を保存しない。"""
        self._gateway = gateway

    def bind(self, gateway: ToolGateway) -> ToolSequenceProvider:
        """一 Runtime 専用の実行 port を返し、他 Run と共有しない。"""
        return ToolSequenceProvider(gateway)

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """全静的引数を先に検証し、一件の失敗/条件不成立で後続を未実行にする。"""
        gateway = self._gateway
        if gateway is None or context.tool_call_id is None:
            raise ToolProviderError(
                "unavailable", "Sequence runtime is unavailable", retryable=False
            )
        steps = deepcopy(arguments.get("steps"))
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_SEQUENCE_STEPS:
            raise ToolProviderError("invalid_request", "Invalid sequence size", retryable=False)
        # どの step も前段の未知値に依存させず、全登録/権限/Schema を副作用前に検査する。
        try:
            for step in steps:
                gateway.validate_sequence_step(
                    capability_to_sdk_name(step["capability"]), step["arguments"]
                )
        except (KeyError, TypeError, ValueError, LookupError, PermissionError):
            raise ToolProviderError(
                "invalid_request",
                "Sequence contains an unavailable or non-sequence tool",
                retryable=False,
            ) from None
        completed: list[dict[str, Any]] = []
        outcome, reason = "COMPLETED", None
        for position, step in enumerate(steps):
            response = await gateway.invoke_sequence_step(context, step, position=position)
            item = {"position": position, "capability": step["capability"], "response": response}
            completed.append(item)
            # 子結果は個別に監査済み。合計の上限到達は明示し、後続を実行しない。
            limit = min(
                1_048_576, context.run.limits.max_output_bytes if context.run else 1_048_576
            )
            if len(canonical_json(completed).encode("utf-8")) > max(0, limit - 4096):
                item["response"] = {
                    "status": "stored",
                    "omitted": "aggregate_byte_limit",
                    "evidence_refs": response.get("evidence_refs", []),
                    "artifact_refs": response.get("artifact_refs", []),
                }
                outcome, reason = "STOPPED", "aggregate_byte_limit"
                break
            if response.get("status") == "error":
                outcome, reason = "STOPPED", "tool_failed"
                break
            if not matches_checks(response, step.get("checks", [])):
                outcome, reason = "STOPPED", "condition_failed"
                break
        response = {
            "status": "success",
            "provider": "platform",
            "outcome": outcome,
            "steps": completed,
            "not_run": list(range(len(completed), len(steps))),
            "stop_reason": reason,
        }
        return ProviderToolResult(
            response=response,
            evidence=(
                EvidenceDraft(
                    evidence_type="tool-sequence",
                    source_uri=f"tool-sequence://runs/{context.run_id}/{context.tool_call_id}",
                    source_locator={"tool_call_id": str(context.tool_call_id)},
                    content_hash="sha256:" + sha256_hex(canonical_json(response)),
                    metadata={
                        "snapshot": response,
                        "meaning": "Tool results, not a business verdict",
                    },
                ),
            ),
        )
