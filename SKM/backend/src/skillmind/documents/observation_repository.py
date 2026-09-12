"""成功済み metadata Evidence だけを元 Run の文書観測として復元する読取 repository。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import Evidence, Run, ToolCall
from skillmind.documents.snapshot import (
    DOCUMENT_INSPECT_CAPABILITY,
    DOCUMENT_LIST_CAPABILITY,
    DOCUMENT_PROVIDER,
    DocumentSnapshotError,
    FrozenDocument,
)
from skillmind.documents.source import ProjectDocumentObservation, validate_source_object_key
from skillmind.storage import FileStorageError
from skillmind.storage.observation import BlobObservation


class DocumentObservationLookup(Protocol):
    """現在の実行権とは分離し、原 Run の確定した観測来歴だけを照合する port。"""

    async def load(
        self, *, project_id: UUID, run_id: UUID, document: FrozenDocument, reference: str
    ) -> ProjectDocumentObservation | None:
        """同一 Run/文書の観測だけを返す。欠落・不正記録は代替観測を作らない。"""

        ...


class PostgresDocumentObservationLookup:
    """ToolCall の成功結果と Evidence の原所属・内容を同じ SELECT で突き合わせる。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Worker と同じ正本 DB を使い、外部接続や storage 読取を行わない。"""

        self._session_factory = session_factory

    async def load(
        self, *, project_id: UUID, run_id: UUID, document: FrozenDocument, reference: str
    ) -> ProjectDocumentObservation | None:
        """越境・旧未確認記録を同じ不成立として扱い、実 I/O の前に session を閉じる。"""

        if not valid_observation_reference(reference):
            return None
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(Evidence, ToolCall)
                        .join(ToolCall, ToolCall.id == Evidence.tool_call_id)
                        .join(Run, Run.id == ToolCall.run_id)
                        .where(
                            Run.id == run_id,
                            Run.project_id == project_id,
                            Evidence.run_id == run_id,
                            Evidence.evidence_ref == reference,
                            ToolCall.run_id == run_id,
                            ToolCall.status == "SUCCEEDED",
                            ToolCall.capability_version.in_(
                                (DOCUMENT_INSPECT_CAPABILITY, DOCUMENT_LIST_CAPABILITY)
                            ),
                            ToolCall.provider == DOCUMENT_PROVIDER,
                            ToolCall.integration_id.is_(None),
                        )
                    )
                ).one_or_none()
                if row is None:
                    return None
                evidence, tool = row
                return _restore(evidence, tool, project_id=project_id, document=document)
        except SQLAlchemyError as error:
            raise DocumentSnapshotError("Stored document observation is unavailable") from error


def valid_observation_reference(value: object) -> bool:
    """任意文字列や SQL 的入力を、成功済み Evidence ID として問い合わせない。"""

    return (
        isinstance(value, str)
        and len(value) <= 64
        and re.fullmatch(r"ev_[a-zA-Z0-9_-]+", value) is not None
    )


def _restore(
    evidence: Evidence, tool: ToolCall, *, project_id: UUID, document: FrozenDocument
) -> ProjectDocumentObservation | None:
    """本文の未検証フラグ・観測 JSON・成功応答・原所在摘要を補完せず検査する。"""

    metadata = evidence.metadata_json
    result = _observation_response(tool, evidence.evidence_ref)
    if (
        evidence.evidence_type != "document"
        or evidence.artifact_ref is not None
        or evidence.source_uri
        != f"document://projects/{project_id}/{document.document_id}/metadata"
        or evidence.source_locator
        != {"document_id": str(document.document_id), "path": document.path}
        or tool.error_json is not None
        or not isinstance(metadata, dict)
        or set(metadata) != {"observation_version", "observation", "source_reference_checksum"}
        or metadata.get("observation_version") not in ("v1", "v2")
        or not isinstance(result, dict)
        or result.get("status") != "success"
        or result.get("provider") != "project"
        or result.get("evidence_refs") != [evidence.evidence_ref]
    ):
        return None
    observation = metadata.get("observation")
    reference_checksum = metadata.get("source_reference_checksum")
    expected_fields = {"document", "storage", "observed_at", "content_verified"}
    if metadata["observation_version"] == "v2":
        expected_fields.add("source_object_key")
    if (
        not isinstance(observation, dict)
        or set(observation) != expected_fields
        or observation.get("document") != document.to_json()
        or observation.get("content_verified") is not False
        or ("source_object_key" in result) != (metadata["observation_version"] == "v2")
        or not isinstance(reference_checksum, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", reference_checksum) is None
        or any(result.get(key) != value for key, value in observation.items())
    ):
        return None
    try:
        body = canonical_json(observation)
        if len(body.encode("utf-8")) > 65_536:
            return None
        if canonical_json(observation["document"]) != canonical_json(
            document.to_json()
        ) or body != canonical_json({key: result.get(key) for key in observation}):
            return None
        checksum = f"sha256:{sha256_hex(body)}"
        if checksum != evidence.content_hash or result.get("observation_checksum") != checksum:
            return None
        _utc_timestamp(observation["observed_at"])
        storage = observation["storage"]
        if not isinstance(storage, dict) or set(storage) != {
            "last_modified",
            "version_id",
            "etag",
            "size",
            "content_type",
        }:
            return None
        observed = BlobObservation(
            last_modified=_utc_timestamp(storage["last_modified"]),
            version_id=storage["version_id"],
            etag=storage["etag"],
            size=storage["size"],
            content_type=storage["content_type"],
        )
        if observed.size != document.size:
            return None
        key = (validate_source_object_key(observation["source_object_key"])
               if metadata["observation_version"] == "v2" else None)
        return ProjectDocumentObservation(project_id, document, observed, reference_checksum, key)
    except (ValueError, TypeError, FileStorageError):
        return None


def _observation_response(tool: ToolCall, reference: str) -> dict[str, Any] | None:
    """一覧の Evidence index を厳密に解決し、単一観測と同じ codec に渡す。"""

    result = tool.result_json
    if not isinstance(result, dict):
        return None
    if tool.capability_version == DOCUMENT_INSPECT_CAPABILITY:
        return result
    if (
        tool.capability_version != DOCUMENT_LIST_CAPABILITY
        or result.get("status") != "success"
        or result.get("provider") != "project"
        or result.get("scope") != "run_frozen_documents"
    ):
        return None
    entries, refs = result.get("entries"), result.get("evidence_refs")
    if (
        not isinstance(entries, list)
        or not 1 <= len(entries) <= 50
        or not isinstance(refs, list)
        or len(refs) != len(entries) + 1
        or any(not valid_observation_reference(item) for item in refs)
        or len(set(refs)) != len(refs)
        or reference == refs[0]
    ):
        return None
    selected = None
    for index, entry in enumerate(entries, start=1):
        if (
            not isinstance(entry, dict)
            or set(entry) - {"source_object_key"}
            != {
                "document",
                "storage",
                "observed_at",
                "content_verified",
                "observation_checksum",
                "matches_filter",
                "evidence_index",
            }
            or type(entry["evidence_index"]) is not int
            or entry["evidence_index"] != index
            or type(entry["matches_filter"]) is not bool
        ):
            return None
        if refs[index] == reference:
            selected = entry
    if selected is None:
        return None
    return {
        "status": "success",
        "provider": "project",
        "evidence_refs": [reference],
        **{
            key: value
            for key, value in selected.items()
            if key not in {"matches_filter", "evidence_index"}
        },
    }


def _utc_timestamp(value: object) -> datetime:
    """観測時に保存された aware UTC 時刻だけを受理し、日付や local 時刻を補わない。"""

    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Observation timestamp is invalid")
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(None):
        raise ValueError("Observation timestamp is invalid")
    return timestamp
