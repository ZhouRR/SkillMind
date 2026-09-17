"""MCP の一操作と明示的な回読を、既存の承認契約へ束縛する。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from skillmind.effects.domain import ChangeProposalDraft, ChangeProposalValidationError
from skillmind.integrations.mcp_readback import McpReadBackError, validate_read_back_schema
from skillmind.integrations.mcp_tools import (
    PROFILE,
    configured_tool,
    digest,
    tool_access,
    validate_arguments,
    validate_tool_value,
)

MCP_CALL = "mcp.call/v1"
MCP_PROVIDER_VERSION = PROFILE
EFFECT_ID = "${effect_id}"


def resolve_effect_id(value: Any, effect_id: str) -> Any:
    """完全一致の予約値だけを置換し、部分文字列や式を評価しない。"""
    if isinstance(value, dict):
        return {key: resolve_effect_id(item, effect_id) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_effect_id(item, effect_id) for item in value]
    return effect_id if value == EFFECT_ID else value


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
    """工具名・全引数・回読条件を一緒に承認する。工具名で業務動作を推定しない。"""
    name = target.get("locator")
    if operation != "call" or not isinstance(name, str) or tool_access(config, name) != "call":
        raise ValueError("MCP operation target is invalid")
    tool = configured_tool(config, scope, name)
    if (
        len(changes) != 1
        or set(changes[0]) != {"path", "action", "value"}
        or changes[0]["path"] != "/call"
        or changes[0]["action"] != "SET"
        or verification != {"method": "READ_BACK", "paths": ["/call"]}
        or precondition != {"revision": digest(config["tool_catalog"])}
    ):
        raise ValueError("MCP proposal requires its observed catalog and exact call")
    value = changes[0]["value"]
    if (
        not isinstance(value, dict)
        or not {"arguments", "read_back"} <= set(value)
        or set(value) - {"arguments", "read_back", "cancel_target"}
    ):
        raise ValueError("MCP call requires explicit arguments and read_back")
    args = validate_arguments(name, value["arguments"])
    read_back = value["read_back"]
    if not isinstance(read_back, dict) or set(read_back) != {"name", "arguments", "checks"}:
        raise ValueError("MCP read_back is invalid")
    reader = read_back["name"]
    if not isinstance(reader, str) or tool_access(config, reader) != "read":
        raise ValueError("MCP read_back must use an authorized read tool")
    reader_tool = configured_tool(config, scope, reader)
    read_args = validate_arguments(reader, read_back["arguments"])
    checks = read_back["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= 20:
        raise ValueError("MCP read_back requires bounded checks")
    for check in checks:
        if (
            not isinstance(check, dict)
            or set(check) not in ({"path", "equals"}, {"path", "one_of"})
            or not isinstance(check["path"], str)
            or re.fullmatch(r"(?:/(?:[^~/]|~[01])*)+", check["path"]) is None
            or len(check["path"]) > 512
            or (
                "one_of" in check
                and (not isinstance(check["one_of"], list) or not 1 <= len(check["one_of"]) <= 20)
            )
        ):
            raise ValueError("MCP read_back check is invalid")
    sample_id = "00000000-0000-4000-8000-000000000001"
    validate_tool_value(tool["input_schema"], resolve_effect_id(args, sample_id))
    validate_tool_value(reader_tool["input_schema"], resolve_effect_id(read_args, sample_id))
    validate_read_back_schema(
        reader_tool["output_schema"], resolve_effect_id(checks, sample_id)
    )
    payload = {
        "name": name,
        "arguments": args,
        "read_back": read_back,
        "catalog_hash": precondition["revision"],
    }
    if "cancel_target" in value:
        cancel_target = value["cancel_target"]
        if not isinstance(cancel_target, str) or str(UUID(cancel_target)) != cancel_target:
            raise ValueError("MCP cancellation requires an original Effect UUID")
        payload["cancel_target"] = cancel_target
        if not has_operation_identity(payload):
            raise ValueError("MCP cancellation must read back the original operation identity")
    validate_arguments(name, payload)
    return payload


def check_read_back(read_back: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    """JSON Pointer が実際に存在し、承認済み比較条件を満たすことを確認する。"""
    for index, check in enumerate(read_back["checks"]):
        value: Any = result
        try:
            for part in check["path"][1:].split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list):
                    if re.fullmatch(r"0|[1-9][0-9]*", key) is None:
                        raise ValueError("Invalid array index")
                    value = value[int(key)]
                else:
                    value = value[key]
        except (KeyError, TypeError, ValueError, IndexError):
            raise McpReadBackError("read_back_path_missing", index) from None
        candidates = check["one_of"] if "one_of" in check else [check["equals"]]
        if not any(digest(value) == digest(candidate) for candidate in candidates):
            raise McpReadBackError("read_back_mismatch", index)


def has_operation_identity(payload: Mapping[str, Any]) -> bool:
    """再照会は原 Effect ID の完全一致を確認できる契約だけに限定する。"""

    identity = payload.get("cancel_target", EFFECT_ID)

    def contains(value: Any) -> bool:
        """キーや部分文字列を原 ID の引数として数えない。"""
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        if isinstance(value, list):
            return any(contains(item) for item in value)
        return isinstance(value, str) and value == identity

    return (
        contains(payload["arguments"])
        and contains(payload["read_back"]["arguments"])
        and any(check.get("equals") == identity for check in payload["read_back"]["checks"])
    )


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
    except McpReadBackError as error:
        raise ChangeProposalValidationError(
            f"MCP read_back check {error.check_index + 1} contradicts the reader outputSchema. "
            "Use fields and values from the actual output contract or an authorized observation; "
            "input arguments do not imply output fields. This validation sends no operation."
        ) from None
    except ValueError:
        raise ChangeProposalValidationError(
            "MCP proposal does not match the authorized tool contract"
        ) from None


def mcp_call_scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """承認に含む工具だけを要求し、資源 URI の権限を追加しない。"""
    return {
        "resource_uris": [],
        "tool_names": sorted({payload["name"], payload["read_back"]["name"]}),
    }
