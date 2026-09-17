"""固定の子プロセスで JSON Schema を検証する。外部参照・動的コードは受理しない。"""

from __future__ import annotations

import json
import math
import sys
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

MAX_FILE_BYTES = 1_048_576
MAX_ERRORS = 50
MAX_DEPTH = 64
MAX_NODES = 100_000
DIALECT = "https://json-schema.org/draft/2020-12/schema"


class ValidationRejected(ValueError):
    """未実施と不適合を区別するための固定 error code。"""

    def __init__(self, code: str) -> None:
        """入力本文を例外へ保持しない。"""
        super().__init__(code)
        self.code = code


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """同名 key の後勝ちによる検証内容の曖昧さを拒否する。"""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationRejected("invalid_json")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    """JSON にない NaN/Infinity を受理しない。"""
    raise ValidationRejected("invalid_json")


def _parse(value: str) -> Any:
    """UTF-8 と JSON 構造を有界に読み、parser の例外本文を返さない。"""
    if len(value.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValidationRejected("too_large")
    try:
        parsed = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_constant)
    except (ValueError, RecursionError) as error:
        raise ValidationRejected("invalid_json") from error
    stack = [(parsed, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > MAX_DEPTH or count > MAX_NODES:
            raise ValidationRejected("too_large")
        if isinstance(node, float) and not math.isfinite(node):
            raise ValidationRejected("invalid_json")
        if isinstance(node, dict):
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)
    return parsed


def _check_schema(schema: Any, checker: FormatChecker) -> None:
    """2020-12、同一文書 fragment 参照、実装済み format だけを検証対象とする。"""
    if not isinstance(schema, (dict, bool)):
        raise ValidationRejected("invalid_schema")
    stack = [schema]
    # Schema を持つ keyword のみを辿る。default/examples 内の JSON は Schema ではない。
    singles = {
        "additionalProperties",
        "unevaluatedProperties",
        "propertyNames",
        "items",
        "contains",
        "unevaluatedItems",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
    maps = {"$defs", "definitions", "properties", "patternProperties", "dependentSchemas"}
    arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if "$schema" in node and node["$schema"] != DIALECT:
            raise ValidationRejected("unsupported_schema")
        vocabulary = node.get("$vocabulary", {})
        if isinstance(vocabulary, dict) and any(
            required and uri not in Draft202012Validator.META_SCHEMA["$vocabulary"]
            for uri, required in vocabulary.items()
        ):
            raise ValidationRejected("unsupported_schema")
        for keyword in ("$ref", "$dynamicRef"):
            if keyword in node and (
                not isinstance(node[keyword], str) or not node[keyword].startswith("#")
            ):
                raise ValidationRejected("external_reference")
        if "format" in node and (
            not isinstance(node["format"], str) or node["format"] not in checker.checkers
        ):
            raise ValidationRejected("unsupported_format")
        for key, child in node.items():
            if key in singles:
                stack.append(child)
            elif key in maps and isinstance(child, dict):
                stack.extend(child.values())
            elif key in arrays and isinstance(child, list):
                stack.extend(child)
    try:
        Draft202012Validator.check_schema(schema)
    except (SchemaError, TypeError, ValueError, RecursionError) as error:
        raise ValidationRejected("invalid_schema") from error


def _pointer(parts: Any) -> str:
    """値を出さず、エラー位置だけを JSON Pointer で有界に返す。"""
    return (
        ("/" + "/".join(str(x).replace("~", "~0").replace("/", "~1") for x in parts))[:1024]
        if parts
        else ""
    )


def validate(schema_text: str, instance_text: str) -> dict[str, Any]:
    """Schema と instance の実検証結果を返す。成功は業務上の正しさを意味しない。"""
    schema, instance = _parse(schema_text), _parse(instance_text)
    checker = FormatChecker()
    _check_schema(schema, checker)
    # Registry の既定は外部取得を拒否する。別の取得 callback や URL I/O を加えない。
    validator = Draft202012Validator(schema, format_checker=checker, registry=Registry())
    errors: list[dict[str, str]] = []
    truncated = False
    try:
        for error in validator.iter_errors(instance):
            if len(errors) == MAX_ERRORS:
                truncated = True
                break
            keyword = str(error.validator)[:100]
            errors.append(
                {
                    "instance_path": _pointer(error.absolute_path),
                    "schema_path": _pointer(error.absolute_schema_path),
                    "keyword": keyword,
                    "message": f"JSON Schema constraint failed: {keyword}",
                }
            )
    except (Unresolvable, RecursionError, ValueError, TypeError) as error:
        raise ValidationRejected("invalid_schema") from error
    return {
        "valid": not errors,
        "errors": errors,
        "truncated": truncated,
        "schema_dialect": DIALECT,
        "format_assertions": True,
    }


def main() -> None:
    """CPU・memory 上限付き固定 worker。入力は本文のみで、file/URL を開かない。"""
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
    resource.setrlimit(resource.RLIMIT_AS, (268_435_456, 268_435_456))
    try:
        raw = sys.stdin.buffer.read(4 * MAX_FILE_BYTES + 1)
        if len(raw) > 4 * MAX_FILE_BYTES:
            raise ValidationRejected("too_large")
        request = json.loads(raw)
        result = validate(request["schema"], request["instance"])
    except ValidationRejected as error:
        result = {"error": error.code}
    except (MemoryError, RecursionError):
        result = {"error": "validation_limit"}
    except Exception:
        # 外部入力を扱う process の最終境界。traceback と本文は親へ漏らさない。
        result = {"error": "invalid_schema"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
