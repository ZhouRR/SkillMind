"""隔離 Run workspace の UTF-8 file 読み取りと平文検索 Provider。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from projectmind.agent.evidence import EvidenceDraft
from projectmind.agent.input_workspace import read_input_file, verify_input
from projectmind.agent.materialization_storage import (
    FileReadLimitError,
    MaterializationError,
    UnsafeWorkspaceFileError,
    list_workspace_files,
    read_file,
)
from projectmind.agent.materialization_storage import (
    write_workspace_file as write_safe_workspace_file,
)
from projectmind.agent.text_window import select_line_window
from projectmind.agent.tool_gateway import (
    ProviderToolResult,
    RunToolContext,
    ToolProviderError,
)
from projectmind.artifacts.domain import MAX_ARTIFACT_BYTES, ArtifactDraft
from projectmind.core.hashing import sha256_hex

_MAX_FILE_BYTES = MAX_ARTIFACT_BYTES
_MAX_SEARCH_FILES = 500
_MAX_SEARCH_BYTES = 10_485_760
_MAX_SEARCH_ENTRIES = 5_000
_MAX_EXCERPT_CHARACTERS = 1_000
_ALLOWED_ROOT_NAMES = frozenset({"input", "workspace"})
# 書き込み面は Agent 自身の中間産物と成果物に限る。物化済み input/ は冻结证据であり書き込み対象
# にしない (計画 §19 W2)。read/search とは別語彙で、逃さないよう root 集合を明示分離する。
_WRITABLE_ROOT_NAMES = frozenset({"workspace", "output"})


class WorkspaceReadProvider:
    """Run に明示された input/workspace 内の一つの UTF-8 file を読む。"""

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """Path と symlink 境界を検証し、行 window と再現可能 Evidence を返す。"""

        relative, path = await asyncio.to_thread(
            _resolve_workspace_path,
            context,
            arguments.get("path"),
            require_file=True,
        )
        data = await asyncio.to_thread(
            _read_workspace_bytes, context, relative, path, max_bytes=_MAX_FILE_BYTES
        )
        content = _decode_content(data)
        window = select_line_window(
            content.text,
            arguments,
            subject="Workspace file",
            allow_empty=True,
        )
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "workspace",
                "path": relative,
                "content": window.content,
                "content_hash": content.checksum,
                "line_start": window.line_start,
                "line_end": window.line_end,
                "warnings": ["Content was truncated"] if window.truncated else [],
                "truncated": window.truncated,
            },
            evidence=(
                EvidenceDraft(
                    evidence_type="workspace-file",
                    source_uri=_workspace_uri(context, relative),
                    source_locator={
                        "path": relative,
                        "line_start": window.line_start,
                        "line_end": window.line_end,
                    },
                    content_hash=content.checksum,
                    excerpt=window.content[:2_000],
                    metadata={"scope": "run-workspace", "read_only": True},
                ),
            ),
        )


class WorkspaceSearchProvider:
    """Run workspace 内を bounded な literal substring で検索する。"""

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """対象 file/byte/result 数を制限し、regex や symlink traversal を受理しない。"""

        query = arguments.get("query")
        if not isinstance(query, str) or not query:
            raise ToolProviderError(
                "invalid_request", "Workspace search query is invalid", retryable=False
            )
        case_sensitive = arguments.get("case_sensitive") is True
        max_results = _max_results(arguments.get("max_results"))
        targets = await asyncio.to_thread(_search_targets, context, arguments.get("paths"))
        files, discovery_truncated = await asyncio.to_thread(_bounded_files, context, targets)
        matches: list[dict[str, Any]] = []
        evidence: list[EvidenceDraft] = []
        warnings: list[str] = ["Search discovery limit was reached"] if discovery_truncated else []
        scanned_files = 0
        scanned_bytes = 0
        truncated = discovery_truncated
        needle = query if case_sensitive else query.casefold()

        for relative, path in files:
            if scanned_files >= _MAX_SEARCH_FILES:
                warnings.append("Search file limit was reached")
                truncated = True
                break
            remaining = _MAX_SEARCH_BYTES - scanned_bytes
            if remaining <= 0:
                warnings.append("Search byte limit was reached")
                truncated = True
                break
            try:
                data = await asyncio.to_thread(
                    _read_workspace_bytes,
                    context,
                    relative,
                    path,
                    max_bytes=min(_MAX_FILE_BYTES, remaining),
                )
            except ToolProviderError as error:
                if error.code != "too_large":
                    raise
                if remaining < _MAX_FILE_BYTES:
                    warnings.append("Search byte limit was reached")
                    truncated = True
                    break
                warnings.append("One or more files exceeded the per-file limit")
                truncated = True
                continue
            # 検証・decode した実 byte を計上し、binary skip でも読取予算を返却しない。
            scanned_files += 1
            scanned_bytes += len(data)
            try:
                content = _decode_content(data)
            except ToolProviderError:
                warnings.append("One or more non-UTF-8 files were skipped")
                continue
            file_match_lines: list[int] = []
            for line_no, line in enumerate(content.text.splitlines(), start=1):
                haystack = line if case_sensitive else line.casefold()
                start = 0
                while (column := haystack.find(needle, start)) >= 0:
                    matches.append(
                        {
                            "path": relative,
                            "line": line_no,
                            "column": column + 1,
                            "excerpt": line[:_MAX_EXCERPT_CHARACTERS],
                        }
                    )
                    file_match_lines.append(line_no)
                    start = column + max(1, len(needle))
                    if len(matches) >= max_results:
                        truncated = True
                        break
                if len(matches) >= max_results:
                    break
            if file_match_lines:
                evidence.append(
                    EvidenceDraft(
                        evidence_type="workspace-search-match",
                        source_uri=_workspace_uri(context, relative),
                        source_locator={
                            "path": relative,
                            "matched_lines": sorted(set(file_match_lines)),
                        },
                        content_hash=content.checksum,
                        excerpt=next(
                            item["excerpt"] for item in matches if item["path"] == relative
                        ),
                        metadata={"scope": "run-workspace", "read_only": True},
                    )
                )
            if len(matches) >= max_results:
                warnings.append("Search result limit was reached")
                break

        if not evidence:
            # 0 件も「検索を実行した」という監査事実を残す。query 本文は保存せず hash のみとする。
            evidence.append(
                EvidenceDraft(
                    evidence_type="workspace-search",
                    source_uri=f"workspace://runs/{context.run_id}/",
                    source_locator={
                        "scope": "input-and-workspace",
                        "query_hash": f"sha256:{sha256_hex(query)}",
                    },
                    content_hash=f"sha256:{sha256_hex(b'')}",
                    metadata={"matched": False, "read_only": True},
                )
            )
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "workspace",
                "matches": matches,
                "scanned_files": scanned_files,
                "warnings": sorted(set(warnings)),
                "truncated": truncated,
            },
            evidence=tuple(evidence),
        )


class WorkspaceWriteProvider:
    """Agent の中間産物と成果物を Run workspace の workspace//output/ へ書き込む。

    書き込み面は明示的に workspace//output/ に限り、物化済み input/ を除外する (計画 §19 W2)。
    input/ は冻结证据であり、可書きにすると「内容が binding revision に対応・再現可能」という
    不変式が壊れる。書き込みは Evidence を残し、結論の可追溯性を保つ。
    """

    async def execute(
        self, context: RunToolContext, arguments: Mapping[str, Any]
    ) -> ProviderToolResult:
        """UTF-8 content を上限内で書き込み、content hash 付き Evidence を返す。"""

        content = arguments.get("content")
        if not isinstance(content, str):
            raise ToolProviderError(
                "invalid_request", "Workspace content is invalid", retryable=False
            )
        try:
            data = content.encode("utf-8")
        except UnicodeError:
            raise ToolProviderError(
                "invalid_request", "Workspace content must be UTF-8", retryable=False
            ) from None
        if len(data) > _MAX_FILE_BYTES:
            raise ToolProviderError(
                "too_large", "Workspace content exceeds the write limit", retryable=False
            )
        relative, _, target = await asyncio.to_thread(
            _resolve_writable_path, context, arguments.get("path")
        )
        # v1 と凍結済み権限の意味は変えない。v2 の output のみ書込前の原 byte を封じる。
        artifact = (
            ArtifactDraft(path=relative, content=data)
            if context.tool.capability == "workspace.write/v2" and relative.startswith("output/")
            else None
        )
        checksum = f"sha256:{sha256_hex(data)}"
        # 既知の監査形式エラーは file 変更前に拒否する。DB quota/commit は別の後段境界である。
        evidence = EvidenceDraft(
            evidence_type="workspace-write",
            source_uri=_workspace_uri(context, relative),
            source_locator={"path": relative, "bytes": len(data)},
            content_hash=checksum,
            excerpt=content[:2_000],
            metadata={"scope": "run-workspace", "read_only": False},
            artifact=artifact,
        )
        created = await asyncio.to_thread(
            _write_workspace_file, context.workspace.root, target, data
        )
        return ProviderToolResult(
            response={
                "status": "success",
                "provider": "workspace",
                "path": relative,
                "content_hash": checksum,
                "bytes_written": len(data),
                "created": created,
                "warnings": [],
            },
            evidence=(evidence,),
        )


class _TextContent:
    """一つの file の decoded text と全体 checksum。"""

    def __init__(self, text: str, checksum: str) -> None:
        """Immutable として扱う二値を保持する。"""

        self.text = text
        self.checksum = checksum


def _validated_relative(value: Any) -> PurePosixPath:
    """公開 relative path の文字列健全性を検証し、root を含む安全な相対 path を返す。

    absolute、`..`/`.`、空要素、非印字文字、backslash を拒否する。root 種別の許否は呼び出し側が
    自分の許可集合で判定する (read/search は input/workspace、write は workspace/output)。
    """

    if not isinstance(value, str) or not value or "\\" in value:
        raise ToolProviderError("invalid_request", "Workspace path is invalid", retryable=False)
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} or not part.isprintable() for part in raw_parts):
        raise ToolProviderError("invalid_request", "Workspace path is invalid", retryable=False)
    relative = PurePosixPath(value)
    if relative.is_absolute():
        raise ToolProviderError("invalid_request", "Workspace path is invalid", retryable=False)
    return relative


def _resolve_writable_path(context: RunToolContext, value: Any) -> tuple[str, Path, Path]:
    """書き込み対象を workspace//output/ の実体へ閉じ込める。対象 file は未存在でよい。

    read と違い最終要素は存在しないことがあるため strict resolve せず、各既存要素で symlink を
    拒否しつつ base 配下に組み立てる。戻り値は (公開 relative, base root, 実体 target)。
    """

    relative = _validated_relative(value)
    # 書き込みは root 配下の file を要する。裸の root (parts=1) には書けない。
    if relative.parts[0] not in _WRITABLE_ROOT_NAMES or len(relative.parts) < 2:
        raise ToolProviderError(
            "invalid_request",
            "Workspace path is outside the writable roots",
            retryable=False,
        )
    base = _verified_roots(context, _WRITABLE_ROOT_NAMES)[relative.parts[0]]
    candidate = base
    for part in relative.parts[1:]:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ToolProviderError(
                "invalid_request",
                "Workspace symbolic links are not allowed",
                retryable=False,
            )
    if candidate.exists() and not candidate.is_file():
        raise ToolProviderError(
            "invalid_request", "Workspace path is not a writable file", retryable=False
        )
    return relative.as_posix(), base, candidate


def _write_workspace_file(root: Path, target: Path, data: bytes) -> bool:
    """固定 Run root を使う安全 I/O の失敗を、公開 error へ変換する。"""

    try:
        return write_safe_workspace_file(root, target.relative_to(root).as_posix(), data)
    except (ValueError, UnsafeWorkspaceFileError) as error:
        raise ToolProviderError(
            "invalid_request", "Workspace target is not a safe file", retryable=False
        ) from error
    except MaterializationError as error:
        raise ToolProviderError(
            "unavailable", "Workspace file could not be written", retryable=False
        ) from error


def _resolve_workspace_path(
    context: RunToolContext,
    value: Any,
    *,
    require_file: bool,
) -> tuple[str, Path]:
    """公開 relative path を input/workspace の実体へ閉じ込める。"""

    relative = _validated_relative(value)
    if relative.parts[0] not in _ALLOWED_ROOT_NAMES:
        raise ToolProviderError(
            "invalid_request", "Workspace path is outside the allowed roots", retryable=False
        )
    roots = _verified_roots(context, (relative.parts[0],))
    base = roots[relative.parts[0]]
    if relative.parts[0] == "input":
        if context.workspace.input_files is None:
            raise ToolProviderError(
                "unavailable", "Run input has no trusted receipt", retryable=False
            )
        inner = "/".join(relative.parts[1:])
        index = context.workspace.input_file_index
        known = inner in index or (
            not require_file and (not inner or any(path.startswith(f"{inner}/") for path in index))
        )
        if not known:
            raise ToolProviderError("not_found", "Input path is not in this Run", retryable=False)
        return relative.as_posix(), base.joinpath(*relative.parts[1:])
    candidate = base
    for part in relative.parts[1:]:
        candidate /= part
        if candidate.is_symlink():
            raise ToolProviderError(
                "invalid_request", "Workspace symbolic links are not allowed", retryable=False
            )
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise ToolProviderError(
            "not_found", "Workspace path was not found", retryable=False
        ) from error
    if not resolved.is_relative_to(base):
        raise ToolProviderError(
            "invalid_request", "Workspace path escapes the allowed root", retryable=False
        )
    if require_file and not resolved.is_file():
        raise ToolProviderError("not_found", "Workspace file was not found", retryable=False)
    if not require_file and not (resolved.is_file() or resolved.is_dir()):
        raise ToolProviderError("not_found", "Workspace path was not found", retryable=False)
    return relative.as_posix(), resolved


def _verified_roots(
    context: RunToolContext, names: Iterable[str] = ("input", "workspace")
) -> dict[str, Path]:
    """RunWorkspace の root 関係を再検証し、cwd を security boundary として信頼しない。

    ``names`` で検証対象の root を絞る。read/search は既定の input/workspace、write は
    workspace/output を渡す。要求外の root は解決しないため、書き込み経路が input を見ない。
    """

    try:
        run_root = context.workspace.root.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise ToolProviderError(
            "unavailable", "Run workspace is unavailable", retryable=False
        ) from error
    if context.workspace.root.is_symlink() or run_root != context.workspace.root:
        raise ToolProviderError("unavailable", "Run workspace boundary is invalid", retryable=False)
    available = {
        "input": context.workspace.input_dir,
        "workspace": context.workspace.cwd,
        "output": context.workspace.output_dir,
    }
    roots: dict[str, Path] = {}
    for name in names:
        raw = available[name]
        if raw.is_symlink():
            raise ToolProviderError(
                "unavailable", "Run workspace boundary is invalid", retryable=False
            )
        try:
            resolved = raw.resolve(strict=True)
        except (FileNotFoundError, OSError) as error:
            raise ToolProviderError(
                "unavailable", "Run workspace is unavailable", retryable=False
            ) from error
        if raw != resolved or not resolved.is_dir() or not resolved.is_relative_to(run_root):
            raise ToolProviderError(
                "unavailable", "Run workspace boundary is invalid", retryable=False
            )
        roots[name] = resolved
    return roots


def _read_workspace_bytes(
    context: RunToolContext, relative: str, path: Path, *, max_bytes: int
) -> bytes:
    """input は回执と同じ byte、可変 workspace は安全な有界読取だけを返す。"""

    is_input = relative.startswith("input/")
    if is_input:
        seal = context.workspace.input_file_index.get(relative.removeprefix("input/"))
        if seal is not None and seal.size > max_bytes:
            raise ToolProviderError("too_large", "Input exceeds the read limit", retryable=False)
    try:
        if is_input:
            return read_input_file(
                context.workspace, relative.removeprefix("input/"), max_bytes=max_bytes
            )
        return read_file(
            context.workspace.root,
            path.relative_to(context.workspace.root).as_posix(),
            max_bytes=max_bytes,
        )
    except FileReadLimitError as error:
        # input の期待 size は上で確認済み。この超過は凍結後の改変であり普通の skip にしない。
        code = "unavailable" if is_input else "too_large"
        raise ToolProviderError(
            code, "Workspace file exceeds its verified limit", retryable=False
        ) from error
    except (MaterializationError, ValueError) as error:
        raise ToolProviderError(
            "unavailable", "Workspace file could not be read safely", retryable=False
        ) from error


def _decode_content(data: bytes) -> _TextContent:
    """応答と Evidence の元になる同一 byte を decode し、hash を計算する。"""

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ToolProviderError(
            "invalid_request", "Workspace file is not UTF-8 text", retryable=False
        ) from error
    return _TextContent(text, f"sha256:{sha256_hex(data)}")


def _search_targets(context: RunToolContext, value: Any) -> tuple[tuple[str, Path], ...]:
    """任意 path list または既定二 root を検索対象へ解決する。"""

    if value is None:
        roots = _verified_roots(context)
        return tuple((name, roots[name]) for name in ("input", "workspace"))
    if not isinstance(value, list) or not value:
        raise ToolProviderError(
            "invalid_request", "Workspace search paths are invalid", retryable=False
        )
    return tuple(_resolve_workspace_path(context, item, require_file=False) for item in value)


def _bounded_files(
    context: RunToolContext, targets: tuple[tuple[str, Path], ...]
) -> tuple[tuple[tuple[str, Path], ...], bool]:
    """input は完全性確認後の可信清単、workspace は descriptor 内の有界探索を使う。"""

    found: dict[str, Path] = {}
    truncated = False
    try:
        if any(relative == "input" or relative.startswith("input/") for relative, _ in targets):
            verify_input(context.workspace, content=False)
        for relative, target in targets:
            if relative == "input" or relative.startswith("input/"):
                for sealed in context.workspace.input_file_index:
                    logical = f"input/{sealed}"
                    if logical == relative or logical.startswith(f"{relative}/"):
                        found.setdefault(logical, context.workspace.input_dir / sealed)
                continue
            if target.is_file():
                found.setdefault(relative, target)
                continue
            children, limited = list_workspace_files(
                context.workspace.root,
                target.relative_to(context.workspace.root).as_posix(),
                max_files=_MAX_SEARCH_FILES + 1,
                max_entries=_MAX_SEARCH_ENTRIES,
            )
            truncated |= limited
            for child in children:
                found.setdefault(f"{relative}/{child}", target / child)
    except (MaterializationError, ValueError) as error:
        raise ToolProviderError(
            "unavailable", "Workspace search input could not be verified", retryable=False
        ) from error
    files = tuple(sorted(found.items()))
    return files[: _MAX_SEARCH_FILES + 1], truncated or len(files) > _MAX_SEARCH_FILES + 1


def _max_results(value: Any) -> int:
    """Schema 境界を再確認し、検索結果数の既定値を返す。"""

    if value is None:
        return 50
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ToolProviderError(
            "invalid_request", "Workspace max_results is invalid", retryable=False
        )
    return value


def _workspace_uri(context: RunToolContext, relative: str) -> str:
    """Credential/query を含まない論理 workspace URI を返す。"""

    return f"workspace://runs/{context.run_id}/{quote(relative, safe='/')}"
