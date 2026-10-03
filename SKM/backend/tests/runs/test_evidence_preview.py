"""歴史の表 preview は同一 Run/原 hash/連続応答に限る。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from skillmind.db.models import Evidence, ToolCall
from skillmind.runs.evidence_preview import evidence_preview_metadata


def _records() -> tuple[Evidence, list[ToolCall]]:
    """表頭と本文を別 ToolCall に分け、offset は行中から開始する。"""

    source = (
        "# Scope\n\n| Case | Expected |\n| --- | --- |\n"
        "| A | first |\n| B | second |\n| C | third |\n"
    )
    split = source.index("first") + 1
    run_id, checksum = uuid4(), "sha256:" + "a" * 64
    path = "workspace/document-conversions/spec.md"
    calls = [
        ToolCall(
            id=uuid4(),
            run_id=run_id,
            provider="workspace",
            status="SUCCEEDED",
            capability_version="workspace.read/v1",
            result_json={
                "status": "success",
                "provider": "workspace",
                "path": path,
                "content_hash": checksum,
                "offset": offset,
                "content": content,
            },
        )
        for offset, content in [(0, source[:split]), (split, source[split:])]
    ]
    item = Evidence(
        id=uuid4(),
        run_id=run_id,
        tool_call_id=calls[1].id,
        evidence_type="workspace-file",
        content_hash=checksum,
        source_locator={"path": path, "offset": split},
        excerpt=source[split:],
        metadata_json={"scope": "run-workspace", "read_only": True},
    )
    return item, calls


def test_historical_preview_uses_saved_reads_without_mutating_evidence() -> None:
    """元の抜粋と metadata を書換えず、表示投影だけに完全な行を補う。"""

    item, calls = _records()
    original = item.excerpt
    result = evidence_preview_metadata([item], calls)
    assert result[item.id]["excerpt_preview"]["source"] == (
        "| Case | Expected |\n| --- | --- |\n| B | second |\n| C | third |\n"
    )
    assert item.excerpt == original and "excerpt_preview" not in item.metadata_json


@pytest.mark.parametrize(
    "mismatch", ["run", "hash", "path", "gap", "overlap", "failed", "provider"]
)
def test_context_is_not_borrowed_across_identity_or_missing_reads(mismatch: str) -> None:
    """照合不成立・欠落・失敗・異 Provider の応答を表示事実として利用しない。"""

    item, calls = _records()
    assert calls[0].result_json is not None
    if mismatch == "run":
        calls[0].run_id = uuid4()
    elif mismatch == "hash":
        calls[0].result_json["content_hash"] = "sha256:" + "b" * 64
    elif mismatch == "path":
        calls[0].result_json["path"] = "workspace/other.md"
    elif mismatch == "gap":
        calls[0].result_json["content"] = calls[0].result_json["content"][:-1]
    elif mismatch == "overlap":
        calls.append(
            ToolCall(
                id=uuid4(),
                run_id=item.run_id,
                provider="workspace",
                status="SUCCEEDED",
                capability_version="workspace.read/v1",
                result_json={
                    **calls[0].result_json,
                    "content": "incorrect bytes",
                    "offset": 2,
                },
            )
        )
    elif mismatch == "failed":
        calls[0].status = "FAILED"
    else:
        calls[0].provider = "other"
    assert evidence_preview_metadata([item], calls) == {}
