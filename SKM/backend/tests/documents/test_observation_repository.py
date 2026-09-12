"""実 SQLite JOIN と本番 codec で観測 Evidence の来歴を検証する。PG lock の証明ではない。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from skillmind.agent.document_inspection import DocumentInspectProvider
from skillmind.agent.document_listing import DocumentListProvider
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import Evidence, Run, ToolCall
from skillmind.documents.observation_repository import PostgresDocumentObservationLookup
from skillmind.documents.source import ProjectDocumentObservation
from skillmind.storage.observation import BlobObservation
from sqlalchemy import Column, MetaData, Table, create_engine
from sqlalchemy.orm import Session
from tests.agent.test_document_provider import _context
from tests.documents.fakes import document_content, document_snapshot


class _Session:
    """実 ORM session の検索だけを await/context port に接続する。"""

    def __init__(self, session):
        """外側 fixture が transaction と寿命を所有する。"""
        self.session = session

    async def __aenter__(self):
        """本文取得前に終了を記録可能な読取 context を返す。"""
        return self

    async def __aexit__(self, *args):
        """元の例外は抑制しない。"""

    async def execute(self, statement):
        """条件式を mock で解釈せず、実 SQL JOIN を実行する。"""
        return self.session.execute(statement)


@pytest.fixture
def saved():
    """元 Run・成功 ToolCall・metadata Evidence を独立した実テーブルに保存する。"""

    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    for model in (Run, ToolCall, Evidence):
        Table(
            model.__tablename__,
            metadata,
            *[
                Column(column.name, column.type, primary_key=column.primary_key)
                for column in model.__table__.columns
            ],
        )
    metadata.create_all(engine)
    with Session(engine) as session:
        project_id, run_id, tool_id = uuid4(), uuid4(), uuid4()
        document = document_snapshot(
            project_id, [document_content(b"abc", name="cases.xlsx")]
        ).documents[0]
        storage = BlobObservation(
            datetime(2026, 9, 11, tzinfo=UTC), "opaque", "v1", 3, document.mime
        )
        observed = ProjectDocumentObservation(project_id, document, storage, "sha256:" + "b" * 64)
        observation = {
            "document": document.to_json(),
            "content_verified": False,
            "observed_at": "2026-09-11T01:00:00+00:00",
            "storage": {
                key: value for key, value in storage.to_json().items() if key != "consistency"
            },
        }
        checksum = "sha256:" + sha256_hex(canonical_json(observation))
        tool = ToolCall(
            id=tool_id,
            run_id=run_id,
            run_attempt_id=uuid4(),
            agent_session_id=uuid4(),
            sdk_tool_use_id="inspect-original",
            request_fingerprint="a" * 64,
            tool_name="mcp__skillmind__document_inspect",
            capability_version="document.inspect/v1",
            provider="project-documents",
            integration_id=None,
            arguments_summary={},
            status="SUCCEEDED",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            result_json={
                "status": "success",
                "provider": "project",
                **deepcopy(observation),
                "observation_checksum": checksum,
                "evidence_refs": ["ev_original"],
                "warnings": [],
            },
            error_json=None,
        )
        evidence = Evidence(
            id=uuid4(),
            evidence_ref="ev_original",
            run_id=run_id,
            tool_call_id=tool_id,
            evidence_type="document",
            source_uri=f"document://projects/{project_id}/{document.document_id}/metadata",
            source_locator={"document_id": str(document.document_id), "path": document.path},
            content_hash=checksum,
            created_at=datetime.now(UTC),
            metadata_json={
                "observation_version": "v1",
                "observation": deepcopy(observation),
                "source_reference_checksum": observed.reference_checksum,
            },
        )
        session.execute(metadata.tables["runs"].insert().values(id=run_id, project_id=project_id))
        session.add_all([tool, evidence])
        session.flush()
        lookup = PostgresDocumentObservationLookup(lambda: _Session(session))
        yield SimpleNamespace(
            session=session,
            lookup=lookup,
            project_id=project_id,
            run_id=run_id,
            document=document,
            observed=observed,
            evidence=evidence,
            tool=tool,
        )
    engine.dispose()


async def _load(saved, **changes):
    """期待する原 scope を既定値として本番 lookup を呼び出す。"""
    args = {
        "project_id": saved.project_id,
        "run_id": saved.run_id,
        "document": saved.document,
        "reference": "ev_original",
        **changes,
    }
    return await saved.lookup.load(**args)


async def test_successful_original_inspection_can_be_restored_across_attempts(saved):
    """元観測の Attempt が現在と異なっても、同 Run の確定事実として復元する。"""
    restored = await _load(saved)
    assert restored == saved.observed
    assert saved.evidence.metadata_json["observation_version"] == "v1"
    assert "source_object_key" not in saved.evidence.metadata_json["observation"]
    assert restored.source_object_key is None


async def _save_provider_observation(saved, capability, source_object_key):
    """本番 Provider の観測を SQL に保存し、本文取得を伴わない往復に利用する。"""
    observed = replace(saved.observed, source_object_key=source_object_key)
    content = document_content(b"abc", name="cases.xlsx", document_id=saved.document.document_id)
    context = _context(saved.project_id, content=content)
    tool = replace(context.tool, capability=capability)
    run = replace(
        context.run,
        run_id=saved.run_id,
        tools=(tool,),
        permission_snapshot={"allowed_capabilities": [capability]},
    )
    context = replace(context, run_id=saved.run_id, run=run, tool=tool)
    source = SimpleNamespace(
        inspect=AsyncMock(return_value=observed),
        fetch_observed=AsyncMock(),
        fetch=AsyncMock(),
    )
    if capability == "document.inspect/v1":
        result = await DocumentInspectProvider(source).execute(
            context, {"path": "specs/cases.xlsx"}
        )
    else:
        result = await DocumentListProvider(source).execute(context, {"directory": "specs"})
    draft = result.evidence[-1]
    saved.tool.capability_version = capability
    saved.evidence.metadata_json = dict(draft.metadata)
    saved.evidence.source_locator = dict(draft.source_locator)
    saved.evidence.source_uri = draft.source_uri
    saved.evidence.content_hash = draft.content_hash
    saved.tool.result_json = {
        **result.response,
        "evidence_refs": (
            [saved.evidence.evidence_ref]
            if capability == "document.inspect/v1"
            else ["ev_page", saved.evidence.evidence_ref]
        ),
    }
    saved.session.flush()
    source.fetch.assert_not_called()
    source.fetch_observed.assert_not_called()
    return observed


def _response_observation(saved, result):
    """単一応答と一覧内の原 entry の観測 payload を選ぶ。"""
    return result["entries"][0] if saved.tool.capability_version == "document.list/v1" else result


def _save_changed_observation(saved, metadata, result, *, rehash=False):
    """改変した記録を保存し、必要時だけ実 codec で両方の摘要を一致させる。"""
    if rehash:
        checksum = "sha256:" + sha256_hex(canonical_json(metadata["observation"]))
        saved.evidence.content_hash = checksum
        _response_observation(saved, result)["observation_checksum"] = checksum
    saved.evidence.metadata_json = metadata
    saved.tool.result_json = result
    saved.session.flush()


@pytest.mark.parametrize("capability", ["document.inspect/v1", "document.list/v1"])
@pytest.mark.parametrize("source_object_key", [None, "projects/fixture/documents/original.xlsx"])
async def test_actual_inspection_provider_evidence_round_trips_through_sql_lookup(
    saved, capability, source_object_key
):
    """本番 Provider と SQL lookup で v1 の欠省と v2 の物理 key 保存を往復検証する。"""
    observed = await _save_provider_observation(saved, capability, source_object_key)
    metadata = saved.evidence.metadata_json
    assert metadata["observation_version"] == ("v1" if source_object_key is None else "v2")
    assert ("source_object_key" in metadata["observation"]) is (source_object_key is not None)
    assert await _load(saved) == observed
    assert metadata["observation"].get("source_object_key") == source_object_key


@pytest.mark.parametrize("capability", ["document.inspect/v1", "document.list/v1"])
@pytest.mark.parametrize("target", ["evidence", "response", "both"])
async def test_changed_source_object_key_cannot_reuse_original_observation_hash(
    saved, capability, target
):
    """応答との一致だけでなく、物理 key を含む原 hash の一致を要求する。"""
    await _save_provider_observation(saved, capability, "projects/fixture/documents/original.xlsx")
    metadata = deepcopy(saved.evidence.metadata_json)
    result = deepcopy(saved.tool.result_json)
    if target in {"evidence", "both"}:
        metadata["observation"]["source_object_key"] = "projects/fixture/documents/changed.xlsx"
    if target in {"response", "both"}:
        _response_observation(saved, result)["source_object_key"] = (
            "projects/fixture/documents/changed.xlsx"
        )
    _save_changed_observation(saved, metadata, result)
    assert await _load(saved) is None


@pytest.mark.parametrize("capability", ["document.inspect/v1", "document.list/v1"])
@pytest.mark.parametrize(
    "key",
    [
        None,
        1,
        "",
        "/absolute.xlsx",
        "../outside.xlsx",
        "documents/./original.xlsx",
        "documents//original.xlsx",
        " documents/original.xlsx",
        "documents/original.xlsx ",
        "documents\\original.xlsx",
        "documents/original\n.xlsx",
    ],
)
async def test_noncanonical_source_object_key_is_rejected_even_with_recomputed_hash(
    saved, capability, key
):
    """再計算済み摘要でも不正 key を正規化・推定せず拒否する。"""
    await _save_provider_observation(saved, capability, "projects/fixture/documents/original.xlsx")
    metadata = deepcopy(saved.evidence.metadata_json)
    result = deepcopy(saved.tool.result_json)
    metadata["observation"]["source_object_key"] = key
    _response_observation(saved, result)["source_object_key"] = key
    _save_changed_observation(saved, metadata, result, rehash=True)
    assert await _load(saved) is None


@pytest.mark.parametrize("capability", ["document.inspect/v1", "document.list/v1"])
@pytest.mark.parametrize("target", ["observation", "response", "both"])
async def test_v1_observation_cannot_carry_source_object_key(saved, capability, target):
    """旧版へ物理 key を混入させても新版と誤認せず、成功応答だけの追加も拒否する。"""
    await _save_provider_observation(saved, capability, None)
    metadata = deepcopy(saved.evidence.metadata_json)
    result = deepcopy(saved.tool.result_json)
    key = "projects/fixture/documents/injected.xlsx"
    if target in {"observation", "both"}:
        metadata["observation"]["source_object_key"] = key
    if target in {"response", "both"}:
        _response_observation(saved, result)["source_object_key"] = key
    _save_changed_observation(saved, metadata, result, rehash=True)
    assert await _load(saved) is None


@pytest.mark.parametrize("capability", ["document.inspect/v1", "document.list/v1"])
@pytest.mark.parametrize(
    "case", ["downgraded_version", "unknown_version", "missing_key", "extra_key"]
)
async def test_v2_observation_requires_exact_version_and_field_set(saved, capability, case):
    """摘要と両記録が一致していても版や必要 field の不一致を受理しない。"""
    await _save_provider_observation(saved, capability, "projects/fixture/documents/original.xlsx")
    metadata = deepcopy(saved.evidence.metadata_json)
    result = deepcopy(saved.tool.result_json)
    response = _response_observation(saved, result)
    if case == "downgraded_version":
        metadata["observation_version"] = "v1"
    elif case == "unknown_version":
        metadata["observation_version"] = "v3"
    elif case == "missing_key":
        del metadata["observation"]["source_object_key"]
        del response["source_object_key"]
    else:
        metadata["observation"]["extra"] = "unrecognized"
        response["extra"] = "unrecognized"
    _save_changed_observation(saved, metadata, result, rehash=True)
    assert await _load(saved) is None


@pytest.mark.parametrize("case", ["project", "run", "reference", "invalid_reference", "document"])
async def test_other_scope_or_document_cannot_reuse_observation(saved, case):
    """同じ文字列・別 scope や別文書の参照を storage I/O の authority にしない。"""
    if case in {"project", "run"}:
        changes = {case + "_id": uuid4()}
    elif case == "document":
        changes = {
            "document": document_snapshot(saved.project_id, [document_content(b"abc")]).documents[0]
        }
    else:
        changes = {"reference": "ev_missing" if case == "reference" else "not an evidence ref"}
    assert await _load(saved, **changes) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "RUNNING"),
        ("status", "FAILED"),
        ("capability_version", "document.convert/v1"),
        ("provider", "other"),
        ("integration_id", uuid4()),
        ("run_id", uuid4()),
        ("error_json", {"code": "unavailable"}),
        ("result_json", None),
    ],
)
async def test_only_successful_original_metadata_tool_is_accepted(saved, field, value):
    """成功していない処理・別 Provider/Run・失敗混在は有効な観測でない。"""
    setattr(saved.tool, field, value)
    saved.session.flush()
    assert await _load(saved) is None


@pytest.mark.parametrize(
    "case",
    [
        "hash",
        "result_hash",
        "result_body",
        "refs",
        "verified",
        "version",
        "locator",
        "uri",
        "reference_checksum",
        "timestamp",
        "storage_size",
        "missing_storage",
        "tool_link",
        "artifact",
    ],
)
async def test_partial_or_changed_evidence_is_not_completed_from_current_storage(saved, case):
    """不完全な記録に現在値を補わず、成功応答と Evidence の整合を要求する。"""
    metadata = deepcopy(saved.evidence.metadata_json)
    result = deepcopy(saved.tool.result_json)
    if case == "hash":
        saved.evidence.content_hash = "sha256:" + "c" * 64
    elif case == "result_hash":
        result["observation_checksum"] = "sha256:" + "c" * 64
    elif case == "result_body":
        result["storage"]["etag"] = "changed"
    elif case == "refs":
        result["evidence_refs"] = ["ev_other"]
    elif case == "verified":
        metadata["observation"]["content_verified"] = True
    elif case == "version":
        metadata["observation_version"] = "v2"
    elif case == "locator":
        saved.evidence.source_locator = {"path": "different"}
    elif case == "uri":
        saved.evidence.source_uri = "file:///different"
    elif case == "reference_checksum":
        metadata["source_reference_checksum"] = "invalid"
    elif case == "timestamp":
        metadata["observation"]["observed_at"] = "2026-09-11"
    elif case == "storage_size":
        metadata["observation"]["storage"]["size"] = 4
    elif case == "missing_storage":
        del metadata["observation"]["storage"]
    elif case == "tool_link":
        saved.evidence.tool_call_id = uuid4()
    elif case == "artifact":
        saved.evidence.artifact_ref = "art_fake"
    saved.evidence.metadata_json = metadata
    saved.tool.result_json = result
    saved.session.flush()
    assert await _load(saved) is None


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "index",
        "bool_index",
        "summary",
        "duplicate_ref",
        "wrong_ref",
        "missing_entry",
        "extra_entry",
        "body",
        "scope",
        "status",
        "filter_type",
    ],
)
async def test_listing_reference_resolves_only_its_own_entry(saved, case):
    """一覧要約・他行・不正 index を文書観測へ昇格させず、原 entry を照合する。"""
    original = deepcopy(saved.tool.result_json)
    entry = {
        key: value
        for key, value in original.items()
        if key not in {"status", "provider", "warnings", "evidence_refs"}
    }
    entry.update(matches_filter=False, evidence_index=1)
    response = {
        "status": "success",
        "provider": "project",
        "scope": "run_frozen_documents",
        "entries": [entry],
        "evidence_refs": ["ev_page", "ev_original"],
    }
    if case == "index":
        entry["evidence_index"] = 0
    elif case == "bool_index":
        entry["evidence_index"] = True
    elif case == "summary":
        response["evidence_refs"] = ["ev_original", "ev_other"]
    elif case == "duplicate_ref":
        response["evidence_refs"] = ["ev_original", "ev_original"]
    elif case == "wrong_ref":
        response["evidence_refs"] = ["ev_page", "ev_other"]
    elif case == "missing_entry":
        response["entries"] = []
    elif case == "extra_entry":
        response["entries"].append(deepcopy(entry))
    elif case == "body":
        entry["storage"]["etag"] = "changed"
    elif case == "scope":
        response["scope"] = "live_bucket"
    elif case == "status":
        response["status"] = "error"
    elif case == "filter_type":
        entry["matches_filter"] = 0
    saved.tool.capability_version = "document.list/v1"
    saved.tool.result_json = response
    saved.session.flush()
    assert await _load(saved) == (saved.observed if case == "valid" else None)
