"""原宣言の調整基準と、明示 scope／候補修復の非対象 field 保護を提供する。"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from typing import Any

from skillmind.core.hashing import canonical_json, sha256_hex

_LAUNCH_FIELDS = frozenset({
    "title", "description", "input_contract", "input_source_trace",
    "resource_requirements", "resource_source_traces", "tools", "diagnostics",
})
_CANDIDATE_FIELDS = frozenset({
    "candidate_version", "title", "description", "input_contract", "input_source_ref",
    "resource_requirements", "platform_tools", "diagnostics",
})


class CandidateRevisionError(ValueError):
    """候補本文を含まない、共有検証で処理可能な固定分類。"""

    def __init__(self, code: str, path: str, message: str) -> None:
        """機密値を複製せず、platform field 位置だけを保持する。"""

        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


def snapshot_launch_contract(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """既存原文実行版の準備宣言を全量投影する。旧 Blueprint は逆変換しない。"""

    if "skill_execution" not in manifest:
        return None
    definition = manifest["skill_execution"]
    tasks = manifest.get("tasks")
    if (
        not isinstance(definition, Mapping)
        or definition.get("execution_version") != "skillmind.skill-execution/v1"
        or not isinstance(definition.get("tasks"), list) or len(definition["tasks"]) != 1
        or not isinstance(tasks, list) or len(tasks) != 1
        or not isinstance(tasks[0], Mapping)
        or not isinstance(definition["tasks"][0], Mapping)
    ):
        raise CandidateRevisionError(
            "candidate_parent_invalid", "/previous_interpretation", "Saved parent is invalid."
        )
    task = definition["tasks"][0]
    try:
        value = {
            "title": task["title"],
            "description": task["description"],
            "input_contract": tasks[0]["input_contract"],
            "input_source_trace": tasks[0]["contract_source_trace"],
            "resource_requirements": definition["resource_requirements"],
            "resource_source_traces": definition["source_traces"],
            "tools": manifest["tools"],
            "diagnostics": manifest["compatibility"].get("diagnostics", []),
        }
    except (KeyError, TypeError, AttributeError) as error:
        raise CandidateRevisionError(
            "candidate_parent_invalid", "/previous_interpretation", "Saved parent is invalid."
        ) from error
    return deepcopy(value)


def launch_contract_checksum(value: Mapping[str, Any]) -> str:
    """調整基準を request 内で検証できる checksum にする。"""

    return "sha256:" + sha256_hex(canonical_json(value))


def validate_editable_paths(value: Sequence[str] | None) -> tuple[str, ...] | None:
    """明示 scope は準備宣言の JSON Pointer に限定し、自然言語から推測しない。"""

    if value is None:
        return None
    if (
        not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 32
        or any(not isinstance(path, str) for path in value)
    ):
        raise ValueError("Editable paths must be a nonempty bounded list of JSON Pointers")
    for path in value:
        tokens = path.split("/")
        if (
            len(path) > 512 or not path.isprintable() or not path.startswith("/")
            or len(tokens) > 16 or any(not token for token in tokens[1:])
            or tokens[1] not in _LAUNCH_FIELDS or re.search(r"~(?![01])", path)
        ):
            raise ValueError("Editable path is outside the launch contract")
    if len(set(value)) != len(value):
        raise ValueError("Editable paths must be unique")
    return tuple(value)


def validate_adjustment_scope(
    previous: Mapping[str, Any] | None,
    adjustment: Mapping[str, Any] | None,
    manifest: Mapping[str, Any],
) -> None:
    """明示された変更範囲だけを許可し、親や子の宣言を修正して通さない。"""

    if not adjustment or "editable_paths" not in adjustment:
        return
    paths = validate_editable_paths(adjustment["editable_paths"])
    if paths is None:
        raise CandidateRevisionError(
            "candidate_adjustment_scope_invalid", "/adjustment", "Explicit scope is invalid."
        )
    before = previous.get("launch_contract") if isinstance(previous, Mapping) else None
    after = snapshot_launch_contract(manifest)
    if (
        not isinstance(previous, Mapping) or not isinstance(before, Mapping) or after is None
        or previous.get("launch_contract_checksum") != launch_contract_checksum(before)
    ):
        raise CandidateRevisionError(
            "candidate_parent_invalid", "/previous_interpretation",
            "Scoped adjustment requires an intact source-execution parent.",
        )
    for changed in _changed_paths(before, after):
        if not any(changed == path or changed.startswith(path + "/") for path in paths):
            # 任意の model key を公開診断に載せず、拒否理由は固定位置に保つ。
            raise CandidateRevisionError(
                "candidate_adjustment_scope_exceeded", "/adjustment",
                "Candidate changed declarations outside the explicitly editable paths.",
            )


def repair_components(path: str, code: str) -> frozenset[str] | None:
    """一意に位置付けられる v2 のエラーだけを、修復可能な候補 component へ写像する。"""

    if code == "candidate_publish_invalid" or not path.startswith("/"):
        # Publish preflight は複数箇所を含み得る。一箇所だけの修復と偽らない。
        return None
    component = path.lstrip("/").split("/", 1)[0]
    if component in _CANDIDATE_FIELDS:
        return frozenset({component})
    if path.startswith("/skill_execution/resource_requirements"):
        return frozenset({"resource_requirements"})
    if code.startswith("contract_"):
        # v2 は入力契約のみをモデルに宣言させる。
        return frozenset({"input_contract"})
    return None


def validate_repair_scope(
    before: Mapping[str, Any], after: Mapping[str, Any], editable: frozenset[str] | None,
) -> None:
    """自動修復の非対象 component を保護する。候補はその後に全体検証する。"""

    if editable is None:
        return
    for field in _CANDIDATE_FIELDS - editable:
        # 省略と null も区別する。Schema が要求する field の削除を黙認しない。
        if (field in before) != (field in after) or canonical_json(before.get(field)) != (
            canonical_json(after.get(field))
        ):
            raise CandidateRevisionError(
                "candidate_repair_scope_exceeded", "/candidate",
                "Repair changed a candidate component unrelated to the reported error.",
            )


def _changed_paths(before: Any, after: Any, path: str = "") -> Iterator[str]:
    """追加・削除を含む差分位置を返し、数値と boolean や配列順を混同しない。"""

    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after)):
            child = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if key not in before or key not in after:
                yield child
            else:
                yield from _changed_paths(before[key], after[key], child)
    elif isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            child = f"{path}/{index}"
            if index >= len(before) or index >= len(after):
                yield child
            else:
                yield from _changed_paths(before[index], after[index], child)
    elif canonical_json(before) != canonical_json(after):
        yield path
