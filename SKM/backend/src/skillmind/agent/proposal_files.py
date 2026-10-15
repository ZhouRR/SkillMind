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


class ProposalFileError(ValueError):
    """原提案の検査で公開可能な固定診断。Service の一般 ValueError と区別する。"""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        """原文検査の分類を保持し、入力検証失敗として上位へ伝える。"""
        self.code = code
        super().__init__(message)


async def expand_proposal_file(context: RunContext, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """file の hash と権限を確認する。変更済み草稿や別 Run の絶対 path は採用しない。"""
    if "request_file" not in arguments:
        return dict(arguments)
    if set(arguments) - {"request_file", "expected_hash", "evidence_refs"}:
        raise ProposalFileError("Proposal file request contains unknown fields")
    if "workspace.read/v1" not in context.permission_snapshot.get("allowed_capabilities", []):
        raise ProposalFileError("Proposal file requires workspace access", code="scope_denied")
    path = arguments["request_file"]
    if not isinstance(path, str) or not path.startswith(("workspace/", "output/")):
        raise ProposalFileError("Proposal file must be inside the Run workspace")
    try:
        raw = await asyncio.to_thread(read_file, context.workspace.root, path, max_bytes=4_300_000)
        if arguments.get("expected_hash") != "sha256:" + sha256_hex(raw):
            raise ProposalFileError("Proposal file changed; use the current file hash")
        result = json.loads(raw)
        if not isinstance(result, dict) or "request_file" in result:
            raise ProposalFileError("Proposal file must contain one inline JSON request object")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ProposalFileError("Proposal file must contain valid UTF-8 JSON") from None
    except MaterializationError:
        raise ProposalFileError(
            "Proposal file could not be read inside this Run's workspace"
        ) from None
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
        raise ProposalFileError(str(error), code=error.code) from None
    return result
