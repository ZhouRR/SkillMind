"""公開資源清単が現在資源・不正 metadata・内部 field を混入させないことを検証する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from skillmind.documents.snapshot import DOCUMENT_PROVIDER, DOCUMENT_READ_CAPABILITY
from skillmind.runs.resource_projection import document_snapshots, source_summaries
from tests.documents.fakes import document_content, document_snapshot


def source(snapshot: Any) -> dict[str, Any]:
    """検証済み metadata を Run の既存 document source に包む。"""

    return {
        "provider": DOCUMENT_PROVIDER,
        "capability": DOCUMENT_READ_CAPABILITY,
        "resource_kind": "document",
        "access": "read",
        "document_snapshot": snapshot,
    }


@pytest.mark.parametrize("mode", ["SINGLE", "SET", "ALL"])
@pytest.mark.parametrize("capability", ["document.read/v1", "document.convert/v1"])
def test_projection_uses_only_original_metadata_and_does_not_mutate_it(
    mode: str, capability: str,
) -> None:
    """清単の公開は source の照会・hash 書換え・歴史補填を行わない。"""

    project = uuid4()
    items = [document_content()]
    if mode != "SINGLE":
        items.append(document_content(name="second.md"))
    snapshot = replace(document_snapshot(project, items), selection_mode=mode)
    sources = {"config": {**source(snapshot.to_json()), "capability": capability}}
    before = deepcopy(sources)
    result = document_snapshots(sources, project_id=project)
    assert result[0].status == "FROZEN"
    assert result[0].snapshot == snapshot
    assert sources == before


@pytest.mark.parametrize(
    "change", ["project", "slot", "hash", "version", "member", "provider", "null"]
)
def test_invalid_snapshot_reveals_no_members(change: str) -> None:
    """他 Project の ID/path や checksum 不正を失敗の本文からも公開しない。"""

    project = uuid4()
    value = document_snapshot(project, [document_content()]).to_json()
    selected = source(value)
    if change == "project":
        value["project_id"] = str(uuid4())
    elif change == "slot":
        value["requirement_key"] = "foreign"
    elif change == "hash":
        value["checksum"] = "sha256:" + "0" * 64
    elif change == "version":
        value["snapshot_version"] = "unknown"
    elif change == "member":
        value["documents"][0]["blob_locator"] = "fixture-private"
    elif change == "provider":
        selected["provider"] = "wrong"
    else:
        selected["document_snapshot"] = None
    result = document_snapshots({"config": selected}, project_id=project)
    assert result[0].status == "INVALID"
    assert result[0].snapshot is None


@pytest.mark.parametrize(
    "selected", ["project", DOCUMENT_PROVIDER, {"capability": DOCUMENT_READ_CAPABILITY}]
)
def test_legacy_source_is_missing_not_an_empty_or_current_snapshot(selected: Any) -> None:
    """情報欠落は正常な空集合や現在の全集へ置き換えない。"""

    result = document_snapshots({"config": selected}, project_id=uuid4())
    assert result[0].status == "LEGACY_UNAVAILABLE"
    assert result[0].snapshot is None
    assert document_snapshots({}, project_id=uuid4()) == ()


def test_conflicting_slots_do_not_publish_a_misleading_frozen_union() -> None:
    """同一 ID の異なる内容を、それぞれ正しい清単として表示しない。"""

    project = uuid4()
    one = document_snapshot(project, [document_content()])
    two = document_snapshot(project, [document_content(b"changed")], key="other")
    result = document_snapshots(
        {"config": source(one.to_json()), "other": source(two.to_json())}, project_id=project
    )
    assert [item.status for item in result] == ["INVALID", "INVALID"]
    assert all(item.snapshot is None for item in result)


def test_source_summaries_have_one_allowlist_for_history_and_detail() -> None:
    """内部 field や将来 field を再帰的に透過せず、旧 provider 表示を保持する。"""

    sources = {
        "legacy": "git",
        "repo": {
            "provider": "git",
            "capability": "repository.read/v1",
            "resource_kind": "repository",
            "access": "read",
            "scope": {"internal": "fixture-private"},
            "secret_locator": "fixture-private",
            "future_field": {"token": "fixture-private"},
        },
    }
    result = source_summaries(sources)
    assert result == {
        "legacy": "git",
        "repo": {
            "provider": "git",
            "capability": "repository.read/v1",
            "resource_kind": "repository",
            "access": "read",
        },
    }
    assert "fixture-private" not in repr(result)
