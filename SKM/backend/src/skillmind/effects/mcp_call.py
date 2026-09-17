"""FlaUI の一操作を既存の proposal/approval/read-back 契約へ束縛する。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from skillmind.effects.domain import ChangeProposalDraft, ChangeProposalValidationError
from skillmind.integrations.mcp_tools import (
    WRITE_TOOLS,
    configured_tool,
    digest,
    validate_arguments,
    validate_tool_value,
)

MCP_CALL = "mcp.call/v1"
MCP_PROVIDER_VERSION = "flaui-step/v1"


def call_payload(
    *,
    operation: str,
    target: Mapping[str, Any],
    changes: tuple[dict[str, Any], ...],
    precondition: Mapping[str, Any],
    verification: Mapping[str, Any],
    scope: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """承認の対象 tool・全引数・清單 revision を一致させる。requestId は platform が採番する。"""
    if operation not in WRITE_TOOLS or target.get("locator") != operation:
        raise ValueError("MCP operation target is invalid")
    tool = configured_tool(config, scope, operation)
    # Task の範囲絞り込みで回読権が落ちていれば、提案段階から拒否する。
    for reader in ("inspect_window", "get_step_status"):
        configured_tool(config, scope, reader)
    if (
        len(changes) != 1
        or set(changes[0]) != {"path", "action", "value"}
        or changes[0]["path"] != "/call"
        or changes[0]["action"] != "SET"
        or verification != {"method": "READ_BACK", "paths": ["/call"]}
        or precondition != {"revision": digest(config["tool_catalog"])}
    ):
        raise ValueError("MCP proposal must use the observed tool contract and one exact call")
    params = validate_arguments(operation, changes[0]["value"], proposal=True)
    schema_args = dict(params)
    if operation == "execute_step":
        schema_args["requestId"] = "00000000-0000-4000-8000-000000000001"
    validate_tool_value(tool["input_schema"], schema_args)
    return {"name": operation, "arguments": params, "catalog_hash": precondition["revision"]}


def validate_mcp_proposal(
    draft: ChangeProposalDraft, scope: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    """保存前と claim 前に同じ関数で model 提案を検証する。"""
    try:
        return call_payload(
            operation=draft.operation,
            target=draft.target,
            changes=draft.changes,
            precondition=draft.precondition,
            verification=draft.verification,
            scope=scope,
            config=config,
        )
    except ValueError:
        raise ChangeProposalValidationError(
            "MCP proposal does not match the authorized tool contract"
        ) from None


def mcp_call_scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """承認範囲は単一 tool とし、resource URI の読取権を追加しない。"""
    return {"resource_uris": [], "tool_names": [payload["name"]]}
