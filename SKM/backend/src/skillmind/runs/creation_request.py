"""資源の現在値から独立した、versioned Run 作成意図を定義する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.documents.snapshot import ALL_DOCUMENTS_SELECTION, parse_document_selection

CREATION_REQUEST_FIELD = "creation_request"
_REQUEST_FIELDS = frozenset(
    {
        "request_version",
        "project_id",
        "skill_version_id",
        "task_key",
        "actor_id",
        "input",
        "sources",
    }
)


def normalize_source_choices(sources: dict[str, str]) -> dict[str, str]:
    """集合と UUID の表記だけを正規化し、未指定と明示選択を混同しない。"""

    normalized: dict[str, str] = {}
    for key, token in sources.items():
        if not isinstance(key, str) or not key or not isinstance(token, str):
            raise ValueError("Resource choices must map requirement names to strings")
        if token == ALL_DOCUMENTS_SELECTION or token.startswith(("document:", "documents:")):
            selection = parse_document_selection(token)
            if selection.mode == "ALL":
                token = ALL_DOCUMENTS_SELECTION
            else:
                prefix = "document" if selection.mode == "SINGLE" else "documents"
                token = prefix + ":" + ",".join(str(item) for item in selection.document_ids)
        elif token.startswith("integration:"):
            try:
                token = f"integration:{UUID(token.removeprefix('integration:'))}"
            except ValueError as error:
                raise ValueError("Integration candidate identity is invalid") from error
        normalized[key] = token
    return normalized


@dataclass(frozen=True, slots=True)
class TaskRunIntent:
    """初回の資源展開・権限計算と分離して保持する作成要求の同一性。"""

    project_id: UUID
    skill_version_id: UUID
    task_key: str
    actor_id: UUID
    input_json: dict[str, Any]
    sources: dict[str, str]

    def __post_init__(self) -> None:
        """呼出し元の可変 dict を所有せず、生成時に表記と形を固定する。"""

        if not all(
            isinstance(item, UUID)
            for item in (self.project_id, self.skill_version_id, self.actor_id)
        ):
            raise ValueError("Creation request identities must be UUIDs")
        if not isinstance(self.task_key, str) or not self.task_key:
            raise ValueError("Creation request must identify an exact task")
        if not isinstance(self.input_json, dict) or not isinstance(self.sources, dict):
            raise ValueError("Creation input and sources must be objects")
        object.__setattr__(self, "input_json", deepcopy(self.input_json))
        object.__setattr__(self, "sources", normalize_source_choices(self.sources))

    def to_json(self) -> dict[str, Any]:
        """内部 task snapshot に保存する、自己記述的な要求を返す。"""

        return {
            "request_version": "v1",
            "project_id": str(self.project_id),
            "skill_version_id": str(self.skill_version_id),
            "task_key": self.task_key,
            "actor_id": str(self.actor_id),
            "input": deepcopy(self.input_json),
            "sources": dict(self.sources),
        }

    def fingerprint(self) -> str:
        """trace、資源の現在値や生成 ID ではなく作成意図の hash を返す。"""

        return sha256_hex(canonical_json(self.to_json()))

    @classmethod
    def from_json(cls, value: object) -> TaskRunIntent:
        """保存済みの形式を厳格に読み、未知版や非正規の identity を拒否する。"""

        if (
            not isinstance(value, dict)
            or set(value) != _REQUEST_FIELDS
            or value.get("request_version") != "v1"
        ):
            raise ValueError("Stored creation request has an unsupported format")
        if not all(
            isinstance(value.get(key), str)
            for key in ("project_id", "skill_version_id", "actor_id", "task_key")
        ):
            raise ValueError("Stored creation request identities are invalid")
        intent = cls(
            project_id=UUID(value["project_id"]),
            skill_version_id=UUID(value["skill_version_id"]),
            task_key=value["task_key"],
            actor_id=UUID(value["actor_id"]),
            input_json=value["input"],
            sources=value["sources"],
        )
        if canonical_json(intent.to_json()) != canonical_json(value):
            raise ValueError("Stored creation request is not canonical")
        return intent
