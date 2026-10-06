"""同 Run の原観測、固定接続、資格、file/hash を実 HTTP client で検証する。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from skillmind.agent.http_source import HttpResourceSource
from skillmind.agent.mcp_download import McpDownloadProvider, observed_path
from skillmind.agent.resource_files import require_file_access
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.core.hashing import canonical_json, sha256_hex
from tests.agent.test_mcp_provider import database_resource as database_resource
from tests.agent.test_mcp_provider import resource as resource
from tests.agent.test_workspace_image import image_bytes, image_run
from tests.agent.test_workspace_provider import _context


@pytest.fixture
def harness(tmp_path, resource):
    """DB だけを固定行で代替し、観測復元・HTTP 境界・安全な書込は実装を動かす。"""
    scope = {"resource_uris": [], "tool_names": ["observe"]}
    bound = replace(resource, scope=scope, integration=replace(
        resource.integration,
        config={**resource.integration.config, "tool_permissions": {"observe": "read"}},
    ))
    _, run = image_run(tmp_path)
    context = replace(
        _context(tmp_path, "mcp.download/v1"),
        run=replace(run, permission_snapshot={"allowed_capabilities": ["workspace.read/v1"]}),
        tool_call_id=uuid4(),
    )
    context = replace(
        context,
        tool=replace(
            context.tool,
            integration_id=bound.integration.integration_id,
            binding_id=uuid4(),
            provider="mcp",
        ),
    )
    data = image_bytes()
    original = {
        "status": "success",
        "provider": "mcp",
        "name": "observe",
        "result": {"screenshot": {"downloadPath": "/api/images/fixture.png"}},
    }
    evidence = SimpleNamespace(
        evidence_type="resource",
        run_id=context.run_id,
        metadata_json={"binding_checksum": bound.checksum},
        source_locator={"tool_name": "observe"},
        content_hash="sha256:" + sha256_hex(canonical_json(original)),
    )
    tool = SimpleNamespace(
        id=uuid4(),
        run_id=context.run_id,
        integration_id=context.tool.integration_id,
        error_json=None,
        result_json={**original, "evidence_refs": ["ev_original"]},
    )
    session = AsyncMock()
    row = Mock()
    row.one_or_none.return_value = (evidence, tool)
    session.execute.return_value = row
    calls = []

    def handle(request):
        """秘密は固定 origin の Authorization だけに使い、応答や log へ転写しない。"""
        calls.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-fixture-key"
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})

    sessions = Mock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=session)))
    provider = McpDownloadProvider(
        sessions,
        source=HttpResourceSource(httpx.MockTransport(handle)),
        secret_resolver=Mock(),
    )
    provider._binding._bound = AsyncMock(return_value=(bound, "synthetic-fixture-key"))
    return SimpleNamespace(
        provider=provider,
        context=context,
        bound=bound,
        original=original,
        evidence=evidence,
        tool=tool,
        calls=calls,
        data=data,
        session=session,
        arguments={
            "source_evidence_ref": "ev_original",
            "pointer": "/result/screenshot/downloadPath",
        },
    )


@pytest.mark.parametrize("file_response", [False, True])
async def test_download_preserves_original_bytes_and_both_mcp_response_modes(
    harness, file_response
):
    """inline/file の両観測から参照を復元し、同 origin へ GET を一度だけ送る。"""
    h = harness
    if file_response:
        path = f"workspace/resources/{h.tool.id}/response.json"
        local = h.context.workspace.root / path
        local.parent.mkdir(parents=True)
        raw = canonical_json(h.original).encode()
        local.write_bytes(raw)
        h.tool.result_json = {
            "file": {"path": path, "content_hash": h.evidence.content_hash},
            "evidence_refs": ["ev_original"],
        }
    result = await h.provider.execute(h.context, h.arguments)
    assert len(h.calls) == 1
    assert str(h.calls[0].url) == "https://mcp.example.test/api/images/fixture.png"
    file = result.response["file"]
    assert (h.context.workspace.root / file["path"]).read_bytes() == h.data
    assert file["content_hash"] == "sha256:" + sha256_hex(h.data)
    assert h.provider._binding._bound.await_count == 3
    assert "synthetic-fixture-key" not in str(result)


@pytest.mark.parametrize(
    "damage", ["run", "integration", "checksum", "scope", "body", "missing", "origin"]
)
async def test_foreign_or_changed_observation_never_contacts_http(harness, damage):
    """他 Run/接続、範囲、改変・欠落・別 origin の参照を資格送信前に拒否する。"""
    h = harness
    if damage == "run":
        h.evidence.run_id = uuid4()
    elif damage == "integration":
        h.tool.integration_id = uuid4()
    elif damage == "checksum":
        h.evidence.metadata_json["binding_checksum"] = "sha256:" + "0" * 64
    elif damage == "scope":
        h.evidence.source_locator["tool_name"] = "not-authorized"
    elif damage == "body":
        h.tool.result_json["name"] = "modified"
    elif damage == "missing":
        h.session.execute.return_value.one_or_none.return_value = None
    else:
        h.original["result"]["screenshot"]["downloadPath"] = "https://other.example.test/image.png"
        h.evidence.content_hash = "sha256:" + sha256_hex(canonical_json(h.original))
        h.tool.result_json = {**h.original, "evidence_refs": ["ev_original"]}
    with pytest.raises(ToolProviderError):
        await h.provider.execute(h.context, h.arguments)
    assert not h.calls


async def test_revocation_after_download_keeps_bytes_out_of_the_workspace(harness):
    """I/O 前後で資格を再確認し、取得済みでも撤権した内容を公開しない。"""
    h = harness
    h.provider._binding._bound.side_effect = [
        (h.bound, "synthetic-fixture-key"),
        (h.bound, "synthetic-fixture-key"),
        (h.bound, "changed-fixture-key"),
    ]
    with pytest.raises(ToolProviderError, match="binding changed"):
        await h.provider.execute(h.context, h.arguments)
    assert len(h.calls) == 1
    assert not (h.context.workspace.cwd / "resources").exists()


@pytest.mark.parametrize(
    "value",
    [
        "//other.example.test/image.png",
        "/api/../secret",
        "/api/%2e%2e/secret",
        "/api/image?key=x",
        "/api/image#fragment",
        "https://user:fixture@mcp.example.test/image",
        "http://mcp.example.test/image",
    ],
)
def test_download_references_do_not_change_authority_or_decode_paths(value):
    """scheme/authority/資格/query/fragment/dot segment を原文のまま拒否する。"""
    with pytest.raises(ValueError):
        observed_path({"path": value}, "/path", "https://mcp.example.test/mcp")


def test_download_does_not_grant_workspace_reading(harness):
    """新しい download 権だけで読取権を補わない。"""
    with pytest.raises(ToolProviderError, match="workspace reading"):
        require_file_access(replace(harness.context, run=None))


@pytest.mark.parametrize("matching", [False, True])
async def test_advertised_file_checksum_is_verified_before_publication(harness, matching):
    """公開済みの画像 hash があれば照合し、別 byte へ差し替わった画像を保存しない。"""
    h = harness
    expected = sha256_hex(h.data) if matching else "0" * 64
    h.original["result"]["screenshot"]["sha256"] = "sha256:" + expected
    h.tool.result_json = {**h.original, "evidence_refs": ["ev_original"]}
    h.evidence.content_hash = "sha256:" + sha256_hex(canonical_json(h.original))
    h.arguments["hash_pointer"] = "/result/screenshot/sha256"
    if matching:
        result = await h.provider.execute(h.context, h.arguments)
        assert result.response["file"]["content_hash"] == "sha256:" + expected
    else:
        with pytest.raises(ToolProviderError):
            await h.provider.execute(h.context, h.arguments)
        assert not (h.context.workspace.cwd / "resources").exists()
    assert len(h.calls) == 1


async def test_revocation_before_http_prevents_credential_dispatch(harness):
    """観測 DB を読んでいる間の撤権も、HTTP 要求を送る直前に確認する。"""
    h = harness
    h.provider._binding._bound.side_effect = [
        (h.bound, "synthetic-fixture-key"), (h.bound, "changed-fixture-key"),
    ]
    with pytest.raises(ToolProviderError, match="binding changed"):
        await h.provider.execute(h.context, h.arguments)
    assert not h.calls
