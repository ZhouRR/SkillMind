"""冻结した ResourceBinding を Run workspace の input/ へ只読物化する (計画 §19 W3/W4)。

Agent は物化済みの input/ を既存の workspace.search/read で自走発見する。凭据は物化段階の
Provider 境界内でのみ解決され、Agent 上下文・日志・manifest には決して現れない。物化対象は
二路: document 一路 (計画 §19.3 D-W3=a) は Project の全文書を input/documents/ へ、repository
一路 (§19 W4) は Run に凍結された binding の scope 配下を revision 固定で
input/<requirement_key>/ へ落とす。いずれも上限超過は截断せず fail closed とし、読めなかった
file は manifest.skipped に必ず残す (「読めない」を「存在しない」と誤認させないため)。

計画 §19 W5 で三つの増分を加えた: ① `.projectmind/files.txt` (物化物と skip の索引。名前で
file を探す idiom (`svn list | grep`) を `workspace.search` で成立させる)、② `.projectmind/
history.txt` (repository の commit 履歴。新 capability を足さずに「いつ誰が触ったか」を渡す)、
③ 二進設計書 (xlsx/xlsm/docx) の platform 側 text 化 (Agent は text だけを見る)。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID

from projectmind.agent.binary_text import (
    BinaryTextError,
    is_textualizable,
    render_text,
)
from projectmind.agent.domain import MaterializedResource, RunWorkspace
from projectmind.agent.repository_client import (
    RepositoryClientError,
    RepositoryCommit,
    RepositoryListing,
)
from projectmind.agent.repository_source import (
    RepositoryBindingRef,
    RepositorySnapshotSource,
    ScopedRepositorySession,
)
from projectmind.core.hashing import sha256_hex
from projectmind.documents.source import ProjectDocumentContent, ProjectDocumentInventory

# 物化した個別 file の上限。search の per-file 予算 (workspace_provider._MAX_FILE_BYTES) と揃え、
# 「物化したのに search が飛ばす」死角を作らない。超過 file は skip し manifest に理由を残す。
_MAX_FILE_BYTES = 1_048_576

# document 一路の固定物化先。option (a) は全 Project 文書を一箇所へ集約するため、requirement_key
# ごとに分けない (document 要求の key は本 option では区別に使わない)。
_DOCUMENTS_KEY = "documents"
_PLATFORM_DIRECTORY = ".projectmind"
_MANIFEST_RELATIVE = f"{_PLATFORM_DIRECTORY}/manifest.json"
_FILE_INDEX_RELATIVE = f"{_PLATFORM_DIRECTORY}/files.txt"
_HISTORY_RELATIVE = f"{_PLATFORM_DIRECTORY}/history.txt"

# 履歴の件数上限。全履歴ではなく「直近 N 件」という定義の明確な部分集合にする (件数は
# history.txt の見出しに書き、Agent が「これで全部」と誤解しないようにする)。
_MAX_HISTORY_ENTRIES = 200


class MaterializationError(RuntimeError):
    """物化が安全に完了できないことを表す fail-closed 信号。

    体积上限超過や書き込み失敗など、部分木を残すと Agent が「不完全なのに完全に見える」樹から
    誤結論を導く状況で送出する。呼出側は Run を明確な理由で失敗させ、截断はしない。
    """


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


class WorkspaceMaterializer:
    """冻结した資源を Run 準備段階で input/ へ只読物化する単一実装。"""

    def __init__(
        self,
        *,
        document_inventory: ProjectDocumentInventory,
        max_bytes: int,
        max_files: int,
        repository_source: RepositorySnapshotSource | None = None,
    ) -> None:
        """文書列挙 port、repository session source と体积/件数上限を保持する。

        ``repository_source`` 未注入 (offline/未配線) の環境では repository 要求を物化できない
        ため、宣言された binding があれば fail closed とする。黙って空の樹を渡すと、Agent は
        「対象 file が存在しない」と誤結論する。
        """

        if max_bytes < 1 or max_files < 1:
            raise ValueError("Materialization limits must be positive")
        self._document_inventory = document_inventory
        self._repository_source = repository_source
        self._max_bytes = max_bytes
        self._max_files = max_files

    async def materialize(
        self,
        *,
        workspace: RunWorkspace,
        project_id: UUID,
        run_id: UUID,
        blueprint: Mapping[str, Any],
        repository_bindings: Mapping[str, RepositoryBindingRef] | None = None,
    ) -> tuple[MaterializedResource, ...]:
        """Blueprint が宣言した資源種別ごとに物化し、その落点を返す。

        Run 単位で冪等: manifest が既に在れば再取得せず、内容 hash の一致だけを検証して reuse
        する (docs/06 §6.4)。不一致は静かに作り直さず fail closed とし、Attempt 間で内容が
        入れ替わっていないことを保証する。

        戻り値は Brief と prompt が Agent へ「どこに何を置いたか」を伝えるための唯一の材料
        (計画 §19 W6)。reuse 経路でも同じ記述子を返し、初回と再試行で案内が食い違わないようにする。
        """

        materialized: list[MaterializedResource] = []
        if _declares_document_requirement(blueprint):
            materialized.append(
                await self._materialize_documents(workspace=workspace, project_id=project_id)
            )
        for requirement_key, binding in sorted((repository_bindings or {}).items()):
            materialized.append(
                await self._materialize_repository(
                    workspace=workspace,
                    project_id=project_id,
                    run_id=run_id,
                    requirement_key=requirement_key,
                    binding=binding,
                )
            )
        return tuple(materialized)

    async def _materialize_documents(
        self, *, workspace: RunWorkspace, project_id: UUID
    ) -> MaterializedResource:
        """Project の全文書を input/documents/ へ物化する。"""

        destination = workspace.input_dir / _DOCUMENTS_KEY
        if _manifest_path(destination).exists():
            return _describe(_verify_materialized(workspace.input_dir, destination))
        contents = await self._document_inventory.list_contents(project_id=project_id)
        accepted, skipped = self._classify_documents(contents)
        generated = [_file_index(_DOCUMENTS_KEY, accepted, skipped)]
        self._ensure_generated_budget(accepted, generated)
        self._write_tree(workspace.input_dir, [*accepted, *generated])
        manifest = {
            "requirement_key": _DOCUMENTS_KEY,
            "provider": "project-documents",
            "kind": "document",
            "scope": {"all_project_documents": True},
            **_materialization_summary(accepted, skipped, generated),
        }
        self._write_manifest(workspace.input_dir, destination, manifest=manifest)
        return _describe(manifest)

    async def _materialize_repository(
        self,
        *,
        workspace: RunWorkspace,
        project_id: UUID,
        run_id: UUID,
        requirement_key: str,
        binding: RepositoryBindingRef,
    ) -> MaterializedResource:
        """凍結 binding の scope 配下を input/<requirement_key>/ へ revision 固定で物化する。"""

        root_name = _materialization_root(requirement_key)
        destination = workspace.input_dir / root_name
        if _manifest_path(destination).exists():
            return _describe(_verify_materialized(workspace.input_dir, destination))
        if self._repository_source is None:
            raise MaterializationError(
                "Repository materialization is not configured for this deployment"
            )
        try:
            async with self._repository_source.open(
                project_id=project_id, run_id=run_id, binding=binding
            ) as session:
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
        self._ensure_generated_budget(accepted, generated)
        self._write_tree(workspace.input_dir, [*accepted, *generated])
        manifest = {
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
        self._write_manifest(workspace.input_dir, destination, manifest=manifest)
        return _describe(manifest)

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
            {"path": f"{root_name}/{item.path}", "reason": item.reason}
            for item in listing.skipped
        ]
        total_bytes = 0
        for entry in sorted(listing.entries, key=lambda item: item.path):
            relative = _repository_relative(root_name, entry.path)
            if relative is None:
                skipped.append(
                    {"path": f"{root_name}/{entry.path}", "reason": "invalid_path"}
                )
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
            usable = _usable_file(relative, data)
            if isinstance(usable, dict):
                skipped.append(usable)
                continue
            accepted.append(usable)
        return accepted

    def _ensure_generated_budget(
        self, accepted: Sequence[_AcceptedFile], generated: Sequence[_AcceptedFile]
    ) -> None:
        """索引・履歴も input/ に落ちる以上、予算 (search の走査予算と同値) に数える。"""

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

        base = input_dir.resolve(strict=True)
        for item in accepted:
            target = base / item.path
            parent = target.parent
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            resolved_parent = parent.resolve(strict=True)
            if not resolved_parent.is_dir() or not resolved_parent.is_relative_to(base):
                raise MaterializationError("Materialized path escapes the input root")
            if target.is_symlink():
                raise MaterializationError("Materialized path must not be a symbolic link")
            descriptor = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
            )
            try:
                os.write(descriptor, item.data)
            finally:
                os.close(descriptor)
            # 物化内容は冻结证据。書込後に只読化し、内容が binding 時点に対応する不変式を守る。
            os.chmod(target, 0o400)

    def _write_manifest(
        self, input_dir: Path, destination: Path, *, manifest: Mapping[str, Any]
    ) -> None:
        """物化清单を書く。skipped は「読めない != 存在しない」を Agent に伝える鍵。"""

        manifest_dir = destination / PurePosixPath(_MANIFEST_RELATIVE).parent
        manifest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        body = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        manifest_path = _manifest_path(destination)
        if not manifest_path.resolve().is_relative_to(input_dir.resolve(strict=True)):
            raise MaterializationError("Materialized manifest escapes the input root")
        descriptor = os.open(
            manifest_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o400
        )
        try:
            os.write(descriptor, body)
        finally:
            os.close(descriptor)


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


def _verify_materialized(input_dir: Path, destination: Path) -> Mapping[str, Any]:
    """既存物化物が manifest どおりかを検証し、その manifest を返す (docs/06 §6.4 の幂等要件)。

    RunAttempt の再試行では取得し直さず reuse するが、内容が入れ替わっていれば結論の根拠が
    変わる。欠落・改変は静かに作り直さず fail closed とする。
    """

    manifest_path = _manifest_path(destination)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MaterializationError("Materialized manifest could not be read") from error
    files = manifest.get("files") if isinstance(manifest, dict) else None
    generated = manifest.get("generated", []) if isinstance(manifest, dict) else None
    if not isinstance(files, list) or not isinstance(generated, list):
        raise MaterializationError("Materialized manifest is invalid")
    base = input_dir.resolve(strict=True)
    # 資源内容と生成物はどちらも個別 hash で確認するが、digest は資源内容だけから作る
    # (索引や履歴は資源の同一性ではない)。
    verified = [_verified_entry(base, item) for item in files]
    for item in generated:
        _verified_entry(base, item)
    if manifest.get("content_digest") != _content_digest(verified):
        raise MaterializationError("Materialized content digest does not match its manifest")
    return cast(Mapping[str, Any], manifest)


def _verified_entry(base: Path, item: Any) -> dict[str, str]:
    """manifest の 1 entry を実 file と突き合わせ、path/hash を返す。"""

    if not isinstance(item, dict):
        raise MaterializationError("Materialized manifest is invalid")
    path = item.get("path")
    content_hash = item.get("content_hash")
    if not isinstance(path, str) or not isinstance(content_hash, str):
        raise MaterializationError("Materialized manifest is invalid")
    try:
        data = (base / path).read_bytes()
    except OSError as error:
        raise MaterializationError("Materialized file is missing") from error
    if f"sha256:{sha256_hex(data)}" != content_hash:
        raise MaterializationError("Materialized file does not match its manifest")
    return {"path": path, "content_hash": content_hash}


def _manifest_path(destination: Path) -> Path:
    """物化先 directory の manifest path を返す。"""

    return destination / _MANIFEST_RELATIVE


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
        or requirement_key in {".", ".."}
        or not requirement_key.isprintable()
    ):
        raise MaterializationError("Resource requirement key is not a valid materialization root")
    return requirement_key


def _declares_document_requirement(blueprint: Mapping[str, Any]) -> bool:
    """Blueprint の resource_requirements に document 種別が在るかを判定する。"""

    requirements = blueprint.get("resource_requirements")
    if not isinstance(requirements, list):
        return False
    return any(
        isinstance(item, Mapping) and item.get("kind") == "document" for item in requirements
    )


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
