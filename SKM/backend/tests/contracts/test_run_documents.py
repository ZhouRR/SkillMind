"""文書選択と Run 公開投影の例・意味・拒否条件を同時に固定する。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from skillmind.documents.snapshot import parse_document_snapshot

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def load(relative: str) -> dict[str, Any]:
    """独立した値を読み、異常系の変更が他の試験へ漏れないようにする。"""

    return json.loads((CONTRACTS / relative).read_text())


def validate(relative: str, value: Any) -> None:
    """UUID 形式も含め、公開例と同じ validator で検証する。"""

    Draft202012Validator(load(relative), format_checker=FormatChecker()).validate(value)


def test_frozen_example_uses_the_actual_server_checksum_and_slot_identity() -> None:
    """例を形だけの hash にせず、Worker/公開投影と同じ文書 parser でも検証する。"""

    value = load("examples/run-detail-documents.v1.json")
    for entry in value["document_snapshots"]:
        if entry["status"] == "FROZEN":
            snapshot = parse_document_snapshot(
                entry["snapshot"], project_id=UUID(value["project_id"]),
                requirement_key=entry["requirement_key"],
            )
            assert snapshot.to_json() == entry["snapshot"]
        else:
            assert entry["snapshot"] is None


def test_history_and_detail_share_the_summary_allowlist() -> None:
    """履歴だけ内部 JSON が復活する契約分岐を作らない。"""

    detail = load("runs/detail/v1.schema.json")
    history = load("runs/history/v1.schema.json")
    assert detail["properties"]["selected_sources"] == (
        history["properties"]["items"]["items"]["properties"]["selected_sources"]
    )


@pytest.mark.parametrize("change", ["missing", "null", "legacy-members", "version", "locator"])
def test_detail_contract_rejects_ambiguous_or_private_document_projection(change: str) -> None:
    """欠落・状態と本文の不一致・内部 field を空集合への変換で通さない。"""

    value = load("examples/run-detail-documents.v1.json")
    entry = value["document_snapshots"][0]
    if change == "missing":
        del value["document_snapshots"]
    elif change == "null":
        entry["snapshot"] = None
    elif change == "legacy-members":
        entry["status"] = "LEGACY_UNAVAILABLE"
    elif change == "version":
        entry["snapshot"]["snapshot_version"] = "unknown"
    else:
        value["selected_sources"]["documents"]["secret_locator"] = "not-public"
    with pytest.raises(ValidationError):
        validate("runs/detail/v1.schema.json", value)


@pytest.mark.parametrize(
    "selection", ["document:", "documents:", "document:not-a-uuid",
                  "documents:00000000-0000-4000-8000-000000000071", "project-documents:other"],
)
def test_creation_contract_rejects_unfinished_reserved_selection_tokens(selection: str) -> None:
    """Provider の既存文字列は保ち、予約済み文書 token の形だけを厳密にする。"""

    value = load("examples/create-task-run-documents-all.v1.json")
    value["sources"] = {"documents": selection}
    with pytest.raises(ValidationError):
        validate("runs/task-create/v1/request.schema.json", value)
