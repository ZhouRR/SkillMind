"""Run file の JSON を式評価せず、明示 Pointer と有界な配列頁で読む。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.core.hashing import canonical_json

MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_SELECTION_BYTES = 64 * 1024
_POINTER = re.compile(r"(?:/(?:[^~/]|~[01])*)*")
_MISSING = object()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """重複 key の後勝ちで値の意味を変えない。"""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _constant(value: str) -> Any:
    """JSON にない NaN/Infinity は受け付けない。"""
    raise ValueError("Invalid JSON number")


def _resolve(value: Any, pointer: str) -> Any:
    """RFC 6901 の原 key と配列添字だけを使い、欠落と null/false を分ける。"""
    for raw in pointer.split("/")[1:] if pointer else ():
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if key not in value:
                return _MISSING
            value = value[key]
        elif isinstance(value, list):
            if re.fullmatch(r"0|[1-9][0-9]*", key) is None or int(key) >= len(value):
                return _MISSING
            value = value[int(key)]
        else:
            return _MISSING
    return value


def select_json(text: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """元 byte の hash は呼出し側で確認し、応答だけを小さい選択結果にする。"""
    pointers = arguments.get("pointers")
    offset, limit = arguments.get("array_offset", 0), arguments.get("array_limit", 20)
    if (
        not isinstance(pointers, list) or not 1 <= len(pointers) <= 50
        or any(not isinstance(p, str) or len(p) > 2048 or _POINTER.fullmatch(p) is None
               for p in pointers)
        or len(set(pointers)) != len(pointers)
        or type(offset) is not int or offset < 0
        or type(limit) is not int or not 1 <= limit <= 200
        or not isinstance(arguments.get("expected_hash"), str)
        or re.fullmatch(r"sha256:[a-f0-9]{64}", arguments["expected_hash"]) is None
        or any(k in arguments for k in ("offset", "max_chars", "line_start", "line_end"))
    ):
        raise ToolProviderError("invalid_request", "JSON selection is invalid", retryable=False)
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ToolProviderError("too_large", "JSON source exceeds its read limit", retryable=False)
    try:
        source = json.loads(text, object_pairs_hook=_object, parse_constant=_constant)
        stack = [(source, 0)]
        nodes = 0
        while stack:
            value, depth = stack.pop()
            nodes += 1
            if depth > 64 or nodes > 1_000_000:
                raise ValueError("JSON structure is too large")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Invalid JSON number")
            if isinstance(value, dict):
                stack.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, list):
                stack.extend((item, depth + 1) for item in value)
    except (ValueError, RecursionError):
        raise ToolProviderError(
            "invalid_request", "Workspace file is not supported JSON", retryable=False,
        ) from None
    selections = []
    truncated = False
    for pointer in pointers:
        value = _resolve(source, pointer)
        entry: dict[str, Any] = {"pointer": pointer, "exists": value is not _MISSING}
        if value is not _MISSING:
            if isinstance(value, list):
                end = min(len(value), offset + limit)
                next_offset = end if end < len(value) else None
                entry.update(value=value[offset:end], offset=offset, next_offset=next_offset,
                             total_items=len(value))
                truncated = truncated or next_offset is not None
            else:
                entry["value"] = value
        selections.append(entry)
    if len(canonical_json(selections).encode("utf-8")) > MAX_SELECTION_BYTES:
        raise ToolProviderError(
            "too_large", "JSON selection is too large; select narrower pointers or array pages",
            retryable=False,
        )
    return {
        "format": "json", "selections": selections, "truncated": truncated,
        "warnings": ["Selected arrays have more items"] if truncated else [],
    }
