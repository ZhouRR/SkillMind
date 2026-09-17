"""凍結 Skill 原文の投影と出典解決を候補形式に依存せず共有する。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from skillmind.skills.capability_blueprint import CapabilityBlueprintError
from skillmind.skills.source_documents import validate_source_documents, validate_source_location

_SOURCE_REF = re.compile(r"s[0-9]+(?::[0-9]+)?\Z")


def source_index(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """凍結 index 順の識別子を原 path と全文へ対応付け、binary は本文無しで示す。"""
    source = request["source"]
    documents = validate_source_documents(source["source_documents"])
    contents = {item["path"]: item["content"] for item in documents}
    expected = {
        item["path"]: (item["sha256"], item["size"])
        for item in source["normalized_package"]["source"]["files"]
        if not item["binary"]
    }
    actual = {
        item["path"]: (item["sha256"], len(item["content"].encode("utf-8"))) for item in documents
    }
    if expected != actual:
        raise ValueError("Frozen Skill source documents differ from the source index")
    return [
        {"id": f"s{index}", "path": item["path"], "content": contents.get(item["path"])}
        for index, item in enumerate(source["normalized_package"]["source"]["files"])
    ]


def model_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """全文は一度だけ行番号付きで送り、保存 request の原 byte と意味は変えない。"""
    sources = []
    for source in source_index(request):
        content = source["content"]
        sources.append(
            {
                "id": source["id"],
                "path": source["path"],
                "numbered_text": None
                if content is None
                else "".join(
                    f"{number}: {line}"
                    for number, line in enumerate(content.splitlines(keepends=True), start=1)
                ),
            }
        )
    return {
        "sources": sources,
        "capabilities": [
            {
                "capability": item["capability"],
                "description": item["description"],
                "providers": item["providers"],
            }
            for item in request["capability_catalog"]["capabilities"]
        ],
        "static_analysis": deepcopy(request["static_analysis"]),
        "previous_interpretation": deepcopy(request.get("previous_interpretation")),
        "adjustment": deepcopy(request.get("adjustment")),
    }


def omit_optional_nulls(value: Any) -> Any:
    """候補の optional null を省略へ戻す。配列内の業務値は削除しない。"""
    if isinstance(value, dict):
        return {key: omit_optional_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [omit_optional_nulls(item) for item in value]
    return value


class SourceLocations:
    """一回の候補に対して索引を作り、全出典を同じ検証と静的診断で解決する。"""

    def __init__(self, sources: Sequence[Mapping[str, Any]]) -> None:
        """Source ID ごとの探索を辞書化し、繰り返し原文リストを走査しない。"""
        self._sources = {source["id"]: source for source in sources}
        self._files = {source["path"]: source["content"] for source in sources}

    def resolve(self, reference: str | None, pointer: str) -> dict[str, Any]:
        """元の候補 field を指して拒否し、値を例外本文へ反射しない。"""
        if reference is None:
            return {"path": None, "line": None}
        if not isinstance(reference, str) or _SOURCE_REF.fullmatch(reference) is None:
            raise CapabilityBlueprintError(
                "candidate_source_invalid", pointer, "Use a frozen source ID and optional line."
            )
        source_id, _, line_text = reference.partition(":")
        source = self._sources.get(source_id)
        if source is None:
            raise CapabilityBlueprintError(
                "candidate_source_invalid", pointer, "Use an ID from the frozen sources list."
            )
        try:
            line = int(line_text) if line_text else None
        except ValueError:
            raise CapabilityBlueprintError(
                "candidate_source_invalid", pointer, "Source line must fit the frozen document."
            ) from None
        validate_source_location(source["path"], line, self._files, pointer=pointer)
        return {"path": source["path"], "line": line}
