"""Project 全体の追加参照、凍結入力、原 upload ID と撤権を実 Provider で検証する。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from skillmind.agent.contract_store import ContractStore
from skillmind.agent.document_listing import DocumentListProvider
from skillmind.agent.document_provider import DocumentProvider
from skillmind.agent.tool_catalog import document_read_tool_definition
from skillmind.agent.tool_gateway import ToolProviderError, ToolRegistry
from skillmind.documents.domain import DocumentNotFoundError
from skillmind.documents.source import ProjectDocumentObservation
from skillmind.storage.observation import BlobObservation
from tests.agent.test_document_provider import CONTRACTS, _context, _validate_response
from tests.agent.test_tool_gateway import MemoryAuditWriter
from tests.documents.fakes import document_content, document_snapshot


class LibrarySource:
    """現在目録と原 ID の blob を別々に保持する、ネットワーク不要の Project source。"""

    def __init__(self, project_id, contents):
        """目録の更新・撤権と取得前後の認可を観測可能にする。"""
        self.project_id = project_id
        self.contents = {item.document_id: item for item in contents}
        self.current_ids = set(self.contents)
        self.revoked = False
        self.revoke_after_fetch = False
        self.fetches = []
        self.authorizations = []

    async def authorize_reader(self, *, project_id, user_id):
        """現在の Project 読取が失われた場合は文書を取得させない。"""
        self.authorizations.append((project_id, user_id))
        if project_id != self.project_id or self.revoked:
            raise DocumentNotFoundError("Document is not accessible")

    async def list_documents(self, *, project_id):
        """回収済みを除く、今この時点の目録を返す。"""
        assert project_id == self.project_id
        return document_snapshot(
            project_id, [self.contents[key] for key in self.current_ids]
        ).documents

    async def find_document(self, *, project_id, document_id=None, folder=None, name=None):
        """URL を受けず、同 Project の現行 ID/path だけを解決する。"""
        return next(
            (
                item
                for item in await self.list_documents(project_id=project_id)
                if (
                    item.document_id == document_id
                    if document_id is not None
                    else item.folder == folder and item.name == name
                )
            ),
            None,
        )

    async def fetch(self, *, project_id, document_id):
        """旧版も ID で保持し、取得中の撤権を再現できる。"""
        assert project_id == self.project_id
        self.fetches.append(document_id)
        if self.revoke_after_fetch:
            self.revoked = True
        return self.contents.get(document_id)

    async def inspect(self, *, project_id, document_id):
        """全文読取を増やさず、各 page の metadata だけを観測する。"""
        content = self.contents[document_id]
        document = document_snapshot(project_id, [content]).documents[0]
        return ProjectDocumentObservation(
            project_id,
            document,
            BlobObservation(
                datetime(2026, 1, 1, tzinfo=UTC),
                "opaque",
                "original",
                content.size,
                content.mime,
            ),
            "sha256:" + "b" * 64,
        )

    async def fetch_observed(self, *, project_id, observed):
        """通常 source と同じ観測固定取得 port。"""
        return await self.fetch(project_id=project_id, document_id=observed.document.document_id)


def library_context(capability="document.read/v1"):
    """選択済み仕様は一件、追加参照の Project 権は別の凍結値として持つ。"""
    context = _context(uuid4())
    tool = replace(context.tool, capability=capability)
    run = replace(
        context.run,
        tools=(tool,),
        permission_snapshot={
            "allowed_capabilities": ["document.read/v1", "document.list/v1", "workspace.read/v1"],
            "project_document_read": {"version": "v1", "project_id": str(context.project_id)},
        },
    )
    return replace(context, tool=tool, run=run)


@pytest.mark.parametrize("selector", ["path", "document_id"])
async def test_new_project_upload_is_readable_by_original_receipt(selector):
    """開始後に増えたログも、入力選択の変更や別 Tool なしに原 hash で読む。"""
    context = library_context()
    uploaded = document_content(b"observed execution log\n", folder="results", name="new.log")
    source = LibrarySource(context.project_id, [document_content()])
    source.contents[uploaded.document_id] = uploaded
    source.current_ids.add(uploaded.document_id)
    arguments = {
        "purpose": "Read original uploaded log",
        "expected_hash": uploaded.checksum,
        selector: "results/new.log" if selector == "path" else str(uploaded.document_id),
    }
    result = await DocumentProvider(source).execute(context, arguments)
    assert result.response["content"] == "observed execution log\n"
    assert result.evidence[0].source_locator["document_id"] == str(uploaded.document_id)
    assert len(source.authorizations) == 2
    assert len(document_snapshot(context.project_id, [document_content()]).documents) == 1
    _validate_response(dict(result.response))


async def test_read_by_receipt_id_returns_complete_file_and_original_hash(tmp_path):
    """実 Gateway・原契約・監査を経て正文を local file として交付する。"""
    context = library_context()
    uploaded = document_content(b"long log\n" * 2000, folder="results", name="new.log")
    source = LibrarySource(context.project_id, [uploaded])
    root = tmp_path / str(context.run_id)
    for directory in [root, root / "workspace", root / "input", root / "output", root / "temp"]:
        directory.mkdir(exist_ok=True)
    workspace = replace(
        context.workspace,
        root=root,
        cwd=root / "workspace",
        input_dir=root / "input",
        output_dir=root / "output",
        temp_dir=root / "temp",
    )
    registry = ToolRegistry((document_read_tool_definition(ContractStore(CONTRACTS), source),))
    registered = registry.resolve(
        "document.read/v1", provider="project-documents", integration_id=None
    )
    runtime = registry.build_gateway_runtime(
        replace(context.run, workspace=workspace, tools=(registered,)),
        audit_writer=MemoryAuditWriter(),
    )
    arguments = {
        "document_id": str(uploaded.document_id),
        "expected_hash": uploaded.checksum,
        "response_mode": "file",
        "purpose": "Read uploaded log",
    }
    await runtime.mcp.on_tool_authorized(registered.sdk_name, arguments, "read-file", str(uuid4()))
    result = await runtime.gateway.invoke_mcp(registered.sdk_name, arguments)
    assert not result.get("is_error"), result
    response = json.loads(result["content"][0]["text"])
    assert "content" not in response
    assert (root / response["file"]["path"]).read_bytes() == uploaded.data
    assert response["file"]["content_hash"] == uploaded.checksum


async def test_historical_run_cannot_read_unselected_project_file():
    """新方針を旧 Run の凍結集合へ後付けしない。"""
    context = _context(uuid4())
    source = LibrarySource(context.project_id, [document_content(folder="extra", name="new.md")])
    with pytest.raises(ToolProviderError) as failed:
        await DocumentProvider(source).execute(context, {"path": "extra/new.md", "purpose": "read"})
    assert failed.value.code == "not_found"
    assert source.fetches == source.authorizations == []


@pytest.mark.parametrize("failure", ["scope", "revoked", "revoked_during_read", "hash", "trash"])
async def test_project_read_does_not_leak_foreign_or_unverified_content(failure):
    """別 Project・撤権・原 hash 不一致・現行目録からの削除は成功応答にしない。"""
    context = library_context()
    uploaded = document_content(b"current\n", folder="extra", name="new.md")
    source = LibrarySource(context.project_id, [uploaded])
    if failure == "scope":
        context.run.permission_snapshot["project_document_read"]["project_id"] = str(uuid4())
    elif failure == "revoked":
        source.revoked = True
    elif failure == "revoked_during_read":
        source.revoke_after_fetch = True
    elif failure == "trash":
        source.current_ids.clear()
    arguments = {"document_id": str(uploaded.document_id), "purpose": "read"}
    if failure == "hash":
        arguments["expected_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ToolProviderError):
        await DocumentProvider(source).execute(context, arguments)
    if failure != "revoked_during_read":
        assert source.fetches == []


async def test_project_grant_keeps_selected_original_when_current_path_is_replaced():
    """広い参照読取でも、同名の新しい文書を元仕様の代わりにしない。"""
    context = library_context()
    original = document_content()
    replacement = document_content(b"new version\n", document_id=uuid4())
    source = LibrarySource(context.project_id, [original, replacement])
    source.current_ids.remove(original.document_id)
    provider = DocumentProvider(source)
    frozen = await provider.execute(context, {"path": "specs/overview.md", "purpose": "read input"})
    current = await provider.execute(
        context,
        {"document_id": str(replacement.document_id), "purpose": "read additional reference"},
    )
    assert frozen.response["content"] == original.data.decode()
    assert current.response["content"] == replacement.data.decode()


async def test_project_listing_includes_unselected_files_and_detects_catalog_change():
    """Project 全体の分頁を実 response 契約で照合し、cursor は元目録へ固定する。"""
    from jsonschema import Draft202012Validator, FormatChecker

    context = library_context("document.list/v1")
    source = LibrarySource(
        context.project_id, [document_content(name="a.md"), document_content(name="b.md")]
    )
    provider = DocumentListProvider(source)
    first = await provider.execute(context, {"directory": "", "limit": 1, "purpose": "list"})
    response = dict(first.response)
    response["evidence_refs"] = ["ev_listing", "ev_observation"]
    Draft202012Validator(
        ContractStore(CONTRACTS).load("tools/document.list/v1/response.schema.json"),
        format_checker=FormatChecker(),
    ).validate(response)
    assert response["scope"] == "project_documents"
    assert response["candidate_count"] == 2
    second = await provider.execute(
        context, {"directory": "", "limit": 1, "purpose": "list", "cursor": response["next_cursor"]}
    )
    assert second.response["entries"][0]["document"]["name"] == "b.md"
    added = document_content(name="c.md")
    source.contents[added.document_id] = added
    source.current_ids.add(added.document_id)
    with pytest.raises(ToolProviderError) as failed:
        await provider.execute(
            context,
            {"directory": "", "limit": 1, "purpose": "list", "cursor": response["next_cursor"]},
        )
    assert failed.value.code == "invalid_request"
