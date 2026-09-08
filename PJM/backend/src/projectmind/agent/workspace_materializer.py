"""冻结した ResourceBinding を Run workspace の input/ へ只読物化する (計画 §19 W3/W4)。

Agent は物化済みの input/ を既存の workspace.search/read で自走発見する。凭据は物化段階の
Provider 境界内でのみ解決され、Agent 上下文・日志・manifest には決して現れない。物化対象は
二路: document 一路は Run の凍結文書集合だけを input/documents/ へ、repository
一路 (§19 W4) は Run に凍結された binding の scope 配下を revision 固定で
input/<requirement_key>/ へ落とす。いずれも上限超過は截断せず fail closed とし、読めなかった
file は manifest.skipped に必ず残す (「読めない」を「存在しない」と誤認させないため)。

計画 §19 W5 で三つの増分を加えた: ① `.projectmind/files.txt` (物化物と skip の索引。名前で
file を探す idiom (`svn list | grep`) を `workspace.search` で成立させる)、② `.projectmind/
history.txt` (repository の commit 履歴。新 capability を足さずに「いつ誰が触ったか」を渡す)、
③ 二進設計書 (xlsx/xlsm/docx) の platform 側 text 化 (Agent は text だけを見る)。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID

from projectmind.agent.binary_text import (
    BinaryTextError,
    is_textualizable,
    render_text,
)
from projectmind.agent.domain import MaterializedResource, RunWorkspace
from projectmind.agent.input_workspace import input_relative, read_input_file, verify_input
from projectmind.agent.materialization_storage import (
    MaterializationError as MaterializationError,
)
from projectmind.agent.materialization_storage import (
    create_generation,
    relative_parts,
    seal_generation,
    verify_tree,
    write_new_file,
)
from projectmind.agent.repository_client import (
    RepositoryClientError,
    RepositoryCommit,
    RepositoryListing,
)
from projectmind.agent.repository_source import (
    RepositoryBindingRef,
    RepositorySnapshotBinding,
    RepositorySnapshotSource,
    ScopedRepositorySession,
)
from projectmind.core.hashing import sha256_hex
from projectmind.documents.snapshot import (
    DOCUMENT_PROVIDER,
    DocumentSnapshot,
    DocumentSnapshotError,
    FrozenDocument,
    parse_document_snapshot,
    selected_document_snapshots,
    snapshot_documents,
)
from projectmind.documents.source import (
    ProjectDocumentContent,
    ProjectDocumentInventory,
    verify_frozen_content,
)
from projectmind.runs.domain import ClaimedRun
from projectmind.runs.input_snapshot import (
    InputFileSeal,
    InputSnapshotError,
    InputSnapshotStatus,
    InputSnapshotStore,
    input_source_checksum,
)

# 物化した個別 file の上限。search の per-file 予算 (workspace_provider._MAX_FILE_BYTES) と揃え、
# 「物化したのに search が飛ばす」死角を作らない。超過 file は skip し manifest に理由を残す。
_MAX_FILE_BYTES = 1_048_576

# 文書は Run 内の選択集合を共用する。各 slot の所属は manifest に残す。
_DOCUMENTS_KEY = "documents"
_PLATFORM_DIRECTORY = ".projectmind"
_MANIFEST_RELATIVE = f"{_PLATFORM_DIRECTORY}/manifest.json"
_FILE_INDEX_RELATIVE = f"{_PLATFORM_DIRECTORY}/files.txt"
_HISTORY_RELATIVE = f"{_PLATFORM_DIRECTORY}/history.txt"

# 履歴の件数上限。全履歴ではなく「直近 N 件」という定義の明確な部分集合にする (件数は
# history.txt の見出しに書き、Agent が「これで全部」と誤解しないようにする)。
_MAX_HISTORY_ENTRIES = 200


@dataclass(frozen=True, slots=True)
class _AcceptedFile:
    """物化が確定した 1 file (path は input/ からの相対)。

    ``converted_from`` が在る file は原本そのものではなく platform が text 化した派生物である。
    Agent と監査が「これは変換結果だ」と判別できるよう、原本の path と hash を manifest へ残す。
    """

    path: str
    data: bytes
    content_hash: str
    converted_from: str | None = None
    source_content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedInput:
    """全資源の完成回执と一致した workspace だけを Brief/Tool へ渡す。"""

    workspace: RunWorkspace
    resources: tuple[MaterializedResource, ...]


@dataclass(frozen=True, slots=True)
class _PreparedRoot:
    """一 root の案内と、生成時の byte から取得した file receipt。"""

    resource: MaterializedResource
    files: tuple[InputFileSeal, ...]


class WorkspaceMaterializer:
    """冻结した資源を Run 準備段階で input/ へ只読物化する単一実装。"""

    def __init__(
        self,
        *,
        document_inventory: ProjectDocumentInventory,
        input_snapshots: InputSnapshotStore,
        max_bytes: int,
        max_files: int,
        max_total_bytes: int = 104_857_600,
        max_total_files: int = 5_000,
        repository_source: RepositorySnapshotSource | None = None,
    ) -> None:
        """文書列挙 port、repository session source と体积/件数上限を保持する。

        ``repository_source`` 未注入 (offline/未配線) の環境では repository 要求を物化できない
        ため、宣言された binding があれば fail closed とする。黙って空の樹を渡すと、Agent は
        「対象 file が存在しない」と誤結論する。
        """

        if min(max_bytes, max_files, max_total_bytes, max_total_files) < 1:
            raise ValueError("Materialization limits must be positive")
        self._document_inventory = document_inventory
        self._repository_source = repository_source
        self._input_snapshots = input_snapshots
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._max_total_bytes = max_total_bytes
        self._max_total_files = max_total_files

    async def materialize(
        self,
        *,
        claimed_run: ClaimedRun,
        workspace: RunWorkspace,
        project_id: UUID,
        run_id: UUID,
        blueprint: Mapping[str, Any],
        repository_bindings: Mapping[str, RepositoryBindingRef] | None = None,
        document_snapshots: Sequence[DocumentSnapshot] = (),
    ) -> PreparedInput:
        """lease 認領した新世代を封じ、DB の READY が確定してから Agent へ渡す。"""

        if (
            claimed_run.project_id != project_id
            or claimed_run.run_id != run_id
            or workspace.root.name != str(run_id)
        ):
            raise MaterializationError("Input preparation does not belong to the claimed Run")
        snapshots = _validated_document_snapshots(blueprint, project_id, document_snapshots)
        if [item.to_json() for item in snapshots] != [
            item.to_json()
            for item in selected_document_snapshots(
                claimed_run.selected_sources_json, project_id=project_id
            )
        ]:
            raise MaterializationError("Prepared documents do not match the frozen Run sources")
        bindings = repository_bindings or {}
        repository_metadata = await self._inspect_bindings(claimed_run, blueprint, bindings)
        # 旧 input には独立回执がない。現在の内容へ補签せず、現場を保存して拒否する。
        await asyncio.to_thread(verify_tree, workspace.root, relative="input", expected_files=set())
        try:
            receipt, created = await self._input_snapshots.begin(claimed_run)
        except InputSnapshotError as error:
            raise MaterializationError(str(error)) from error
        if (
            receipt.project_id != project_id
            or receipt.run_id != run_id
            or receipt.source_checksum
            != input_source_checksum(
                project_id=project_id, run_id=run_id, sources=claimed_run.selected_sources_json
            )
        ):
            raise MaterializationError("Input receipt does not match the frozen Run")
        relative = f".projectmind-inputs/{receipt.snapshot_id}"
        candidate = replace(workspace, input_dir=workspace.root / relative, input_files=None)
        if not created:
            if receipt.status is not InputSnapshotStatus.READY:
                raise MaterializationError(
                    "Input preparation is incomplete; preserve its generation"
                )
            verified = replace(candidate, input_files=receipt.files)
            self._ensure_total_budget(receipt.files)
            await asyncio.to_thread(verify_input, verified)
            resources = await asyncio.to_thread(
                self._reuse, verified, project_id, run_id, snapshots, bindings, repository_metadata
            )
            return PreparedInput(verified, resources)
        if (
            receipt.status is not InputSnapshotStatus.PREPARING
            or receipt.prepared_by_attempt_id != claimed_run.run_attempt_id
            or receipt.files
        ):
            raise MaterializationError(
                "Input preparation receipt cannot authorize a new generation"
            )
        await asyncio.to_thread(create_generation, workspace.root, relative)
        materialized: list[MaterializedResource] = []
        files: list[InputFileSeal] = []
        if snapshots:
            prepared = await self._materialize_documents(
                workspace=candidate,
                project_id=project_id,
                run_id=run_id,
                snapshots=snapshots,
                previous_files=files,
            )
            materialized.append(prepared.resource)
            files.extend(prepared.files)
        for requirement_key, binding in sorted(bindings.items()):
            prepared = await self._materialize_repository(
                workspace=candidate,
                project_id=project_id,
                run_id=run_id,
                requirement_key=requirement_key,
                binding=binding,
                metadata=repository_metadata[requirement_key],
                previous_files=files,
            )
            materialized.append(prepared.resource)
            files.extend(prepared.files)
        sealed = tuple(sorted(files, key=lambda item: item.path))
        await asyncio.to_thread(seal_generation, workspace.root, relative=relative, files=sealed)
        try:
            completed = await self._input_snapshots.complete(
                claimed_run, snapshot_id=receipt.snapshot_id, files=sealed
            )
        except InputSnapshotError as error:
            raise MaterializationError(str(error)) from error
        if (
            completed.snapshot_id != receipt.snapshot_id
            or completed.status is not InputSnapshotStatus.READY
            or completed.files != sealed
            or completed.source_checksum != receipt.source_checksum
        ):
            raise MaterializationError("Input publication did not confirm this generation")
        return PreparedInput(replace(candidate, input_files=sealed), tuple(materialized))

    async def _inspect_bindings(
        self,
        claimed: ClaimedRun,
        blueprint: Mapping[str, Any],
        bindings: Mapping[str, RepositoryBindingRef],
    ) -> dict[str, RepositorySnapshotBinding]:
        """凍結來源と現行 binding を突き合わせ、cache 再訪でも権限検査を省略しない。"""

        declared = {
            item["key"]: item
            for item in blueprint.get("resource_requirements", [])
            if isinstance(item, Mapping)
            and item.get("kind") == "repository"
            and isinstance(item.get("key"), str)
        }
        if set(bindings) - set(declared) or any(
            item.get("required") and key not in bindings for key, item in declared.items()
        ):
            raise MaterializationError("Repository bindings do not match the declared requirements")
        result: dict[str, RepositorySnapshotBinding] = {}
        for key, binding in sorted(bindings.items()):
            _materialization_root(key)
            if self._repository_source is None:
                raise MaterializationError("Repository materialization is not configured")
            source = claimed.selected_sources_json.get(key)
            if not isinstance(source, Mapping) or any(
                source.get(name) != value
                for name, value in {
                    "provider": binding.provider,
                    "binding_id": str(binding.binding_id),
                    "integration_id": str(binding.integration_id),
                }.items()
            ):
                raise MaterializationError("Repository source does not match its frozen binding")
            try:
                metadata = await self._repository_source.inspect(
                    project_id=claimed.project_id, run_id=claimed.run_id, binding=binding
                )
            except RepositoryClientError as error:
                raise MaterializationError(
                    f"Repository binding is unavailable: {error.code}"
                ) from error
            if source.get("binding_checksum") != metadata.checksum:
                raise MaterializationError("Repository binding checksum changed after Run creation")
            result[key] = metadata
        return result

    def _reuse(
        self,
        workspace: RunWorkspace,
        project_id: UUID,
        run_id: UUID,
        snapshots: Sequence[DocumentSnapshot],
        bindings: Mapping[str, RepositoryBindingRef],
        metadata: Mapping[str, RepositorySnapshotBinding],
    ) -> tuple[MaterializedResource, ...]:
        """READY の実 byte からだけ案内を再構成し、Project や remote を再取得しない。"""

        resources: list[MaterializedResource] = []
        expected_roots = set(bindings)
        if snapshots:
            expected_roots.add(_DOCUMENTS_KEY)
            documents = snapshot_documents(snapshots)
            resources.append(
                _describe(
                    _verify_materialized(
                        workspace,
                        _DOCUMENTS_KEY,
                        expected=_document_identity(project_id, run_id, snapshots, documents),
                        documents=documents,
                    )
                )
            )
        for key, binding in sorted(bindings.items()):
            resources.append(
                _describe(
                    _verify_materialized(
                        workspace,
                        key,
                        expected={
                            **_repository_identity(project_id, run_id, key, binding),
                            "binding_checksum": metadata[key].checksum,
                            "scope": {"paths": list(metadata[key].scope_paths)},
                        },
                    )
                )
            )
        if {item.path.split("/", 1)[0] for item in workspace.input_files or ()} != expected_roots:
            raise MaterializationError("Input receipt has unexpected resource roots")
        return tuple(resources)

    async def _materialize_documents(
        self,
        *,
        workspace: RunWorkspace,
        project_id: UUID,
        run_id: UUID,
        snapshots: Sequence[DocumentSnapshot],
        previous_files: Sequence[InputFileSeal],
    ) -> _PreparedRoot:
        """各 slot の凍結集合の和だけを物化し、未選択文書は取得しない。"""

        try:
            documents = snapshot_documents(snapshots)
        except DocumentSnapshotError as error:
            raise MaterializationError(str(error)) from error
        identity = _document_identity(project_id, run_id, snapshots, documents)
        try:
            contents = await self._document_inventory.list_contents(
                project_id=project_id, documents=documents
            )
            _verify_document_contents(documents, contents)
        except DocumentSnapshotError as error:
            raise MaterializationError(str(error)) from error
        accepted, skipped = await asyncio.to_thread(self._classify_documents, contents)
        generated = [_file_index(_DOCUMENTS_KEY, accepted, skipped)]
        manifest = {
            **identity,
            **_materialization_summary(accepted, skipped, generated),
        }
        return await asyncio.to_thread(
            self._write_root, workspace, accepted, generated, manifest, previous_files
        )

    async def _materialize_repository(
        self,
        *,
        workspace: RunWorkspace,
        project_id: UUID,
        run_id: UUID,
        requirement_key: str,
        binding: RepositoryBindingRef,
        metadata: RepositorySnapshotBinding,
        previous_files: Sequence[InputFileSeal],
    ) -> _PreparedRoot:
        """凍結 binding の scope 配下を input/<requirement_key>/ へ revision 固定で物化する。"""

        root_name = _materialization_root(requirement_key)
        identity = _repository_identity(project_id, run_id, requirement_key, binding)
        if self._repository_source is None:
            raise MaterializationError(
                "Repository materialization is not configured for this deployment"
            )
        try:
            async with self._repository_source.open(
                project_id=project_id, run_id=run_id, binding=binding
            ) as session:
                if (
                    session.provider != binding.provider
                    or session.binding_checksum != metadata.checksum
                    or session.scope_paths != metadata.scope_paths
                ):
                    raise MaterializationError("Repository binding changed during preparation")
                listing = await session.list_files()
                planned, skipped = self._plan_repository_files(root_name, listing)
                accepted = await self._read_repository_files(session, planned, skipped)
                commits = await session.read_history(limit=_MAX_HISTORY_ENTRIES)
                revision = session.revision
                provider = session.provider
                scope_paths = list(session.scope_paths)
                binding_checksum = session.binding_checksum
        except RepositoryClientError as error:
            # Provider 側の失敗理由は安定 code だけを引き継ぐ (URI/凭据は含めない)。
            raise MaterializationError(
                f"Repository materialization failed: {error.code}"
            ) from error
        generated = [
            _file_index(root_name, accepted, skipped),
            _history_file(root_name, commits, limit=_MAX_HISTORY_ENTRIES),
        ]
        manifest = {
            **identity,
            "requirement_key": requirement_key,
            "provider": provider,
            "kind": "repository",
            "revision": revision,
            "binding_id": str(binding.binding_id),
            "binding_checksum": binding_checksum,
            "integration_id": str(binding.integration_id),
            "scope": {"paths": scope_paths},
            **_materialization_summary(accepted, skipped, generated),
        }
        return await asyncio.to_thread(
            self._write_root, workspace, accepted, generated, manifest, previous_files
        )

    def _classify_documents(
        self, contents: Sequence[ProjectDocumentContent]
    ) -> tuple[list[_AcceptedFile], list[dict[str, str]]]:
        """物化対象と skip 対象へ分け、体积/件数上限超過は fail closed する。

        書き込み前に全件を分類し、上限超過なら 1 byte も書かずに送出する。截断した部分木で
        「file が存在しない」という誤結論を招かないため、判定は書き込みから完全に分離する。
        """

        accepted: list[_AcceptedFile] = []
        skipped: list[dict[str, str]] = []
        total_bytes = 0
        ordered = sorted(contents, key=lambda item: (item.folder, item.name))
        for content in ordered:
            relative = _document_relative(content)
            if relative is None:
                skipped.append(
                    {"path": f"{content.folder}/{content.name}", "reason": "invalid_path"}
                )
                continue
            if len(content.data) > _MAX_FILE_BYTES:
                skipped.append({"path": relative, "reason": "exceeds_file_limit"})
                continue
            usable = _usable_file(relative, content.data, content_hash=content.checksum)
            if isinstance(usable, dict):
                skipped.append(usable)
                continue
            total_bytes += len(usable.data)
            self._ensure_budget(files=len(accepted) + 1, total_bytes=total_bytes)
            accepted.append(usable)
        return accepted, skipped

    def _plan_repository_files(
        self, root_name: str, listing: RepositoryListing
    ) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
        """列挙結果から物化予定 (input/ 相対 path, repository path) と skip を決める。

        取得前に size で予算判定する。全 file を読み切ってから上限に気付くと、上限の意味
        (Worker memory と search 予算の保護) が無くなる。
        """

        planned: list[tuple[str, str]] = []
        skipped: list[dict[str, str]] = [
            {"path": f"{root_name}/{item.path}", "reason": item.reason} for item in listing.skipped
        ]
        total_bytes = 0
        for entry in sorted(listing.entries, key=lambda item: item.path):
            relative = _repository_relative(root_name, entry.path)
            if relative is None:
                skipped.append({"path": f"{root_name}/{entry.path}", "reason": "invalid_path"})
                continue
            if entry.size > _MAX_FILE_BYTES:
                skipped.append({"path": relative, "reason": "exceeds_file_limit"})
                continue
            total_bytes += entry.size
            self._ensure_budget(files=len(planned) + 1, total_bytes=total_bytes)
            planned.append((relative, entry.path))
        return planned, skipped

    async def _read_repository_files(
        self,
        session: ScopedRepositorySession,
        planned: Sequence[tuple[str, str]],
        skipped: list[dict[str, str]],
    ) -> list[_AcceptedFile]:
        """予定 file を読み、非 UTF-8 と申告外の過大 file を skip へ回す。"""

        accepted: list[_AcceptedFile] = []
        for relative, repository_path in planned:
            try:
                data = await session.read_file(repository_path, max_bytes=_MAX_FILE_BYTES)
            except RepositoryClientError as error:
                if error.code != "too_large":
                    raise
                # 列挙 size が実体と食い違う場合 (server 側の申告誤り) も截断せず skip する。
                skipped.append({"path": relative, "reason": "exceeds_file_limit"})
                continue
            usable = await asyncio.to_thread(_usable_file, relative, data)
            if isinstance(usable, dict):
                skipped.append(usable)
                continue
            accepted.append(usable)
        return accepted

    def _ensure_generated_budget(
        self, accepted: Sequence[_AcceptedFile], generated: Sequence[_AcceptedFile]
    ) -> None:
        """索引・履歴も input/ に落ちる以上、予算 (search の走査予算と同値) に数える。"""

        if any(len(item.data) > _MAX_FILE_BYTES for item in (*accepted, *generated)):
            raise MaterializationError("Generated resource file exceeds the per-file limit")
        self._ensure_budget(
            files=len(accepted) + len(generated),
            total_bytes=sum(len(item.data) for item in (*accepted, *generated)),
        )

    def _ensure_budget(self, *, files: int, total_bytes: int) -> None:
        """件数・体积の上限超過を書き込み前に fail closed する。"""

        if files > self._max_files or total_bytes > self._max_bytes:
            raise MaterializationError(
                "Materialized resource set exceeds the workspace budget; "
                "narrow the binding scope or raise the limit"
            )

    def _write_tree(self, input_dir: Path, accepted: Sequence[_AcceptedFile]) -> None:
        """受理 file を input/ 配下へ書き、書込後に只読 (0o400) へ固める。"""

        paths = {item.path for item in accepted}
        if len(paths) != len(accepted) or any(
            parent.as_posix() in paths for path in paths for parent in PurePosixPath(path).parents
        ):
            raise MaterializationError("Materialized file paths conflict")
        for item in accepted:
            write_new_file(input_dir, item.path, item.data)

    def _write_root(
        self,
        workspace: RunWorkspace,
        accepted: Sequence[_AcceptedFile],
        generated: Sequence[_AcceptedFile],
        manifest: Mapping[str, Any],
        previous_files: Sequence[InputFileSeal],
    ) -> _PreparedRoot:
        """生成時の byte を基準に全予算と回执を作り、disk から補签しない。"""

        body = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        manifest_file = _AcceptedFile(
            path=f"{manifest['requirement_key']}/{_MANIFEST_RELATIVE}",
            data=body,
            content_hash=f"sha256:{sha256_hex(body)}",
        )
        all_generated = (*generated, manifest_file)
        self._ensure_generated_budget(accepted, all_generated)
        contents = (*accepted, *all_generated)
        files = tuple(
            InputFileSeal(item.path, len(item.data), item.content_hash) for item in contents
        )
        self._ensure_total_budget((*previous_files, *files))
        prefix = input_relative(workspace)
        self._write_tree(
            workspace.root, [replace(item, path=f"{prefix}/{item.path}") for item in contents]
        )
        return _PreparedRoot(_describe(manifest), files)

    def _ensure_total_budget(self, files: Sequence[InputFileSeal]) -> None:
        """生成 manifest を含む全 root の存量を、再利用時にも同じ口径で制限する。"""

        if (
            len(files) > self._max_total_files
            or sum(item.size for item in files) > self._max_total_bytes
        ):
            raise MaterializationError("Run input exceeds the total materialization budget")
        roots: dict[str, list[InputFileSeal]] = {}
        for item in files:
            if item.size > _MAX_FILE_BYTES:
                raise MaterializationError("Input file exceeds the per-file limit")
            roots.setdefault(item.path.split("/", 1)[0], []).append(item)
        for items in roots.values():
            self._ensure_budget(files=len(items), total_bytes=sum(item.size for item in items))


def _document_identity(
    project_id: UUID,
    run_id: UUID,
    snapshots: Sequence[DocumentSnapshot],
    documents: Sequence[FrozenDocument],
) -> dict[str, Any]:
    """新規生成と再利用で同じ凍結選択・変換形式を要求する。"""

    return {
        "manifest_version": "v1",
        "preparation_version": "v2",
        "text_converter_version": "v1",
        "project_id": str(project_id),
        "run_id": str(run_id),
        "requirement_key": _DOCUMENTS_KEY,
        "provider": DOCUMENT_PROVIDER,
        "kind": "document",
        "scope": {
            "document_ids": [str(item.document_id) for item in documents],
            "requirements": {item.requirement_key: item.to_json() for item in snapshots},
        },
    }


def _repository_identity(
    project_id: UUID, run_id: UUID, key: str, binding: RepositoryBindingRef
) -> dict[str, Any]:
    """DB の binding 再検証と結び付く、物化 root の不変な由来を返す。"""

    return {
        "manifest_version": "v1",
        "preparation_version": "v2",
        "text_converter_version": "v1",
        "project_id": str(project_id),
        "run_id": str(run_id),
        "requirement_key": key,
        "kind": "repository",
        "provider": binding.provider,
        "binding_id": str(binding.binding_id),
        "integration_id": str(binding.integration_id),
    }


def _usable_file(
    relative: str, data: bytes, *, content_hash: str | None = None
) -> _AcceptedFile | dict[str, str]:
    """UTF-8 ならそのまま、xlsx なら text 化して受理する。どちらでもなければ skip 情報を返す。

    text 化した file は原本と別 path (`<原本>.txt`) に置く。原本 path を残すのは、Agent が
    「この text は設計書 X の変換結果」と辿れるようにするため (計画 §19 W5)。
    """

    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        return _AcceptedFile(
            path=relative,
            data=data,
            content_hash=content_hash or f"sha256:{sha256_hex(data)}",
        )
    if not is_textualizable(relative):
        return {"path": relative, "reason": "binary"}
    try:
        rendered = render_text(relative, data).encode("utf-8")
    except BinaryTextError:
        # 壊れた/未対応の原本は「読めない」として残す。存在しないと誤認させない。
        return {"path": relative, "reason": "unconvertible_document"}
    if len(rendered) > _MAX_FILE_BYTES:
        return {"path": relative, "reason": "conversion_exceeds_file_limit"}
    return _AcceptedFile(
        path=f"{relative}.txt",
        data=rendered,
        content_hash=f"sha256:{sha256_hex(rendered)}",
        converted_from=relative,
        source_content_hash=content_hash or f"sha256:{sha256_hex(data)}",
    )


def _file_index(
    root_name: str,
    accepted: Sequence[_AcceptedFile],
    skipped: Sequence[dict[str, str]],
) -> _AcceptedFile:
    """物化物と skip の索引 file を作る (計画 §19 W5)。

    `workspace.search` は内容検索しか持たないため、名前で file を探す idiom
    (`svn list -R | grep <name>`) はこの索引が無いと成立しない。skip した原本も併記し、
    「見つからない」と「読めなかった」を Agent が区別できるようにする。
    """

    lines = [f"# materialized files under input/{root_name}/"]
    lines.extend(_index_line(item, root_name) for item in accepted)
    lines.extend(
        f"# skipped\t{_relative_to_root(item['path'], root_name)}\t{item['reason']}"
        for item in sorted(skipped, key=lambda item: item["path"])
    )
    data = ("\n".join(lines) + "\n").encode("utf-8")
    return _AcceptedFile(
        path=f"{root_name}/{_FILE_INDEX_RELATIVE}",
        data=data,
        content_hash=f"sha256:{sha256_hex(data)}",
    )


def _index_line(item: _AcceptedFile, root_name: str) -> str:
    """索引 1 行。変換物は原本 path も併記して由来を辿れるようにする。"""

    path = _relative_to_root(item.path, root_name)
    if item.converted_from is None:
        return path
    origin = _relative_to_root(item.converted_from, root_name)
    return f"{path}\t(converted from {origin})"


def _relative_to_root(path: str, root_name: str) -> str:
    """索引内 path を物化 root からの相対にする (root 名の重複を避ける)。"""

    prefix = f"{root_name}/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def _history_file(
    root_name: str, commits: Sequence[RepositoryCommit], *, limit: int
) -> _AcceptedFile:
    """Commit 履歴 text を作る (計画 §19 W5)。

    見出しに件数上限を書く。全履歴だと誤解されると「この file は昔から変わっていない」といった
    誤結論を招くため、部分集合であることを text 自身に持たせる。
    """

    lines = [
        f"# most recent {len(commits)} commits touching the bound scope (limit {limit})",
        "# revision\tcommitted_at\tauthor\tsummary",
    ]
    lines.extend(
        f"{commit.revision}\t{commit.committed_at}\t{commit.author}\t{commit.summary}"
        for commit in commits
    )
    data = ("\n".join(lines) + "\n").encode("utf-8")
    return _AcceptedFile(
        path=f"{root_name}/{_HISTORY_RELATIVE}",
        data=data,
        content_hash=f"sha256:{sha256_hex(data)}",
    )


def _materialization_summary(
    accepted: Sequence[_AcceptedFile],
    skipped: Sequence[dict[str, str]],
    generated: Sequence[_AcceptedFile] = (),
) -> dict[str, Any]:
    """物化統計・content digest・file 一覧・生成物・skip 一覧を manifest 形式で返す。

    ``content_digest`` は資源内容 (`files`) だけから作る。索引や履歴のような platform 生成物は
    資源の同一性ではないため digest には混ぜず、再訪検証では個別 hash で確かめる。
    """

    files = [_manifest_entry(item) for item in accepted]
    return {
        "content_digest": _content_digest(files),
        "materialized": {
            "files": len(accepted),
            "bytes": sum(len(item.data) for item in accepted),
        },
        "files": files,
        "generated": [_manifest_entry(item) for item in generated],
        "skipped": list(skipped),
    }


def _manifest_entry(item: _AcceptedFile) -> dict[str, str]:
    """manifest の file entry。変換物には由来 (原本 path と hash) を併記する。"""

    entry = {"path": item.path, "content_hash": item.content_hash}
    if item.converted_from is not None:
        entry["converted_from"] = item.converted_from
        entry["source_content_hash"] = item.source_content_hash or ""
    return entry


def _content_digest(files: Sequence[Mapping[str, str]]) -> str:
    """物化物全体を一意に表す digest。再訪時の同一性検証にも使う。"""

    source = "\n".join(f"{item['path']}:{item['content_hash']}" for item in files)
    return f"sha256:{sha256_hex(source.encode('utf-8'))}"


def _describe(manifest: Mapping[str, Any]) -> MaterializedResource:
    """manifest から Agent へ案内する落点記述子を作る (計画 §19 W6)。

    初回物化と reuse の双方が同じ manifest を材料にするため、Attempt をまたいでも案内が
    ぶれない。path は `workspace.read`/`workspace.search` へそのまま渡せる表記で組む。
    """

    root_name = str(manifest.get("requirement_key") or _DOCUMENTS_KEY)
    generated = manifest.get("generated")
    generated_paths = {
        str(item.get("path"))
        for item in (generated if isinstance(generated, list) else [])
        if isinstance(item, Mapping)
    }
    history = f"{root_name}/{_HISTORY_RELATIVE}"
    revision = manifest.get("revision")
    files = manifest.get("materialized")
    return MaterializedResource(
        requirement_key=root_name,
        kind=str(manifest.get("kind") or ""),
        provider=str(manifest.get("provider") or ""),
        root=f"input/{root_name}",
        manifest_path=f"input/{root_name}/{_MANIFEST_RELATIVE}",
        index_path=f"input/{root_name}/{_FILE_INDEX_RELATIVE}",
        # 履歴は repository 一路だけが持つ。無い物を案内すると Agent が読めない path を試す。
        history_path=(f"input/{history}" if history in generated_paths else None),
        revision=str(revision) if isinstance(revision, str) else None,
        files=int(files["files"]) if isinstance(files, Mapping) and "files" in files else 0,
        skipped=len(manifest["skipped"]) if isinstance(manifest.get("skipped"), list) else 0,
    )


def _verify_materialized(
    workspace: RunWorkspace,
    root_name: str,
    *,
    expected: Mapping[str, Any],
    documents: Sequence[FrozenDocument] | None = None,
) -> Mapping[str, Any]:
    """既存物化物が manifest どおりかを検証し、その manifest を返す (docs/06 §6.4 の幂等要件)。

    RunAttempt の再試行では取得し直さず reuse するが、内容が入れ替わっていれば結論の根拠が
    変わる。欠落・改変は静かに作り直さず fail closed とする。
    """

    manifest_relative = f"{root_name}/{_MANIFEST_RELATIVE}"
    try:
        manifest = json.loads(
            read_input_file(workspace, manifest_relative, max_bytes=_MAX_FILE_BYTES)
        )
    except ValueError as error:
        raise MaterializationError("Materialized manifest could not be read") from error
    if not isinstance(manifest, dict) or any(
        manifest.get(key) != value for key, value in expected.items()
    ):
        raise MaterializationError("Materialized manifest does not belong to this Run selection")
    files, generated, skipped = (manifest.get(key) for key in ("files", "generated", "skipped"))
    if (
        not isinstance(files, list)
        or not isinstance(generated, list)
        or not isinstance(skipped, list)
    ):
        raise MaterializationError("Materialized manifest is invalid")
    if not all(
        isinstance(item, dict)
        and set(item) == {"path", "reason"}
        and all(isinstance(value, str) for value in item.values())
        for item in skipped
    ):
        raise MaterializationError("Materialized skipped entries are invalid")
    if documents is not None:
        _verify_document_members(manifest, documents)
    # 資源内容と生成物はどちらも個別 hash で確認するが、digest は資源内容だけから作る
    # (索引や履歴は資源の同一性ではない)。
    verified = [_verified_entry(workspace, root_name, item) for item in files]
    artifacts = [_verified_entry(workspace, root_name, item) for item in generated]
    paths = [item.path for item in (*verified, *artifacts)]
    generated_paths = {f"{root_name}/{_FILE_INDEX_RELATIVE}"}
    if expected["kind"] == "repository":
        generated_paths.add(f"{root_name}/{_HISTORY_RELATIVE}")
    if len(set(paths)) != len(paths) or {item.path for item in artifacts} != generated_paths:
        raise MaterializationError("Materialized manifest has unexpected or duplicate files")
    verify_tree(
        workspace.root,
        relative=f"{input_relative(workspace)}/{root_name}",
        expected_files={
            _MANIFEST_RELATIVE,
            *("/".join(relative_parts(path)[1:]) for path in paths),
        },
    )
    index = _file_index(root_name, verified, skipped)
    if next(item for item in artifacts if item.path == index.path).data != index.data:
        raise MaterializationError("Materialized index does not describe its files")
    if manifest.get("materialized") != {
        "files": len(verified),
        "bytes": sum(len(item.data) for item in verified),
    } or manifest.get("content_digest") != _content_digest(
        [_manifest_entry(item) for item in verified]
    ):
        raise MaterializationError("Materialized content digest does not match its manifest")
    return cast(Mapping[str, Any], manifest)


def _verified_entry(workspace: RunWorkspace, root_name: str, item: Any) -> _AcceptedFile:
    """manifest の 1 entry を実 file と突き合わせ、path/hash を返す。"""

    if not isinstance(item, dict) or set(item) not in (
        {"path", "content_hash"},
        {"path", "content_hash", "converted_from", "source_content_hash"},
    ):
        raise MaterializationError("Materialized manifest is invalid")
    path = item.get("path")
    content_hash = item.get("content_hash")
    if (
        not isinstance(path, str)
        or not isinstance(content_hash, str)
        or (re.fullmatch(r"sha256:[a-f0-9]{64}", content_hash) is None)
    ):
        raise MaterializationError("Materialized manifest is invalid")
    parts = relative_parts(path)
    if len(parts) < 2 or parts[0] != root_name:
        raise MaterializationError("Materialized path escapes its resource root")
    if "converted_from" in item and (
        not isinstance(item["converted_from"], str)
        or not isinstance(item["source_content_hash"], str)
    ):
        raise MaterializationError("Materialized conversion metadata is invalid")
    data = read_input_file(workspace, path, max_bytes=_MAX_FILE_BYTES)
    if f"sha256:{sha256_hex(data)}" != content_hash:
        raise MaterializationError("Materialized file does not match its manifest")
    return _AcceptedFile(
        path, data, content_hash, item.get("converted_from"), item.get("source_content_hash")
    )


def _materialization_root(requirement_key: str) -> str:
    """requirement_key を input/ 直下の安全な 1 segment へ落とす。

    key は blueprint 由来 (外部 Skill の記述) であり、path 区切りや `..` が来れば input/ の
    外へ書ける。document 用の予約名との衝突も、静かな上書きになる前に拒否する。
    """

    if (
        not requirement_key
        or requirement_key == _DOCUMENTS_KEY
        or "/" in requirement_key
        or "\\" in requirement_key
        or requirement_key in {".", "..", _PLATFORM_DIRECTORY}
        or not requirement_key.isprintable()
    ):
        raise MaterializationError("Resource requirement key is not a valid materialization root")
    return requirement_key


def _validated_document_snapshots(
    blueprint: Mapping[str, Any], project_id: UUID, snapshots: Sequence[DocumentSnapshot]
) -> tuple[DocumentSnapshot, ...]:
    """必須 slot の欠落、未宣言 slot、別 Project と不正 metadata を取得前に拒否する。"""

    requirements = blueprint.get("resource_requirements", [])
    if not isinstance(requirements, list):
        raise MaterializationError("Resource requirements are invalid")
    declared = {
        item["key"]: item
        for item in requirements
        if isinstance(item, Mapping)
        and item.get("kind") == "document"
        and isinstance(item.get("key"), str)
    }
    keys = {item.requirement_key for item in snapshots}
    if (
        len(keys) != len(snapshots)
        or keys - declared.keys()
        or any(value.get("required") and key not in keys for key, value in declared.items())
    ):
        raise MaterializationError("Run document selections do not match its requirements")
    try:
        return tuple(
            parse_document_snapshot(
                item.to_json(), project_id=project_id, requirement_key=item.requirement_key
            )
            for item in sorted(snapshots, key=lambda item: item.requirement_key)
        )
    except DocumentSnapshotError as error:
        raise MaterializationError(str(error)) from error


def _verify_document_contents(
    documents: Sequence[FrozenDocument], contents: Sequence[ProjectDocumentContent]
) -> None:
    """不正 inventory による追加・欠落・同名別 ID・byte 改変を二次検証する。"""

    by_id = {item.document_id: item for item in contents}
    if len(by_id) != len(contents) or set(by_id) != {item.document_id for item in documents}:
        raise DocumentSnapshotError("Document inventory does not match the frozen selection")
    for document in documents:
        verify_frozen_content(document, by_id[document.document_id])


def _verify_document_members(
    manifest: Mapping[str, Any], documents: Sequence[FrozenDocument]
) -> None:
    """manifest が凍結集合を過不足なく説明し、原文 hash と変換元が一致するか確認する。"""

    expected = {f"{_DOCUMENTS_KEY}/{item.path}": item for item in documents}
    seen: set[str] = set()
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise MaterializationError("Materialized document entry is invalid")
        origin = item.get("converted_from", item.get("path"))
        if not isinstance(origin, str) or origin not in expected or origin in seen:
            raise MaterializationError("Materialized document is not a frozen member")
        seen.add(origin)
        if "converted_from" in item:
            valid = (
                item.get("path") == f"{origin}.txt"
                and is_textualizable(origin)
                and (item.get("source_content_hash") == expected[origin].content_hash)
            )
        else:
            valid = item.get("content_hash") == expected[origin].content_hash
        if not valid:
            raise MaterializationError("Materialized document source does not match its snapshot")
    for item in manifest["skipped"]:
        path = item["path"]
        if (
            path not in expected
            or path in seen
            or item["reason"]
            not in {
                "binary",
                "exceeds_file_limit",
                "unconvertible_document",
                "conversion_exceeds_file_limit",
            }
        ):
            raise MaterializationError("Materialized skipped document is not a frozen member")
        if (item["reason"] == "exceeds_file_limit") != (expected[path].size > _MAX_FILE_BYTES):
            raise MaterializationError("Materialized document skip reason is inconsistent")
        seen.add(path)
    if seen != expected.keys():
        raise MaterializationError("Materialized document selection is incomplete")


def _document_relative(content: ProjectDocumentContent) -> str | None:
    """文書の folder/name を input/documents/ 配下の安全な相対 path へ落とす。

    DB は upload 時に path を検証済みだが、物化は FS へ書くため二次防衛する。`..`/絶対/空要素/
    非印字/backslash を含むものは物化せず invalid_path として skip 対象へ回す。
    """

    raw = f"{content.folder}/{content.name}" if content.folder else content.name
    return _safe_relative(_DOCUMENTS_KEY, raw)


def _repository_relative(root_name: str, path: str) -> str | None:
    """Repository 内 path を input/<requirement_key>/ 配下の安全な相対 path へ落とす。"""

    return _safe_relative(root_name, path)


def _safe_relative(root_name: str, raw: str) -> str | None:
    """物化 root 名と外部由来 path から、逃逸しない相対 path を組み立てる。"""

    if "\\" in raw:
        return None
    parts: list[str] = [root_name]
    for part in raw.split("/"):
        if part in {"", ".", ".."} or not part.isprintable():
            return None
        parts.append(part)
    if len(parts) < 2:
        return None
    return PurePosixPath(*parts).as_posix()
