"""全資源の準備・独立回执・再訪を、実 file I/O と制御した source で通す。"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from skillmind.agent import workspace_materializer as module
from skillmind.agent.domain import RunWorkspace
from skillmind.agent.repository_source import RepositoryBindingRef
from skillmind.agent.workspace import WorkspaceManager
from skillmind.agent.workspace_materializer import MaterializationError, PreparedInput
from skillmind.core.hashing import sha256_hex
from skillmind.documents.snapshot import DocumentSnapshot
from skillmind.runs.domain import ClaimedRun, LeaseValidationError
from skillmind.runs.input_snapshot import InputFileSeal, InputSnapshotStatus
from tests.agent.input_fakes import MemoryInputSnapshots, input_claim
from tests.agent.test_workspace_materializer import (
    _FakeInventory,
    _FakeRepositorySession,
    _FakeRepositorySource,
)
from tests.documents.fakes import document_content, document_snapshot


@dataclass(frozen=True)
class _Scenario:
    """複数の物化根と、引数から独立した凍結 authority を保持する。"""

    workspace: RunWorkspace
    claim: ClaimedRun
    store: MemoryInputSnapshots
    inventory: _FakeInventory
    source: _FakeRepositorySource
    snapshots: tuple[DocumentSnapshot, ...]
    bindings: dict[str, RepositoryBindingRef]
    blueprint: dict[str, Any]

    async def prepare(self, *, claim: ClaimedRun | None = None, **limits: int) -> PreparedInput:
        """本番 constructor の必須 port と凍結引数を省略せず、実物化器を起動する。"""

        materializer = module.WorkspaceMaterializer(
            document_inventory=self.inventory,
            input_snapshots=self.store,
            repository_source=self.source,
            **{
                "max_bytes": 10_485_760,
                "max_files": 500,
                "max_total_bytes": 104_857_600,
                "max_total_files": 5_000,
                **limits,
            },
        )
        return await materializer.materialize(
            claimed_run=claim or self.claim,
            workspace=self.workspace,
            project_id=self.claim.project_id,
            run_id=self.claim.run_id,
            blueprint=self.blueprint,
            document_snapshots=self.snapshots,
            repository_bindings=self.bindings,
        )


def _scenario(tmp_path: Path, *, original: _Scenario | None = None) -> _Scenario:
    """一文書を二 slot で共有し、同じ repository 内容は二 root へ別々に保存する。"""

    tmp_path.mkdir(parents=True, exist_ok=True)
    project_id, run_id = uuid4(), uuid4()
    contents = [document_content(b"frozen document\n", name="design.md")]
    snapshots = tuple(
        document_snapshot(project_id, contents, key=key) for key in ("config", "reference")
    )
    bindings = {
        key: RepositoryBindingRef(provider="git", integration_id=uuid4(), binding_id=uuid4())
        for key in ("repo_a", "repo_b")
    }
    claim = input_claim(
        project_id=project_id, run_id=run_id, snapshots=snapshots, repository_bindings=bindings
    )
    if original is not None:
        # Byte 境界の比較では UUID や凍結内容まで同じにし、保存先だけを隔離する。
        claim, snapshots, bindings = original.claim, original.snapshots, original.bindings
    return _Scenario(
        workspace=WorkspaceManager(tmp_path / "runs").initialize(claim.run_id),
        claim=claim,
        store=MemoryInputSnapshots(claim),
        inventory=_FakeInventory(contents),
        source=_FakeRepositorySource(
            _FakeRepositorySession({"src/main.py": b"print('snapshot')\n"}), scope_paths=("src",)
        ),
        snapshots=snapshots,
        bindings=bindings,
        blueprint={
            "resource_requirements": [
                {"key": key, "kind": kind, "required": True, "access": "read"}
                for kind, keys in (("document", ("config", "reference")), ("repository", bindings))
                for key in keys
            ]
        },
    )


def _physical_files(workspace: RunWorkspace) -> set[str]:
    """未公開候補の観測用に file 名だけを収集し、内容から回执を生成しない。"""

    namespace = workspace.root / ".skillmind-inputs"
    return {
        path.relative_to(namespace).as_posix() for path in namespace.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("orphan", ["empty", "generation", "file", "symlink"])
async def test_preparation_never_resigns_an_orphan_namespace(tmp_path: Path, orphan: str) -> None:
    """回执を失った別 UUID の候補も保存し、二份目を取得・発行しない。"""

    case = _scenario(tmp_path)
    namespace = case.workspace.root / ".skillmind-inputs"
    if orphan == "file":
        namespace.write_bytes(b"preserve this orphan")
    elif orphan == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "evidence").write_bytes(b"preserve this orphan")
        namespace.symlink_to(outside, target_is_directory=True)
    else:
        namespace.mkdir()
        if orphan == "generation":
            generation = namespace / str(uuid4())
            generation.mkdir()
            (generation / "evidence").write_bytes(b"preserve this orphan")
    names = set(namespace.iterdir()) if namespace.is_dir() else set()

    with pytest.raises(MaterializationError):
        await case.prepare()

    assert case.inventory.calls == case.source.calls == case.store.complete_calls == 0
    assert case.store.record is not None
    assert case.store.record.status is InputSnapshotStatus.PREPARING
    if orphan == "file":
        assert namespace.read_bytes() == b"preserve this orphan"
    else:
        assert set(namespace.iterdir()) == names
        if orphan == "generation":
            assert (generation / "evidence").read_bytes() == b"preserve this orphan"
        elif orphan == "symlink":
            assert namespace.is_symlink()
            assert (outside / "evidence").read_bytes() == b"preserve this orphan"


async def test_missing_required_repository_fails_before_receipt_or_content(tmp_path: Path) -> None:
    """必須 binding が無ければ document の部分準備にも進めない。"""

    case = replace(_scenario(tmp_path), bindings={})
    with pytest.raises(MaterializationError, match="declared requirements"):
        await case.prepare()
    assert case.store.begin_calls == case.inventory.calls == case.source.calls == 0
    assert case.source.inspections == 0
    assert not (case.workspace.root / ".skillmind-inputs").exists()


@pytest.mark.parametrize("unit", ["files", "bytes"])
async def test_all_roots_share_an_exact_budget_including_generated_files(
    tmp_path: Path, unit: str
) -> None:
    """境界値は全根を渡し、一単位足りなければ最後の根を部分発行しない。"""

    reference = _scenario(tmp_path / "reference")
    prepared = await reference.prepare()
    files = prepared.workspace.input_files
    assert files is not None
    assert Counter(item.path.split("/", 1)[0] for item in files) == {
        "documents": 3,
        "repo_a": 4,
        "repo_b": 4,
    }
    assert all(
        item.size == (prepared.workspace.input_dir / item.path).stat().st_size for item in files
    )
    bound = len(files) if unit == "files" else sum(item.size for item in files)
    limits = {f"max_total_{unit}": bound}
    exact = _scenario(tmp_path / "exact", original=reference)
    accepted = await exact.prepare(**limits)
    assert accepted.workspace.input_files == files
    assert exact.inventory.calls == 1 and exact.source.calls == 2
    assert exact.store.complete_calls == 1

    short = _scenario(tmp_path / "short", original=reference)
    with pytest.raises(MaterializationError, match="total materialization budget"):
        await short.prepare(**{f"max_total_{unit}": bound - 1})
    assert short.store.complete_calls == 0
    assert short.store.record is not None
    assert short.store.record.status is InputSnapshotStatus.PREPARING
    assert len(_physical_files(short.workspace)) == 7
    assert not list(short.workspace.input_dir.iterdir())


@pytest.mark.parametrize("limit", ["max_files", "max_bytes", "max_total_files", "max_total_bytes"])
async def test_reuse_enforces_current_root_and_total_limits(tmp_path: Path, limit: str) -> None:
    """READY 再訪でも生成物を含む同じ計量を使い、新設定を cache で迂回しない。"""

    case = _scenario(tmp_path)
    prepared = await case.prepare()
    files = prepared.workspace.input_files
    assert files is not None
    if limit.endswith("files"):
        bound = len(files) if limit.startswith("max_total") else 4
    elif limit.startswith("max_total"):
        bound = sum(item.size for item in files)
    else:
        bound = max(
            sum(item.size for item in files if item.path.startswith(f"{root}/"))
            for root in ("documents", "repo_a", "repo_b")
        )
    with pytest.raises(MaterializationError, match="budget"):
        await case.prepare(**{limit: bound - 1})
    assert case.store.record is not None
    assert case.store.record.status is InputSnapshotStatus.READY
    assert case.store.complete_calls == 1
    assert case.inventory.calls == 1 and case.source.calls == 2


@pytest.mark.parametrize("unit", ["files", "bytes"])
async def test_initial_root_limit_counts_manifest_index_and_history(
    tmp_path: Path, unit: str
) -> None:
    """per-root も実 byte と生成物を計上し、内容 file だけの上限に縮退しない。"""

    reference = _scenario(tmp_path / "reference")
    prepared = await reference.prepare()
    files = prepared.workspace.input_files
    assert files is not None
    bound = max(
        sum(
            1 if unit == "files" else item.size
            for item in files
            if item.path.startswith(f"{root}/")
        )
        for root in ("documents", "repo_a", "repo_b")
    )
    exact = _scenario(tmp_path / "exact", original=reference)
    assert (await exact.prepare(**{f"max_{unit}": bound})).workspace.input_files == files
    short = _scenario(tmp_path / "short", original=reference)
    with pytest.raises(MaterializationError, match="workspace budget"):
        await short.prepare(**{f"max_{unit}": bound - 1})
    assert short.store.complete_calls == 0
    assert short.store.record is not None
    assert short.store.record.status is InputSnapshotStatus.PREPARING
    assert not list(short.workspace.input_dir.iterdir())


@pytest.mark.parametrize("destination", ["outside", "loop"])
async def test_ready_namespace_link_replacement_is_a_materialization_failure(
    tmp_path: Path, destination: str
) -> None:
    """READY 後の directory 置換も安全に拒否し、回执や退避した原本を変更しない。"""

    case = _scenario(tmp_path)
    prepared = await case.prepare()
    receipt = case.store.record
    namespace = case.workspace.root / ".skillmind-inputs"
    retained = tmp_path / "retained"
    namespace.rename(retained)
    namespace.symlink_to(
        retained if destination == "outside" else namespace, target_is_directory=True
    )
    with pytest.raises(MaterializationError, match="Input generation path is not safe"):
        await case.prepare()
    assert case.store.record == receipt
    assert case.store.complete_calls == 1
    assert case.inventory.calls == 1 and case.source.calls == 2
    assert prepared.workspace.input_files is not None
    for item in prepared.workspace.input_files:
        data = (retained / prepared.workspace.input_dir.name / item.path).read_bytes()
        assert len(data) == item.size and f"sha256:{sha256_hex(data)}" == item.checksum


@pytest.mark.parametrize("reason", ["cancelled", "expired", "taken_over"])
async def test_sealed_candidate_cannot_complete_after_execution_authority_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    """file 同期後の取消/lease 失効でも READY を渡さず、封じた現場を保存する。"""

    case = _scenario(tmp_path)
    original = module.seal_generation

    def seal_then_revoke(root: Path, *, relative: str, files: tuple[InputFileSeal, ...]) -> None:
        """最後の file I/O と DB 完成の間へ、制御された権限変化を挿入する。"""

        original(root, relative=relative, files=files)
        if reason == "cancelled":
            case.store.cancelled = True
        elif reason == "expired":
            case.store.current_claim = replace(
                case.claim, lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        else:
            case.store.current_claim = replace(
                case.claim, run_attempt_id=uuid4(), lease_token="replacement-lease"
            )

    monkeypatch.setattr(module, "seal_generation", seal_then_revoke)
    expected = MaterializationError if reason == "cancelled" else LeaseValidationError
    with pytest.raises(expected):
        await case.prepare()
    assert case.store.complete_calls == 1
    assert case.store.record is not None
    assert case.store.record.status is InputSnapshotStatus.PREPARING
    assert case.store.record.files == ()
    assert len(_physical_files(case.workspace)) == 11
    assert not list(case.workspace.input_dir.iterdir())
    if reason == "taken_over":
        with pytest.raises(MaterializationError, match="incomplete"):
            await case.prepare(claim=case.store.current_claim)
        assert case.store.complete_calls == 1
        assert case.inventory.calls == 1 and case.source.calls == 2
        assert len(_physical_files(case.workspace)) == 11


@pytest.mark.parametrize("new_segment", [False, True])
async def test_completed_input_survives_takeover_without_reading_live_sources(
    tmp_path: Path, new_segment: bool
) -> None:
    """技術復旧も業務續行も同じ READY を読み、移動した branch を取得し直さない。"""

    case = _scenario(tmp_path)
    first = await case.prepare()
    next_claim = replace(
        case.claim,
        run_attempt_id=uuid4(),
        run_segment_id=uuid4() if new_segment else case.claim.run_segment_id,
        lease_token="replacement-lease",
    )
    case.store.current_claim = next_claim
    case.inventory._contents = ()
    case.source._session = _FakeRepositorySession(
        {"src/main.py": b"untrusted new branch content\n"}, revision="b" * 40
    )
    reused = await case.prepare(claim=next_claim)
    assert reused == first
    assert case.store.complete_calls == 1
    assert case.inventory.calls == 1 and case.source.calls == 2
    assert case.source.inspections == 4


@pytest.mark.parametrize("changed", ["checksum", "scope"])
async def test_cached_repositories_still_require_frozen_binding_authorization(
    tmp_path: Path, changed: str
) -> None:
    """cache の存在が binding の現行検査や保存 scope の照合を省略する理由にならない。"""

    case = _scenario(tmp_path)
    await case.prepare()
    if changed == "checksum":
        case.source.checksum = f"sha256:{'b' * 64}"
    else:
        case.source._scope_paths = ("different-scope",)
    with pytest.raises(MaterializationError):
        await case.prepare()
    assert case.store.complete_calls == 1
    assert case.inventory.calls == 1 and case.source.calls == 2


@pytest.mark.parametrize("section", ["files", "generated"])
async def test_file_and_manifest_co_tampering_cannot_replace_trusted_receipt(
    tmp_path: Path, section: str
) -> None:
    """内容・manifest hash・内容摘要を同時に改竄しても独立 DB 回执が拒否する。"""

    case = _scenario(tmp_path)
    prepared = await case.prepare()
    receipt = case.store.record
    manifest_path = prepared.workspace.input_dir / "repo_a/.skillmind/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = manifest[section][-1]
    target = prepared.workspace.input_dir / entry["path"]
    forged = bytes(reversed(target.read_bytes()))
    target.chmod(0o600)
    target.write_bytes(forged)
    entry["content_hash"] = f"sha256:{sha256_hex(forged)}"
    digest_source = "\n".join(
        f"{item['path']}:{item['content_hash']}" for item in manifest["files"]
    )
    manifest["content_digest"] = f"sha256:{sha256_hex(digest_source)}"
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    with pytest.raises(MaterializationError, match="trusted receipt"):
        await case.prepare()
    assert case.store.record == receipt
    assert case.store.complete_calls == 1
    assert case.inventory.calls == 1 and case.source.calls == 2
    assert target.read_bytes() == forged


async def test_forged_claim_and_matching_arguments_cannot_redefine_frozen_sources(
    tmp_path: Path,
) -> None:
    """呼出し側が claim と引数を共に改変しても、独立 store の凍結事実は変わらない。"""

    case = _scenario(tmp_path)
    forged = document_content(b"forged content\n", name="forged.md")
    snapshots = tuple(
        document_snapshot(case.claim.project_id, [forged], key=key)
        for key in ("config", "reference")
    )
    alternate = input_claim(
        project_id=case.claim.project_id,
        run_id=case.claim.run_id,
        snapshots=snapshots,
        repository_bindings=case.bindings,
    )
    claim = replace(case.claim, selected_sources_json=alternate.selected_sources_json)
    with pytest.raises(MaterializationError, match="sources do not match"):
        await replace(case, snapshots=snapshots).prepare(claim=claim)
    assert case.store.begin_calls == 1 and case.store.record is None
    assert case.store.complete_calls == case.inventory.calls == case.source.calls == 0
    assert not (case.workspace.root / ".skillmind-inputs").exists()
