"""原生 SQL の query identity・完全性・来歴を実 SQLite JOIN で検証する。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from skillmind.agent.postgres_native import query_identity
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.effects.postgres_native import SqlObservationValidationError
from tests.runs.test_database_observation import observation as observation


@pytest.fixture
def native_observation(observation):
    """新規行が存在しない query の証拠を一つの Run/binding に固定する。"""
    h = observation
    h.binding.scope_json = {"statements": ["SELECT", "INSERT"]}
    h.draft.capability_version = "database.execute/v1"
    h.tool.capability_version = "database.query/v1"
    h.evidence.evidence_type = "resource"
    h.payload = {
        "read_back": {"sql": "SELECT status FROM public.items WHERE id=$1", "parameters": [1]}
    }
    h.evidence.content_hash = "sha256:" + sha256_hex(
        canonical_json({"rows": [], "truncated": False})
    )
    h.draft.precondition = {"revision": h.evidence.content_hash}
    h.evidence.source_locator = {
        "query_identity": query_identity(h.payload["read_back"], h.binding.scope_json)
    }
    h.evidence.metadata_json = {"binding_checksum": h.binding.checksum, "truncated": False}
    return h


async def verify(h):
    """保存と承認の共通 validator を呼ぶ。"""
    await h.repository._validate_database_observation(h.draft, binding=h.binding, payload=h.payload)


async def test_exact_empty_query_with_default_options_is_valid(native_observation):
    """SQL 空白や配送目的の違いは無視し、既定 limit は同じ identity とする。"""
    h = native_observation
    h.payload["read_back"].update(
        sql="select status from public.items where id = $1;",
        limit=1000,
        purpose="Check absence",
        response_mode="file",
    )
    await verify(h)


@pytest.mark.parametrize(
    "damage",
    [
        "uuid-query",
        "parameters",
        "limit",
        "projection",
        "run",
        "tool-run",
        "integration",
        "binding",
        "failed",
        "wrong-capability",
        "wrong-provider",
        "missing-tool",
        "hash",
        "truncated",
        "missing-truncated",
        "missing-query-identity",
        "evidence-ref",
        "workspace-copy",
    ],
)
async def test_unrelated_or_untrusted_query_is_rejected_before_proposal(native_observation, damage):
    """内容 hash が一致しても、異なる query・権限・Run・不完全結果を使わせない。"""
    h = native_observation
    if damage == "uuid-query":
        h.evidence.source_locator = {
            "query_identity": query_identity(
                {"sql": "SELECT gen_random_uuid() AS new_id"}, h.binding.scope_json
            )
        }
    elif damage == "parameters":
        h.payload["read_back"]["parameters"] = [2]
    elif damage == "limit":
        h.payload["read_back"]["limit"] = 1
    elif damage == "projection":
        h.payload["read_back"]["sql"] = "SELECT id FROM public.items WHERE id=$1"
    elif damage == "run":
        h.evidence.run_id = uuid4()
    elif damage == "tool-run":
        h.tool.run_id = uuid4()
    elif damage == "integration":
        h.tool.integration_id = uuid4()
    elif damage == "binding":
        h.evidence.metadata_json = {"binding_checksum": "other", "truncated": False}
    elif damage == "failed":
        h.tool.status = "FAILED"
    elif damage == "wrong-capability":
        h.tool.capability_version = "workspace.read/v1"
    elif damage == "wrong-provider":
        h.tool.provider = "http"
    elif damage == "missing-tool":
        h.evidence.tool_call_id = uuid4()
    elif damage == "hash":
        h.draft.precondition = {"revision": "sha256:" + "f" * 64}
    elif damage == "truncated":
        h.evidence.metadata_json = {"binding_checksum": h.binding.checksum, "truncated": True}
    elif damage == "missing-truncated":
        h.evidence.metadata_json = {"binding_checksum": h.binding.checksum}
    elif damage == "missing-query-identity":
        h.evidence.source_locator = {}
    elif damage == "evidence-ref":
        h.draft.evidence_refs = ("ev_unrelated",)
    else:
        h.evidence.evidence_type = "workspace-file"
    with pytest.raises(SqlObservationValidationError):
        await verify(h)
