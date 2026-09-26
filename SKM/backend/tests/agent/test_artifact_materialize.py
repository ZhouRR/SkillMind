"""Artifact の file 交付と現在認可・Run 分離を検証する。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from skillmind.agent.artifact_materialize import ArtifactMaterializeProvider
from skillmind.agent.tool_gateway import ToolProviderError
from tests.agent.test_artifact_append import MemorySource, context_at
from tests.agent.test_workspace_provider import _validate_response


def _context(tmp_path):
    """共通 fixture の境界を新能力に合わせる。"""
    context = context_at(tmp_path)
    return replace(context, tool=replace(context.tool, capability="artifact.materialize/v1"))


async def test_materialization_restores_saved_bytes_without_republication(tmp_path):
    """変更/欠落した作業用 file は元参照の byte から復元し、新 Artifact を発行しない。"""
    context = _context(tmp_path)
    data = ("日本語😀\r\n" * 10000).encode()
    source = MemorySource(context, data)
    provider = ArtifactMaterializeProvider(source)
    args = {"artifact_ref": source.original.metadata.artifact_ref}
    first = await provider.execute(context, args)
    _validate_response("tools/artifact.materialize/v1/response.schema.json", dict(first.response))
    path = context.workspace.root / first.response["file"]["path"]
    assert path.read_bytes() == data and "content" not in first.response
    path.write_text("modified")
    await provider.execute(context, args)
    assert path.read_bytes() == data
    path.unlink()
    restored = await provider.execute(context, args)
    assert path.read_bytes() == data and restored.response == first.response
    assert all(item.artifact is None for item in restored.evidence)


@pytest.mark.parametrize("failure", ["run", "project", "ref", "revoked", "missing"])
async def test_materialization_refuses_foreign_or_revoked_sources(tmp_path, failure):
    """他 Run/Project、不存在、撤権では本文を見せず file も作らない。"""
    context = _context(tmp_path)
    source = MemorySource(context)
    ref = source.original.metadata.artifact_ref
    if failure == "revoked":
        source.revoked_at = 1
    elif failure == "missing":
        source.read_artifact = AsyncMock(side_effect=LookupError("private"))
    else:
        key = {"run": "run_id", "project": "project_id", "ref": "artifact_ref"}[failure]
        value = "art_foreign" if failure == "ref" else uuid4()
        source.original = replace(
            source.original, metadata=replace(source.original.metadata, **{key: value})
        )
        source.read_artifact = AsyncMock(return_value=source.original)
    with pytest.raises(ToolProviderError) as error:
        await ArtifactMaterializeProvider(source).execute(context, {"artifact_ref": ref})
    assert error.value.code == "not_found"
    assert list(context.workspace.cwd.iterdir()) == []


async def test_materialization_rejects_symlink_directory(tmp_path):
    """読み出した byte が workspace 外へ書かれない。"""
    context = _context(tmp_path)
    source = MemorySource(context)
    (context.workspace.cwd / "artifacts").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ToolProviderError):
        await ArtifactMaterializeProvider(source).execute(
            context,
            {
                "artifact_ref": source.original.metadata.artifact_ref,
            },
        )
