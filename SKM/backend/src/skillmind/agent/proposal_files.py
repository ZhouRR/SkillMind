"""承認前に file の正確な要求を読み、既存提案 validator へ渡す。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from skillmind.agent.domain import RunContext
from skillmind.agent.materialization_storage import MaterializationError, read_file
from skillmind.agent.tool_policy import ToolExecutionPolicy, ToolPolicyViolation
from skillmind.core.hashing import sha256_hex
from skillmind.effects.proposal import CHANGE_PROPOSE_SDK_NAME


async def expand_proposal_file(context: RunContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """file の hash と権限を確認する。変更済み草稿や別 Run の絶対 path は採用しない。"""
    if "request_file" not in arguments:
        return dict(arguments)
    if set(arguments) - {"request_file", "expected_hash", "evidence_refs"}:
        raise ValueError("Proposal file request contains unknown fields")
    if "workspace.read/v1" not in context.permission_snapshot.get("allowed_capabilities", []):
        raise ValueError("Proposal file requires workspace access")
    path = arguments["request_file"]
    if not isinstance(path, str) or not path.startswith(("workspace/", "output/")):
        raise ValueError("Proposal file must be inside the Run workspace")
    try:
        raw = await asyncio.to_thread(read_file, context.workspace.root, path, max_bytes=4_300_000)
        if arguments.get("expected_hash") != "sha256:" + sha256_hex(raw):
            raise ValueError("Proposal file changed")
        result = json.loads(raw)
        if not isinstance(result, dict) or "request_file" in result:
            raise ValueError("Proposal file is invalid")
    except MaterializationError:
        raise ValueError("Proposal file could not be read") from None
    if "evidence_refs" in arguments:
        result["evidence_refs"] = arguments["evidence_refs"]
    # SDK の envelope 検査だけで承認へ進めない。preflight・即時実行・延期保存が
    # それぞれ読み直した原 byte に、凍結 Tool と同じ Schema/境界検査を適用する。
    try:
        ToolExecutionPolicy(
            context.tools,
            allowed_capabilities=frozenset(context.permission_snapshot["allowed_capabilities"]),
        ).authorize(CHANGE_PROPOSE_SDK_NAME, result)
    except ToolPolicyViolation as error:
        raise ValueError(str(error)) from None
    return result
