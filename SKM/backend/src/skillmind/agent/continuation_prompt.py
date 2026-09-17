"""同じ原生会話の続行だけに差分 prompt を作り、監査 Brief は全量のまま保持する。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from skillmind.agent.domain import RunContext
from skillmind.agent.task_brief import render_task_brief_prompt
from skillmind.core.hashing import canonical_json, sha256_hex


def continuation_prompt(
    context: RunContext,
    prompt: str,
    previous: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """静的指示の同一性が記録で確認できる場合だけ、既送の指示・事実を再送しない。"""

    brief = context.task_brief
    if not brief or not context.task_brief_checksum or prompt != context.prompt:
        return prompt, {}
    if context.task_brief_checksum != f"sha256:{sha256_hex(canonical_json(brief))}":
        raise ValueError("Continuation Brief checksum is invalid")
    static = deepcopy(dict(brief))
    static["identity"]["segment_no"] = 1
    if brief.get("brief_version") == "skillmind.agent-task-brief/v1":
        static["objective"]["segment_objective"] = static["objective"]["run_objective"]
    static["checkpoint"] = {}
    static_prompt = render_task_brief_prompt(
        static,
        input_json=context.input_json,
        output_schema=context.result_schema,
    )
    static_checksum = sha256_hex(
        canonical_json(
            {
                "protocol": "skillmind.codex-continuation/v1",
                "prompt": static_prompt,
                "permission": dict(context.permission_snapshot),
                "tools": [
                    {"name": tool.sdk_name, "schema": tool.input_schema} for tool in context.tools
                ],
            }
        )
    )
    checkpoint = brief["checkpoint"]
    hashes = {
        key: [_hash(item) for item in value] if isinstance(value, list) else _hash(value)
        for key, value in checkpoint.items()
    }
    metadata = {"static_prompt_checksum": static_checksum, "checkpoint_hashes": hashes}
    if previous is None or previous.get("static_prompt_checksum") != static_checksum:
        return prompt, metadata
    old_hashes = previous.get("checkpoint_hashes")
    if not isinstance(old_hashes, Mapping):
        return prompt, metadata
    delta: dict[str, Any] = {}
    replaced_lists: list[str] = []
    for key in old_hashes.keys() - hashes.keys():
        delta[key] = None
    for key, value in checkpoint.items():
        if hashes[key] == old_hashes.get(key):
            continue
        if isinstance(value, list) and isinstance(old_hashes.get(key), list):
            prior = old_hashes[key]
            if hashes[key][: len(prior)] != prior:
                # 並べ替え・削除・途中挿入は置換。完全な prefix 一致だけを追加とする。
                delta[key] = value
                replaced_lists.append(key)
            else:
                delta[key] = value[len(prior) :]
        else:
            delta[key] = value
    return (
        "Continue the same Run and native conversation under its unchanged frozen Skill, "
        "input, resource bindings, permissions and output contract. Do not restart completed "
        "work. Only the following audited continuation data is new; omitted facts and "
        "references remain in the conversation. List entries below are additions except "
        "for explicitly named replacement lists; null clears a removed checkpoint field. "
        "A receipt is evidence of that original effect, not current remote state or new "
        "write permission. Preserve its exact IDs, storage coordinates and before/after "
        "references; never reconstruct them from guesses.\n"
        + "Run identity (JSON): "
        + canonical_json(brief["identity"])
        + "\nCurrent task: "
        + (
            brief["task"]["title"]
            if brief.get("brief_version") == "skillmind.agent-task-brief/v2"
            else brief["objective"]["segment_objective"]
        )
        + "\nAudited continuation changes (JSON): "
        + canonical_json(delta)
        + "\nReplacement checkpoint lists (JSON): "
        + canonical_json(replaced_lists)
        + "\nFor the next RESUME checkpoint, supply a short summary and only new business "
        "facts/references; the platform retains previous facts and references. Complete "
        "the original deliverables and report when all required effects are verified.",
        metadata,
    )


def _hash(value: Any) -> str:
    """既送値の比較には正本と同じ canonical JSON hash を使い、本文を再保存しない。"""

    return sha256_hex(canonical_json(value))
