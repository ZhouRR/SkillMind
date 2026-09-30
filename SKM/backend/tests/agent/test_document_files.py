"""本文を転送しない一覧・提案準備と元 binding の権限境界を確認する。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from skillmind.agent.contract_store import ContractStore
from skillmind.agent.document_files import DocumentFilesProvider
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.artifacts.repository import ArtifactRepository
from skillmind.documents.library import (
    DocumentLibraryBindingRepository,
    FrozenDocumentLibraryBinding,
)
from tests.agent.test_document_provider import CONTRACTS, _context
from tests.documents.test_document_library_binding import target
from tests.documents.test_document_management import add_document, apply, change
from tests.documents.test_document_management import db as db
from tests.storage.test_object_effect import fixture as object_fixture


def provider_context(db, monkeypatch):
    """本物の目录 SQL と凍結 binding を使い、共有認可 port だけを置き換える。"""
    library = target()
    context = _context(db.rows["projects"]["id"])
    binding = FrozenDocumentLibraryBinding(
        context.project_id, context.run_id, uuid4(), "outputs", library
    )
    run = replace(
        context.run,
        resolved_sources={"outputs": binding.to_json()},
        permission_snapshot={"allowed_capabilities": ["document.write/v1", "document.files/v1"]},
    )
    context = replace(context, run=run)
    authorized = AsyncMock(
        return_value=SimpleNamespace(checksum=binding.to_json()["binding_checksum"])
    )
    monkeypatch.setattr(DocumentLibraryBindingRepository, "require", authorized)

    @asynccontextmanager
    async def sessions():
        """metadata 読み取り中だけ session を開く。"""
        with db.transaction() as port:
            yield port

    return DocumentFilesProvider(sessions, target=library), context, authorized


def validate(response):
    """Gateway の採番を補い、実応答を公開契約へ照合する。"""
    Draft202012Validator(
        ContractStore(CONTRACTS).load("tools/document.files/v1/response.schema.json")
    ).validate({**response, "evidence_refs": ["ev_fixture"]})


async def test_list_stat_and_prepare_use_metadata_only_and_exact_revision(db, monkeypatch):
    row = add_document(db)
    provider, context, authorized = provider_context(db, monkeypatch)
    base = {"library_key": "outputs", "purpose": "Organize documents"}
    result = await provider.execute(context, {**base, "action": "list"})
    validate(result.response)
    assert result.response["listing"]["total"] == 2
    stat = await provider.execute(context, {**base, "action": "stat", "path": "specs/source.md"})
    validate(stat.response)
    request = {
        **base,
        "action": "prepare",
        "path": "specs/source.md",
        "operation": "MOVE",
        "destination": "review/renamed.md",
        "expected_revision": stat.response["state"]["revision"],
    }
    prepared = await provider.execute(context, request)
    validate(prepared.response)
    assert prepared.response["proposal"]["precondition"]["revision"] == request["expected_revision"]
    with db.transaction() as port:
        assert (await port.get(type(row), row.id)).folder == "specs"
    await apply(db, "MOVE", [change(row, "ui", "changed.md")])
    with pytest.raises(ToolProviderError):
        await provider.execute(context, request)
    assert authorized.await_count == 7


async def test_pagination_detects_directory_changes(db, monkeypatch):
    add_document(db)
    provider, context, _ = provider_context(db, monkeypatch)
    args = {"library_key": "outputs", "purpose": "List", "action": "list", "limit": 1}
    result = await provider.execute(context, args)
    page = result.response["listing"]
    assert page["next_offset"] == 1
    add_document(db, "new.md")
    with pytest.raises(ToolProviderError):
        await provider.execute(
            context, {**args, "offset": 1, "expected_revision": page["revision"]}
        )


async def test_revocation_and_wrong_run_cannot_list_current_files(db, monkeypatch):
    add_document(db)
    provider, context, authorized = provider_context(db, monkeypatch)
    args = {"library_key": "outputs", "purpose": "List", "action": "list"}
    with pytest.raises(ToolProviderError, match="not authorized"):
        await provider.execute(replace(context, run_id=uuid4()), args)
    authorized.assert_not_awaited()
    authorized.side_effect = ValueError("Synthetic revoked binding")
    with pytest.raises(ToolProviderError):
        await provider.execute(context, args)


async def test_prepare_save_references_exact_run_artifact_without_body(db, monkeypatch):
    provider, context, _ = provider_context(db, monkeypatch)
    _, _, values = object_fixture()
    artifact = values["artifact"]
    lookup = AsyncMock(return_value=artifact)
    monkeypatch.setattr(ArtifactRepository, "get_content", lookup)
    result = await provider.execute(
        context,
        {
            "library_key": "outputs",
            "purpose": "Save",
            "action": "prepare",
            "operation": "CREATE",
            "path": "new/result.md",
            "artifact_ref": artifact.metadata.artifact_ref,
            "mime_type": "text/markdown",
        },
    )
    validate(result.response)
    value = result.response["proposal"]["changes"][0]["value"]
    assert value["content_hash"] == artifact.metadata.checksum
    assert value["size_bytes"] == len(artifact.content)
    assert set(value) == {"artifact_ref", "content_hash", "size_bytes", "mime_type"}
    lookup.assert_awaited_once_with(
        project_id=context.project_id,
        run_id=context.run_id,
        artifact_ref=artifact.metadata.artifact_ref,
    )


async def test_revocation_during_metadata_read_prevents_publication(db, monkeypatch):
    """途中で撤権された場合、取得済み metadata を Tool 結果として交付しない。"""
    add_document(db)
    provider, context, authorized = provider_context(db, monkeypatch)
    authorized.side_effect = [authorized.return_value, ValueError("Synthetic revocation")]
    with pytest.raises(ToolProviderError):
        await provider.execute(
            context, {"library_key": "outputs", "purpose": "List", "action": "list"}
        )
    assert authorized.await_count == 2
