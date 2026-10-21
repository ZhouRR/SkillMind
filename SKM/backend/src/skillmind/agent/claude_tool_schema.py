"""業務契約を変更せず、Claude が受理できる Tool 入力 schema へ展開する。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

# Anthropic API は最上位のこれらを拒否し、Claude CLI はその Tool 自体を除外する。
# 入れ子の位置は許容されるため、最上位だけを取り除く。
ROOT_KEYWORDS = ("oneOf", "anyOf", "allOf", "not", "if", "then", "else")
_ALTERNATIVES = {
    "oneOf": "Exactly one request form applies; required fields per form",
    "anyOf": "At least one of these field sets is required",
}


class ClaudeToolInputSchema:
    """SDK 公開用の入力 schema。検証は引き続き原契約だけで行う。

    SDK は公開 schema でも呼出前に入力を検証するため、公開 schema は原契約が受理する入力を
    すべて受理しなければならない(上位集合)。最上位が oneOf/anyOf だけの契約は、各 alternative
    の properties を最上位へ併記して SDK の公開条件も満たす。排他・条件必須は説明文へ移す。
    """

    def __init__(self, original: Mapping[str, Any]) -> None:
        """原契約は複写し、公開 schema の変更が登録 Tool へ波及しないようにする。"""

        source = deepcopy(dict(original))
        schema = {key: value for key, value in source.items() if key not in ROOT_KEYWORDS}
        properties: dict[str, Any] = dict(schema.get("properties", {}))
        # 最上位に定義があれば、入力は必ずそれを満たすため alternative 側の絞り込みは不要。
        from_alternatives: dict[str, list[Any]] = {}
        for key in ("oneOf", "anyOf"):
            for alternative in source.get(key, []):
                for name, definition in alternative.get("properties", {}).items():
                    if name not in properties:
                        definitions = from_alternatives.setdefault(name, [])
                        if definition not in definitions:
                            definitions.append(definition)
        for name, definitions in from_alternatives.items():
            # alternative 間で定義が異なる field は、どれかを満たせば受理する。
            properties[name] = definitions[0] if len(definitions) == 1 else {"anyOf": definitions}
        schema["properties"] = properties
        note = self._alternatives_note(source)
        if note:
            schema["description"] = "\n".join(filter(None, [schema.get("description"), note]))
        self.schema: dict[str, Any] = schema

    @staticmethod
    def _alternatives_note(source: Mapping[str, Any]) -> str:
        """失われる排他・必須の構造を、alternative ごとの必須 field として文章で残す。"""

        notes = []
        for key, label in _ALTERNATIVES.items():
            forms = [
                ", ".join(alternative.get("required") or []) or "no additional fields"
                for alternative in source.get(key, [])
            ]
            if forms:
                form_list = "; ".join(f"({index}) {form}" for index, form in enumerate(forms, 1))
                notes.append(f"{label}: {form_list}")
        return " ".join(notes)
