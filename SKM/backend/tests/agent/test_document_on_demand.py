"""按需文書の準備・凍結回执・混合 slot・明示取得を実 file I/O で検証する。"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from skillmind.agent.document_provider import DocumentConvertProvider
from skillmind.agent.tool_gateway import ToolProviderError
from skillmind.agent.workspace import WorkspaceManager
from skillmind.agent.workspace_materializer import (
    MaterializationError,
    PreparedInput,
    WorkspaceMaterializer,
)
from skillmind.documents.snapshot import DocumentSnapshot, DocumentSnapshotError
from skillmind.documents.source import ProjectDocumentContent
from skillmind.runs.domain import ClaimedRun
from skillmind.runs.input_snapshot import InputSnapshotStatus
from tests.agent.input_fakes import MemoryInputSnapshots, input_claim
from tests.agent.test_document_conversion import _conversion_context
from tests.agent.test_document_provider import _FakeSource
from tests.agent.test_workspace_materializer import _FakeInventory
from tests.documents.fakes import document_content, document_snapshot


@dataclass
class _Case:
    """本文 source と凍結 authority を独立させた文書のみの準備シナリオ。"""

    claim: ClaimedRun
    snapshots: tuple[DocumentSnapshot, ...]
    store: MemoryInputSnapshots
    materializer: WorkspaceMaterializer
    inventory: AsyncMock
    root: Path

    async def prepare(self, claim: ClaimedRun | None = None) -> PreparedInput:
        """同じ Run 世代を初回準備または再訪し、対象を再列挙しない。"""

        return await self.materializer.materialize(
            claimed_run=claim or self.claim,
            workspace=WorkspaceManager(self.root).initialize(self.claim.run_id),
            project_id=self.claim.project_id,
            run_id=self.claim.run_id,
            blueprint={
                "resource_requirements": [
                    {
                        "key": item.requirement_key,
                        "kind": "document",
                        "access": "read",
                        "required": True,
                    }
                    for item in self.snapshots
                ]
            },
            document_snapshots=self.snapshots,
        )


def _case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mixed: bool = False,
    policy: object = "on-demand/v1",
    legacy: bool = False,
    capability: str = "document.convert/v1",
) -> tuple[_Case, ProjectDocumentContent]:
    """変換対象は意図的に不正な Excel とし、準備中に取得しないことを明瞭にする。"""

    project_id, run_id = uuid4(), uuid4()
    excel = document_content(b"\xffnot acquired until explicit conversion", name="cases.xlsx")
    text = document_content(b"reference text", name="reference.md")
    snapshots = (document_snapshot(project_id, [excel], key="excel"),)
    if mixed:
        snapshots += (document_snapshot(project_id, [excel, text], key="reference"),)
    claim = input_claim(project_id=project_id, run_id=run_id, snapshots=snapshots)
    source = {**claim.selected_sources_json["excel"], "capability": capability}
    if not legacy:
        source["preparation_policy"] = policy
    claim = replace(claim, selected_sources_json={**claim.selected_sources_json, "excel": source})
    store = MemoryInputSnapshots(claim)
    inventory = _FakeInventory([excel, text])
    calls = AsyncMock(wraps=inventory.list_contents)
    monkeypatch.setattr(inventory, "list_contents", calls)
    materializer = WorkspaceMaterializer(
        document_inventory=inventory, input_snapshots=store, max_bytes=10_485_760, max_files=500
    )
    return _Case(claim, snapshots, store, materializer, calls, tmp_path / "runs"), excel


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize(
    "capability", ["document.convert/v1", "document.inspect/v1", "document.list/v1"]
)
async def test_preparation_defers_original_bytes_even_when_read_slot_overlaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mixed: bool,
    capability: str,
) -> None:
    """按需文書は index だけに現れ、重複 slot 経由の先行取得・変換も起こらない。"""

    case, excel = _case(tmp_path, monkeypatch, mixed=mixed, capability=capability)
    prepared = await case.prepare()
    assert case.store.record is not None and case.store.record.status is InputSnapshotStatus.READY
    assert case.inventory.await_count == (1 if mixed else 0)
    if mixed:
        acquired = case.inventory.await_args.kwargs["documents"]
        assert [item.name for item in acquired] == ["reference.md"]
    root = prepared.workspace.input_dir / "documents"
    assert not (root / "specs/cases.xlsx").exists()
    assert not (root / "specs/cases.xlsx.txt").exists()
    manifest = json.loads((root / ".skillmind/manifest.json").read_bytes())
    assert manifest["manifest_version"] == "v2"
    assert manifest["skipped"] == []
    assert manifest["deferred"] == [
        {
            "path": "documents/specs/cases.xlsx",
            "document_id": str(excel.document_id),
            "content_hash": excel.checksum,
            "reason": "on_demand",
        }
    ]
    assert len(manifest["files"]) == (1 if mixed else 0)
    index = (root / ".skillmind/files.txt").read_text()
    assert "# deferred\tspecs/cases.xlsx\t" + excel.checksum in index
    assert "not downloaded or converted" in index
    assert prepared.resources[0].deferred == 1 and prepared.resources[0].skipped == 0
    first_receipt = case.store.record
    repeated = await case.prepare()
    assert repeated == prepared
    assert case.store.record == first_receipt and case.store.complete_calls == 1
    assert case.inventory.await_count == (1 if mixed else 0)


async def test_legacy_conversion_source_keeps_original_manifest_and_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 source に新 policy を補わず、READY v1 を読み替え・再生成しない。"""

    case, _ = _case(tmp_path, monkeypatch, legacy=True)
    prepared = await case.prepare()
    manifest = json.loads(
        (prepared.workspace.input_dir / "documents/.skillmind/manifest.json").read_bytes()
    )
    assert manifest["manifest_version"] == "v1" and "deferred" not in manifest
    assert manifest["skipped"][0]["reason"] == "unconvertible_document"
    assert case.inventory.await_count == 1
    assert await case.prepare() == prepared
    assert case.inventory.await_count == 1


@pytest.mark.parametrize("policy", [None, "", "on-demand/v2", {}, True])
async def test_unknown_policy_fails_before_preparation_or_source_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: object,
) -> None:
    """不明・欠損 policy 値は新旧の既定へ丸めず、回执も作らない。"""

    case, _ = _case(tmp_path, monkeypatch, policy=policy)
    with pytest.raises(DocumentSnapshotError, match="policy is invalid"):
        await case.prepare()
    assert case.store.record is None and case.inventory.await_count == 0


@pytest.mark.parametrize("original_legacy", [False, True])
async def test_ready_cannot_change_preparation_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    original_legacy: bool,
) -> None:
    """再訪時の policy 追加・削除は原 source checksum で拒否し、現場を保つ。"""

    case, _ = _case(tmp_path, monkeypatch, legacy=original_legacy)
    prepared = await case.prepare()
    before = {
        path: path.read_bytes()
        for path in prepared.workspace.input_dir.rglob("*")
        if path.is_file()
    }
    source = dict(case.claim.selected_sources_json["excel"])
    if original_legacy:
        source["preparation_policy"] = "on-demand/v1"
    else:
        del source["preparation_policy"]
    changed = replace(
        case.claim, selected_sources_json={**case.claim.selected_sources_json, "excel": source}
    )
    with pytest.raises(MaterializationError, match="sources do not match"):
        await case.prepare(changed)
    assert all(path.read_bytes() == data for path, data in before.items())


async def test_corrupted_deferred_manifest_cannot_be_resigned_or_refetched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """按需でも manifest の独立 file seal を緩めず、変更を補修しない。"""

    case, _ = _case(tmp_path, monkeypatch)
    prepared = await case.prepare()
    path = prepared.workspace.input_dir / "documents/.skillmind/manifest.json"
    path.chmod(0o600)
    changed = path.read_bytes().replace(b"on_demand", b"destroyed")
    path.write_bytes(changed)
    with pytest.raises(MaterializationError):
        await case.prepare()
    assert path.read_bytes() == changed and case.inventory.await_count == 0


async def test_cancelled_preparation_does_not_publish_deferred_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本文 I/O がなくても現在の取消門禁を省略せず、READY を作らない。"""

    case, _ = _case(tmp_path, monkeypatch)
    case.store.cancelled = True
    with pytest.raises(MaterializationError, match="cancelled"):
        await case.prepare()
    assert case.store.record is None and case.inventory.await_count == 0


async def test_deferred_source_still_requires_explicit_tool_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """準備成功を変換成功とせず、明示 Tool 呼出しで初めて取得・原本検証を行う。"""

    case, excel = _case(tmp_path, monkeypatch)
    prepared = await case.prepare()
    context = _conversion_context(excel)
    assert context.run is not None
    run = replace(
        context.run,
        run_id=case.claim.run_id,
        run_attempt_id=case.claim.run_attempt_id,
        project_id=case.claim.project_id,
        user_id=case.claim.actor_id,
        workspace=prepared.workspace,
        resolved_sources=case.claim.selected_sources_json,
    )
    context = replace(
        context,
        run=run,
        run_id=run.run_id,
        run_attempt_id=run.run_attempt_id,
        project_id=run.project_id,
        user_id=run.user_id,
        workspace=prepared.workspace,
    )
    source = _FakeSource(project_id=run.project_id, content=excel)
    assert source.calls == [] and case.inventory.await_count == 0
    # 不正 Excel は実 Tool で初めて失敗する。準備時に skip/成功へ偽装されない。
    with pytest.raises(ToolProviderError) as caught:
        await DocumentConvertProvider(source).execute(context, {"path": "specs/cases.xlsx"})
    assert caught.value.code == "unavailable"
    assert source.calls == [excel.document_id]
    assert case.store.record is not None and case.store.record.status is InputSnapshotStatus.READY
