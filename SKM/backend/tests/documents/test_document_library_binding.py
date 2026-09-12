"""Project 文書庫の namespace/prefix を、外部 Integration なしで凍結・再検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from skillmind.documents.library import (
    DOCUMENT_WRITE_CAPABILITY,
    DocumentLibraryBindingRepository,
    DocumentLibraryTarget,
    FrozenDocumentLibraryBinding,
    document_library_scope,
    parse_document_library_source,
)
from skillmind.storage.namespace import make_s3_namespace


def target():
    """実接続を持たない namespace descriptor だけを用意する。"""
    return DocumentLibraryTarget(
        make_s3_namespace(
            namespace_id=uuid4(), endpoint="https://storage.example.test", bucket="fixture-library"
        ),
        "fixture-library",
    )


def test_library_reference_is_stable_across_runs_and_slots_without_rewriting_snapshot():
    """同じ庫の登録 ID は Run/slot を越えて一致し、原 snapshot/hash を変更しない。"""
    library, project_id = target(), uuid4()
    original = library.reference(project_id)
    assert UUID(original["document_library_id"]).version == 5
    assert set(original) == {"document_library_id", "project_id", "bucket"}
    for slot in ("outputs", "backup"):
        snapshot = FrozenDocumentLibraryBinding(project_id, uuid4(), uuid4(), slot, library)
        stored = snapshot.to_json()
        parsed = parse_document_library_source(stored, project_id=project_id, requirement_key=slot)
        assert parsed.target.reference(project_id) == original
        assert parsed.to_json() == stored
    assert library.reference(uuid4())["document_library_id"] != original["document_library_id"]
    assert target().reference(project_id)["document_library_id"] != original["document_library_id"]
    other_bucket = DocumentLibraryTarget(library.namespace, "fixture-other-library")
    assert other_bucket.reference(project_id)["document_library_id"] != original[
        "document_library_id"
    ]


def test_persisted_business_library_id_keeps_its_v1_identity():
    """既に業務 DB に保存された ID を実装変更で置き換えないため、固定 vector を守る。"""
    library = DocumentLibraryTarget(
        make_s3_namespace(
            namespace_id=UUID("00000000-0000-4000-8000-000000000624"),
            endpoint="https://storage.example.test", bucket="fixture-documents",
        ),
        "fixture-documents",
    )
    assert library.reference(UUID("00000000-0000-4000-8000-000000000623")) == {
        "document_library_id": "b43aa5c0-7b69-5b58-a4e2-174f34e885e6",
        "project_id": "00000000-0000-4000-8000-000000000623",
        "bucket": "fixture-documents",
    }


async def binding():
    """transaction port を模し、共有 checksum を使った実 binding 作成を通す。"""
    session = MagicMock()
    session.flush = AsyncMock()
    session.scalar = AsyncMock()
    repository = DocumentLibraryBindingRepository(session, target=target())
    identity = dict(project_id=uuid4(), run_id=uuid4(), requirement_key="review_outputs")
    row = await repository.freeze(**identity, actor_id=uuid4())
    session.scalar.return_value = row
    return repository, session, row, identity


async def test_freeze_and_require_use_real_resource_binding_without_fake_integration():
    """元 Project/Run と scope を固定し、placeholder Integration/Secret を作成しない。"""
    repository, session, row, identity = await binding()
    assert row.integration_id is None and row.source_binding_id is None
    assert row.capability_version == DOCUMENT_WRITE_CAPABILITY
    assert row.scope_json["key_prefix"] == (
        f"projects/{identity['project_id']}/documents/effects-v2/"
    )
    assert await repository.require(**identity, binding_id=row.id) is row
    session.add.assert_called_once_with(row)
    session.flush.assert_awaited_once()
    statement = session.scalar.call_args.args[0]
    assert statement.get_execution_options()["populate_existing"] is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("integration_id", uuid4()),
        ("project_id", uuid4()),
        ("run_id", uuid4()),
        ("scope_level", "TASK"),
        ("scope_key", "different"),
        ("requirement_key", "different"),
        ("resource_kind", "repository"),
        ("provider", "postgres"),
        ("capability_version", "database.write/v1"),
        ("revision", "1"),
        ("disabled_at", datetime.now(UTC)),
        ("source_binding_id", uuid4()),
        ("checksum", "sha256:" + "a" * 64),
    ],
)
async def test_changed_binding_fails_before_use(field, value):
    """一つでも違う元束縛を現在の配置で補正して採用しない。"""
    repository, _session, row, identity = await binding()
    setattr(row, field, value)
    with pytest.raises(ValueError, match="changed"):
        repository.validate(row, **identity)


async def test_namespace_switch_or_tampered_prefix_cannot_retarget_frozen_run():
    """同じ bucket 名でも保存先世代が変われば古い Run の成果を新配置へ移さない。"""
    repository, session, row, identity = await binding()
    with pytest.raises(ValueError, match="changed"):
        DocumentLibraryBindingRepository(session, target=target()).validate(row, **identity)
    row.scope_json["key_prefix"] = "other-project/"
    with pytest.raises(ValueError, match="changed"):
        repository.validate(row, **identity)


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": "ignored"},
        {"project_id": str(UUID(int=0))},
        {"namespace_id": str(UUID(int=0))},
        {"descriptor_checksum": "invalid"},
        {"key_prefix": "projects/other/"},
        {"bucket": "../another"},
        {"bucket": False},
        {"namespace_id": None},
    ],
)
def test_scope_parser_requires_exact_canonical_descriptor(changes):
    """未知項目・nil・型違い・越境 prefix は一致した hash の前提にならない。"""
    original = target()
    project_id = uuid4()
    scope = original.scope(project_id)
    assert document_library_scope(scope) == (project_id, original)
    with pytest.raises(ValueError):
        document_library_scope({**scope, **changes})
