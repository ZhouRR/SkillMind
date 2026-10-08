"""現在の文書一覧を補わず、原要求と凍結した参照 ID だけを検証する。"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from skillmind.db.models import Run, TaskSchedule, TaskScheduleOccurrence
from skillmind.documents.domain import DocumentReferencesUnavailableError
from skillmind.documents.library import (
    DOCUMENT_LIBRARY_SELECTION,
    is_document_library_source,
    parse_document_library_source,
)
from skillmind.documents.snapshot import (
    DOCUMENT_PROVIDER,
    DocumentSnapshot,
    is_document_source,
    parse_document_selection,
    parse_document_snapshot,
    snapshot_documents,
)
from skillmind.runs.creation_replay import stored_creation_intent
from skillmind.schedules.repository_occurrences import occurrence_snapshot

# Blueprint の resourceKind のうち Integration を表す種類。Tool 名・version は参照判定に使わない。
_INTEGRATION_RESOURCE_KINDS = frozenset({"issue", "repository", "file", "knowledge", "other"})


def _require_integration_source(source: Mapping[str, object], selection: str | None) -> None:
    """凍結した種類と原選択/default の接続 identity で、Project 文書以外の資源と確認する。"""

    kind, provider = source.get("resource_kind"), source.get("provider")
    candidate, identity = source.get("candidate_key"), source.get("integration_id")
    if (
        not isinstance(kind, str) or kind not in _INTEGRATION_RESOURCE_KINDS
        or not isinstance(provider, str) or not provider
        or not isinstance(candidate, str) or not candidate.startswith("integration:")
        or not isinstance(identity, str)
    ):
        raise ValueError("Stored Integration resource type cannot be verified")
    integration_id = UUID(identity)
    if integration_id.int == 0 or UUID(candidate.removeprefix("integration:")) != integration_id:
        raise ValueError("Stored Integration resource identity cannot be verified")
    if selection is not None:
        if not selection.startswith("integration:") or UUID(
            selection.removeprefix("integration:")
        ) != integration_id:
            raise ValueError("Integration resource does not match the original choice")
    else:
        # 省略された選択は保存済み default binding で証明し、現在の接続設定から補わない。
        binding_id = source.get("source_binding_id")
        if not isinstance(binding_id, str) or UUID(binding_id).int == 0:
            raise ValueError("Original Integration default binding cannot be verified")


def choice_document_ids(sources: object) -> frozenset[UUID]:
    """未展開の ALL は固定参照にせず、旧/未知の provider 選択は無参照と推測しない。"""

    if not isinstance(sources, dict):
        raise ValueError("Stored source choices must be an object")
    result: set[UUID] = set()
    for key, token in sources.items():
        if not isinstance(key, str) or not key or not isinstance(token, str) or not token:
            raise ValueError("Stored source choice is invalid")
        if token == DOCUMENT_LIBRARY_SELECTION:
            # 保存先の明示選択は既存入力の参照を持たない。prefix の類似だけでは採用しない。
            continue
        if is_document_source(token):
            result.update(parse_document_selection(token).document_ids)
        elif token.startswith("integration:"):
            UUID(token.removeprefix("integration:"))
        else:
            # 旧 provider 名だけでは資源の種類を証明できない。現在の catalog で補填しない。
            raise ValueError("Legacy source choice cannot be verified")
    return frozenset(result)


def run_document_ids(run: Run) -> frozenset[UUID]:
    """全状態の Run について、原意図と各文書 snapshot の対応・集合整合性を検証する。"""

    original = stored_creation_intent(run)
    frozen: list[DocumentSnapshot] = []
    document_keys = {key for key, token in original.sources.items() if is_document_source(token)}
    library_keys = {
        key for key, token in original.sources.items() if token == DOCUMENT_LIBRARY_SELECTION
    }
    actual_library_keys: set[str] = set()
    actual_keys: set[str] = set()
    for key, source in run.selected_sources_json.items():
        if not isinstance(key, str) or not key or not isinstance(source, Mapping):
            raise ValueError("Stored Run source is not verifiable")
        if is_document_library_source(source):
            parse_document_library_source(
                source, project_id=run.project_id, run_id=run.id, requirement_key=key
            )
            if key not in library_keys:
                raise ValueError("Document library does not match the original choice")
            actual_library_keys.add(key)
            continue
        if not is_document_source(source) and not is_document_source(source.get("candidate_key")):
            _require_integration_source(source, original.sources.get(key))
            continue
        actual_keys.add(key)
        value = source.get("document_snapshot")
        if (
            source.get("provider") != DOCUMENT_PROVIDER
            or source.get("resource_kind") != "document"
            or not isinstance(value, Mapping)
            or key not in document_keys
        ):
            raise ValueError("Stored document source is not verifiable")
        selected = parse_document_selection(original.sources[key])
        candidate_token = source.get("candidate_key")
        if not isinstance(candidate_token, str):
            raise ValueError("Original document candidate is missing")
        candidate = parse_document_selection(candidate_token)
        snapshot = parse_document_snapshot(value, project_id=run.project_id, requirement_key=key)
        if (
            selected != candidate
            or selected.mode != snapshot.selection_mode
            or (
                selected.mode != "ALL"
                and selected.document_ids != tuple(item.document_id for item in snapshot.documents)
            )
        ):
            raise ValueError("Frozen documents do not match their original selection")
        frozen.append(snapshot)
    if actual_keys != document_keys or actual_library_keys != library_keys:
        raise ValueError("Original document selection has no trusted snapshot")
    return frozenset(item.document_id for item in snapshot_documents(frozen))


def occurrence_document_ids(
    row: TaskScheduleOccurrence, schedule: TaskSchedule | None
) -> frozenset[UUID]:
    """保持済み発火は終結後も原 ID を保護し、親の編集・暂停を解除条件にしない。"""

    snapshot = occurrence_snapshot(row)
    if (
        schedule is None
        or row.schedule_id != schedule.id
        or row.project_id != schedule.project_id
        or row.created_by != schedule.created_by
        or row.skill_version_id != schedule.skill_version_id
        or snapshot.intent.task_key != schedule.task_key
        or row.status not in {"PENDING", "SETTLED"}
    ):
        raise ValueError("Occurrence ownership cannot be verified")
    return choice_document_ids(snapshot.intent.sources)


def references_unavailable() -> DocumentReferencesUnavailableError:
    """内部 snapshot/locator を利用者向け例外に含めない。"""

    return DocumentReferencesUnavailableError("Document references could not be verified")
