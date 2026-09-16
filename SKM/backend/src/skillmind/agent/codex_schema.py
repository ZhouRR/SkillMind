"""業務 Schema を変更せず、Codex の strict 出力へ可逆変換する。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


class CodexOutputError(ValueError):
    """転送用 JSON を業務候補へ復元できない場合の固定分類。"""

    def __init__(self, reason: str, path: tuple[str | int, ...] = ()) -> None:
        """値や自由 key を含めず、固定分類と Schema 上の位置だけを保持する。"""

        super().__init__(reason)
        self.reason = reason
        self.path = path

    @property
    def detail(self) -> str:
        """有界な診断をログと候補修復の両方へ渡す。"""

        parts = [
            str(part) if re.fullmatch(r"[A-Za-z0-9_]{1,64}", str(part)) else "field"
            for part in self.path[:12]
        ]
        return f"codex_output:{self.reason}; path=/" + "/".join(parts)

def _json(text: str) -> Any:
    """非 JSON 数値と重複 key による黙示的な値の欠落を拒否する。"""

    def invalid(_: str) -> Any:
        """Python が許容する非 JSON 数値を固定分類で拒否する。"""

        raise CodexOutputError("non_json_constant")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        """同じ key の後勝ちによる値の欠落を防ぐ。"""

        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise CodexOutputError("duplicate_key")
            result[key] = value
        return result

    try:
        return json.loads(text, parse_constant=invalid, object_pairs_hook=pairs)
    except CodexOutputError:
        raise
    except (ValueError, RecursionError) as error:
        raise CodexOutputError("invalid_json") from error


@dataclass
class _Node:
    """Wire Schema と対応する復元規則を一緒に保持する。"""

    schema: dict[str, Any]
    encoded: bool = False
    properties: dict[str, _Node] = field(default_factory=dict)
    optional: set[str] = field(default_factory=set)
    items: _Node | None = None
    presence_value: _Node | None = None

    def decode(self, value: Any, path: tuple[str | int, ...] = ()) -> Any:
        """省略 sentinel と JSON 文字列だけを復元し、業務値を推測しない。"""

        if self.presence_value is not None:
            return self.presence_value.decode(value["value"], (*path, "value"))
        if self.encoded:
            if not isinstance(value, str):
                raise CodexOutputError("expected_encoded_json", path)
            try:
                return _json(value)
            except CodexOutputError as error:
                raise CodexOutputError(error.reason, path) from error
        if self.schema.get("type") == "object":
            return {
                key: self.properties[key].decode(item, (*path, key))
                for key, item in value.items()
                if key not in self.optional or (
                    item is not None and (
                        self.properties[key].presence_value is None or item["present"]
                    )
                )
            }
        if self.items is not None:
            return [self.items.decode(item, (*path, index)) for index, item in enumerate(value)]
        return value


def _encoded(kind: str | None = None) -> _Node:
    """自由 key・複合型・深い構造は元の JSON 全体を文字列で運ぶ。"""

    schema: dict[str, Any] = {
        "type": "string",
        "description": (
            f"JSON-encoded {kind or 'value'}: serialize the complete original value as JSON text. "
            "Preserve all keys and types. Follow the original business schema. "
            "Encode exactly once, without an extra JSON string wrapper."
        ),
    }
    if kind in {"object", "array"}:
        # 元の型を wire にも制約し、自由 object の文字列化や二重 encoding を防ぐ。
        schema["pattern"] = r"^\s*\{" if kind == "object" else r"^\s*\["
    return _Node(schema, encoded=True)


class CodexOutputSchema:
    """Strict Schema は転送形式だけを担い、復元後の業務検証を代替しない。"""

    def __init__(self, original: Mapping[str, Any]) -> None:
        """参照を局所展開し、過深な構造と自由値は JSON 文字列へ退避する。"""

        self._original = original
        self._root = self._compile(original, depth=0, references=frozenset())
        self._wrapped = self._root.schema.get("type") != "object"
        self.schema: dict[str, Any] = (
            {
                "type": "object",
                "properties": {"result_json": self._root.schema},
                "required": ["result_json"],
                "additionalProperties": False,
            }
            if self._wrapped
            else self._root.schema
        )
        self._validator = Draft202012Validator(self.schema)

    @property
    def instructions(self) -> str:
        """元の業務契約と API 転送形式の役割を model に明示する。"""

        return (
            "\n\nCodex output transport: the requested outputSchema is a reversible wire "
            "representation of the original business schema. Follow it for the final response. "
            "For fields described as JSON-encoded, serialize the original complete value "
            "as a JSON string (including nested objects, arrays, and null). A null in an "
            "optional wire field means OMIT that property; use the JSON string \"null\" for "
            "an explicit null in a JSON-encoded field. For optional fields wrapped in "
            "{present, value}, present=false omits the original field; present=true retains "
            "the native value, including explicit null. If the root is wrapped in result_json, "
            "place the complete original result there. The restored result must satisfy ALL "
            "constraints of the original business schema; these are not relaxed."
        )

    def decode(self, text: str) -> dict[str, Any]:
        """Wire を検証してから元の dict 候補へ戻し、本文を例外へ含めない。"""

        value = _json(text)
        try:
            self._validator.validate(value)
        except ValidationError as error:
            raise CodexOutputError(
                "invalid_wire_" + str(error.validator), tuple(error.absolute_path)
            ) from error
        except RecursionError as error:
            raise CodexOutputError("output_depth_exceeded") from error
        result = self._root.decode(value["result_json"] if self._wrapped else value)
        if not isinstance(result, dict):
            raise CodexOutputError("expected_object")
        return result

    def _compile(
        self, source: Any, *, depth: int, references: frozenset[str]
    ) -> _Node:
        """Expressible な骨格だけを投影し、unsupported 制約は原検証へ残す。"""

        if not isinstance(source, Mapping):
            return _encoded()
        if "$ref" in source:
            reference = source["$ref"]
            if (
                not isinstance(reference, str)
                or not reference.startswith("#/")
                or reference in references
                or set(source) - {"$ref", "description", "title"}
            ):
                return _encoded()
            target: Any = self._original
            for part in reference[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return self._compile(target, depth=depth, references=references | {reference})
        kind = source.get("type")
        values = [source["const"]] if "const" in source else source.get("enum")
        if kind is None and values:
            types = {type(value) for value in values}
            if len(types) == 1:
                kind = {str: "string", bool: "boolean", int: "integer", float: "number"}.get(
                    next(iter(types))
                )
        scalar_types = {"string", "number", "integer", "boolean", "null"}
        if (
            isinstance(kind, list) and set(kind) <= scalar_types
            and not any(key in source for key in ("anyOf", "oneOf"))
        ):
            # Nullable な source path 等を二重 JSON 化せず、native union として保持する。
            union_schema: dict[str, Any] = {"type": list(kind)}
            if values is not None:
                union_schema["enum"] = values
            return _Node(union_schema)
        if not isinstance(kind, str) or any(
            key in source for key in ("anyOf", "oneOf", "prefixItems")
        ):
            return _encoded()
        if kind == "object":
            if (
                depth >= 6
                or source.get("additionalProperties") is not False
                or "patternProperties" in source
                or values is not None
            ):
                return _encoded("object")
            children: dict[str, _Node] = {}
            properties: dict[str, Any] = {}
            optional: set[str] = set()
            for key, child in source.get("properties", {}).items():
                node = self._compile(child, depth=depth + 1, references=references)
                if key not in source.get("required", []):
                    optional.add(key)
                    # Nullable scalar は present で省略を表し、通常の文字列を二重 JSON 化しない。
                    child_type = node.schema.get("type")
                    if child_type == "null" or (
                        isinstance(child_type, list) and "null" in child_type
                    ):
                        node = _Node(
                            {
                                "type": "object",
                                "description": (
                                    "Optional field: present=false omits the field; "
                                    "present=true retains value, including explicit null."
                                ),
                                "properties": {
                                    "present": {"type": "boolean"}, "value": node.schema,
                                },
                                "required": ["present", "value"],
                                "additionalProperties": False,
                            },
                            presence_value=node,
                        )
                        properties[key] = node.schema
                    else:
                        properties[key] = {"anyOf": [node.schema, {"type": "null"}]}
                else:
                    properties[key] = node.schema
                children[key] = node
            return _Node(
                {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                    "additionalProperties": False,
                },
                properties=children,
                optional=optional,
            )
        if kind == "array":
            if depth >= 6 or values is not None:
                return _encoded("array")
            items = self._compile(source.get("items"), depth=depth + 1, references=references)
            return _Node({"type": "array", "items": items.schema}, items=items)
        if kind not in scalar_types:
            return _encoded()
        schema: dict[str, Any] = {"type": kind}
        if values is not None:
            schema["enum"] = values
        if isinstance(source.get("description"), str):
            schema["description"] = source["description"]
        return _Node(schema)


class NativeCodexOutputSchema:
    """候補専用の strict Schema を変換せず使い、null やネストも元の JSON で運ぶ。"""

    def __init__(self, schema: Mapping[str, Any]) -> None:
        """SDK に渡す Schema と復元後に検証する Schema を一致させる。"""

        self.schema = dict(schema)

    @property
    def instructions(self) -> str:
        """別の wire 形式を指示せず、単一の出力契約を使う。"""

        return "\nReturn native JSON matching outputSchema. Do not JSON-encode objects or arrays."

    def decode(self, text: str) -> dict[str, Any]:
        """重複 key/非 JSON 定数を拒否し、意味候補の検査は共有 runner へ委ねる。"""

        value = _json(text)
        if not isinstance(value, dict):
            raise CodexOutputError("expected_object")
        # Provider が Schema を逸脱した候補も共有 validator の有界修復へ渡す。
        return value
