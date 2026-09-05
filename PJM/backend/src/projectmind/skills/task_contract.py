"""TaskContractDraft を安全な JSON Schema へ決定的にコンパイルする。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from projectmind.core.hashing import canonical_json, sha256_hex

TASK_CONTRACT_VERSION = "projectmind.task-contract-draft/v1"
MAX_CONTRACT_FIELDS = 100
MAX_CONTRACT_DEPTH = 5
MAX_ENUM_VALUES = 50
MAX_DESCRIPTION_LENGTH = 500
MAX_PATTERN_LENGTH = 128
MAX_STRING_LENGTH = 100_000

_ALLOWED_TYPES = frozenset({"array", "boolean", "integer", "number", "object", "string"})
_COMMON_KEYS = frozenset(
    {
        "description",
        "enum",
        "fields",
        "items",
        "max_length",
        "maximum",
        "min_length",
        "minimum",
        "pattern",
        "type",
    }
)
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Group、alternation、range quantifier、backreference は ReDoS 評価が必要になるため
# v1 では扱わない。
_UNSAFE_PATTERN_PARTS = ("(", ")", "{", "}", "|", "\\1", "\\2", "\\k", "\\g")


class TaskContractCompilationError(ValueError):
    """安全に公開できる code と path を持つ contract compile failure。"""

    def __init__(self, code: str, path: str, message: str) -> None:
        """機密値を含まない安定した失敗情報を保持する。"""

        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


@dataclass(frozen=True, slots=True)
class CompiledTaskContract:
    """生成済み JSON Schema と canonical checksum の不変な組。"""

    schema: dict[str, Any]
    checksum: str


@dataclass(slots=True)
class _CompileState:
    """一回の compile で共有する field 数を追跡する。"""

    field_count: int = 0


def compile_task_contract(draft: Mapping[str, Any]) -> CompiledTaskContract:
    """同じ TaskContractDraft から同じ Draft 2020-12 Schema と checksum を返す。"""

    normalized = dict(draft)
    _reject_unknown_keys(
        normalized,
        allowed=_COMMON_KEYS | {"contract_version"},
        path="/",
    )
    if normalized.get("contract_version") != TASK_CONTRACT_VERSION:
        raise _failure(
            "contract_version_invalid",
            "/contract_version",
            f"contract_version must be {TASK_CONTRACT_VERSION}",
        )
    schema = _compile_node(normalized, path="/", depth=1, state=_CompileState())
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **schema,
    }
    Draft202012Validator.check_schema(schema)
    return CompiledTaskContract(
        schema=schema,
        checksum=f"sha256:{sha256_hex(canonical_json(schema))}",
    )


def _compile_node(
    node: Mapping[str, Any],
    *,
    path: str,
    depth: int,
    state: _CompileState,
) -> dict[str, Any]:
    """一つの contract node を再帰的に JSON Schema へ変換する。"""

    if depth > MAX_CONTRACT_DEPTH:
        raise _failure(
            "contract_depth_exceeded",
            path,
            f"Contract nesting depth must not exceed {MAX_CONTRACT_DEPTH}",
        )
    value_type = node.get("type")
    if not isinstance(value_type, str) or value_type not in _ALLOWED_TYPES:
        raise _failure("contract_type_invalid", f"{path}type", "Contract type is invalid")

    schema: dict[str, Any] = {"type": value_type}
    description = node.get("description")
    if description is not None:
        if not isinstance(description, str) or not description.strip():
            raise _failure(
                "contract_description_invalid",
                f"{path}description",
                "Description must be a non-empty string",
            )
        if len(description) > MAX_DESCRIPTION_LENGTH:
            raise _failure(
                "contract_description_too_long",
                f"{path}description",
                f"Description must not exceed {MAX_DESCRIPTION_LENGTH} characters",
            )
        schema["description"] = description

    _compile_enum(node, schema, value_type=value_type, path=path)
    if value_type == "object":
        _compile_object(node, schema, path=path, depth=depth, state=state)
    elif value_type == "array":
        _compile_array(node, schema, path=path, depth=depth, state=state)
    elif value_type == "string":
        _compile_string(node, schema, path=path)
    elif value_type in {"integer", "number"}:
        _compile_number(node, schema, path=path)
    _reject_inapplicable_keywords(node, value_type=value_type, path=path)
    return schema


def _compile_object(
    node: Mapping[str, Any],
    schema: dict[str, Any],
    *,
    path: str,
    depth: int,
    state: _CompileState,
) -> None:
    """Object field を重複なくコンパイルし、余分な property を拒否する。"""

    fields = node.get("fields", [])
    if not _is_sequence(fields):
        raise _failure("contract_fields_invalid", f"{path}fields", "Fields must be an array")
    properties: dict[str, Any] = {}
    required: list[str] = []
    for index, raw_field in enumerate(fields):
        field_path = f"{path}fields/{index}/"
        if not isinstance(raw_field, Mapping):
            raise _failure("contract_field_invalid", field_path, "Field must be an object")
        field = dict(raw_field)
        _reject_unknown_keys(
            field,
            allowed=_COMMON_KEYS | {"key", "required"},
            path=field_path,
        )
        key = field.get("key")
        if not isinstance(key, str) or _KEY_PATTERN.fullmatch(key) is None:
            raise _failure(
                "contract_field_key_invalid",
                f"{field_path}key",
                "Field key must use lower snake_case",
            )
        if key in properties:
            raise _failure(
                "contract_field_duplicate",
                f"{field_path}key",
                f"Duplicate field key: {key}",
            )
        required_value = field.get("required")
        if not isinstance(required_value, bool):
            raise _failure(
                "contract_required_invalid",
                f"{field_path}required",
                "Field required must be a boolean",
            )
        state.field_count += 1
        if state.field_count > MAX_CONTRACT_FIELDS:
            raise _failure(
                "contract_field_limit_exceeded",
                field_path,
                f"Contract must not contain more than {MAX_CONTRACT_FIELDS} fields",
            )
        properties[key] = _compile_node(
            field,
            path=field_path,
            depth=depth + 1,
            state=state,
        )
        if required_value:
            required.append(key)
    schema["properties"] = properties
    schema["additionalProperties"] = False
    if required:
        schema["required"] = sorted(required)


def _compile_array(
    node: Mapping[str, Any],
    schema: dict[str, Any],
    *,
    path: str,
    depth: int,
    state: _CompileState,
) -> None:
    """Array item contract を一つだけ要求してコンパイルする。"""

    items = node.get("items")
    if not isinstance(items, Mapping):
        raise _failure(
            "contract_items_missing", f"{path}items", "Array items contract is required"
        )
    item = dict(items)
    _reject_unknown_keys(item, allowed=_COMMON_KEYS, path=f"{path}items/")
    schema["items"] = _compile_node(
        item,
        path=f"{path}items/",
        depth=depth + 1,
        state=state,
    )


def _compile_string(
    node: Mapping[str, Any], schema: dict[str, Any], *, path: str
) -> None:
    """String 長と安全な正規表現 subset をコンパイルする。"""

    minimum = _optional_integer(node, "min_length", path=path, lower=0)
    maximum = _optional_integer(
        node, "max_length", path=path, lower=0, upper=MAX_STRING_LENGTH
    )
    if minimum is not None and maximum is not None and minimum > maximum:
        raise _failure(
            "contract_string_range_invalid",
            path,
            "min_length must not exceed max_length",
        )
    if minimum is not None:
        schema["minLength"] = minimum
    if maximum is not None:
        schema["maxLength"] = maximum
    pattern = node.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_LENGTH:
            raise _failure(
                "contract_pattern_invalid",
                f"{path}pattern",
                f"Pattern must contain 1 to {MAX_PATTERN_LENGTH} characters",
            )
        if any(part in pattern for part in _UNSAFE_PATTERN_PARTS):
            raise _failure(
                "contract_pattern_unsafe",
                f"{path}pattern",
                "Pattern uses constructs unsupported by TaskContract v1",
            )
        try:
            re.compile(pattern)
        except re.error as error:
            raise _failure(
                "contract_pattern_invalid", f"{path}pattern", "Pattern is invalid"
            ) from error
        schema["pattern"] = pattern


def _compile_number(
    node: Mapping[str, Any], schema: dict[str, Any], *, path: str
) -> None:
    """Number range を bool と区別してコンパイルする。"""

    minimum = _optional_number(node, "minimum", path=path)
    maximum = _optional_number(node, "maximum", path=path)
    if minimum is not None and maximum is not None and minimum > maximum:
        raise _failure(
            "contract_number_range_invalid", path, "minimum must not exceed maximum"
        )
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum


def _compile_enum(
    node: Mapping[str, Any],
    schema: dict[str, Any],
    *,
    value_type: str,
    path: str,
) -> None:
    """Scalar type と一致する小さな enum だけを受理する。"""

    values = node.get("enum")
    if values is None:
        return
    if value_type in {"array", "object"} or not _is_sequence(values):
        raise _failure(
            "contract_enum_invalid", f"{path}enum", "Enum is supported only for scalar types"
        )
    if not values or len(values) > MAX_ENUM_VALUES:
        raise _failure(
            "contract_enum_limit_exceeded",
            f"{path}enum",
            f"Enum must contain 1 to {MAX_ENUM_VALUES} values",
        )
    unique: set[str] = set()
    normalized: list[Any] = []
    for index, value in enumerate(values):
        if not _matches_type(value, value_type):
            raise _failure(
                "contract_enum_type_mismatch",
                f"{path}enum/{index}",
                "Enum value does not match contract type",
            )
        marker = canonical_json(value)
        if marker in unique:
            raise _failure(
                "contract_enum_duplicate",
                f"{path}enum/{index}",
                "Enum values must be unique",
            )
        unique.add(marker)
        normalized.append(value)
    schema["enum"] = normalized


def _reject_inapplicable_keywords(
    node: Mapping[str, Any], *, value_type: str, path: str
) -> None:
    """Type ごとに意味を持たない keyword を拒否し、曖昧な draft を残さない。"""

    allowed_by_type = {
        "object": {"fields"},
        "array": {"items"},
        "string": {"enum", "max_length", "min_length", "pattern"},
        "integer": {"enum", "maximum", "minimum"},
        "number": {"enum", "maximum", "minimum"},
        "boolean": {"enum"},
    }
    structural = {
        "enum",
        "fields",
        "items",
        "max_length",
        "maximum",
        "min_length",
        "minimum",
        "pattern",
    }
    unsupported = sorted((set(node) & structural) - allowed_by_type[value_type])
    if unsupported:
        raise _failure(
            "contract_keyword_inapplicable",
            path,
            f"Keywords are not valid for type {value_type}: {', '.join(unsupported)}",
        )


def _reject_unknown_keys(
    node: Mapping[str, Any], *, allowed: frozenset[str] | set[str], path: str
) -> None:
    """元モデル外の JSON Schema keyword や実行式を fail closed で拒否する。"""

    unknown = sorted(set(node) - set(allowed))
    if unknown:
        raise _failure(
            "contract_keyword_unknown",
            path,
            f"Unsupported contract keys: {', '.join(unknown)}",
        )


def _optional_integer(
    node: Mapping[str, Any],
    key: str,
    *,
    path: str,
    lower: int,
    upper: int | None = None,
) -> int | None:
    """Optional integer constraint を範囲検査して返す。"""

    value = node.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _failure("contract_constraint_invalid", f"{path}{key}", f"{key} must be integer")
    if value < lower or (upper is not None and value > upper):
        raise _failure(
            "contract_constraint_out_of_range", f"{path}{key}", f"{key} is out of range"
        )
    return value


def _optional_number(node: Mapping[str, Any], key: str, *, path: str) -> int | float | None:
    """Optional numeric constraint を bool と区別して返す。"""

    value = node.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _failure("contract_constraint_invalid", f"{path}{key}", f"{key} must be numeric")
    return value


def _matches_type(value: Any, value_type: str) -> bool:
    """JSON scalar が contract type と一致するかを bool/int の差も含めて判定する。"""

    if value_type == "string":
        return isinstance(value, str)
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return False


def _is_sequence(value: Any) -> bool:
    """JSON array として扱える sequence だけを返す。"""

    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def _failure(code: str, path: str, message: str) -> TaskContractCompilationError:
    """安定 code/path を持つ compile failure を生成する。"""

    return TaskContractCompilationError(code, path, message)
