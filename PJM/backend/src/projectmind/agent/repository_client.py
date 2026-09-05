"""Git/SVN の実クライアントを subprocess 境界として実装する (計画 §19 W4)。

資源快照の物化と `repository.read/v1` Provider は、外部 repository へ触れる唯一の経路として
本 module を共有する。Agent はここへ到達せず、URI・凭据・作業 copy はすべて Provider 境界の
内側に留まる。凭据を argv へ置かないのは `/proc/<pid>/cmdline` が同一 host の他 process から
読めるため。git は環境変数経由で Authorization header を注入し、svn は stdin で password を
渡す。子 process へは最小限の環境変数だけを渡し、Worker が持つ DB URL や KEK を継承させない。
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

from projectmind.integrations.domain import REPOSITORY_URI_SCHEMES

# 単一 command の stdout 上限。列挙は scope で有界だが、壊れた/敵対的な server 応答から
# Worker の memory を守る最終防衛線として固定する。
_MAX_COMMAND_OUTPUT_BYTES = 33_554_432
# 列挙 entry 数の parse 上限。物化予算 (max_files) より十分大きく、予算判定は materializer 側の
# fail closed に任せる。ここは parse 段階の暴走だけを止める。
_MAX_LISTING_ENTRIES = 100_000

# 凭据に user 名が含まれないときの既定 user 名。token 認証 (GitHub/GitLab 系) は user 名を無視
# するため、token だけを登録した SecretReference でも HTTP Basic を組める。
_DEFAULT_CREDENTIAL_USERNAME = "x-access-token"

# Revision 式に許す文字。先頭 `-` を排除して option 誤認を防ぎ、shell も介さないため注入面は無い。
_REVISION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_MAX_REPOSITORY_PATH_LENGTH = 4096

# Platform が作る commit の固定 identity。実行者個人ではなく platform を著者にすることで、
# 「誰が承認したか」は Approval 記録、「何が platform 由来か」は履歴、と責務を分ける。
_COMMIT_AUTHOR_NAME = "ProjectMind"
_COMMIT_AUTHOR_EMAIL = "projectmind@projectmind.local"

# Branch 名に許す文字。予約 namespace の強制は effects 層 (policy) が行い、ここでは ref として
# 安全かどうかだけを見る。
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")

# svn の branch 置き場。平台固定の約定 (計画 §20 D5)。repository root は `svn info` の応答から
# 取るため、trunk 直下か仓库根かを推測する必要はない。
SVN_BRANCH_ROOT = "branches/projectmind"

# stderr 本文は Agent へ返さない (URI や server 応答が混ざる)。分類だけを marker で行う。
_NOT_FOUND_MARKERS = (
    b"not found",
    b"does not exist",
    # git は「その revision にその path が無い」を object 名の不正として報告する。
    # not_found へ分類しないと、削除済み file の read-back が接続失敗と区別できない。
    b"not a valid object name",
    b"unable to find",
    b"non-existent",
    b"unknown revision",
    b"no such",
)
_AUTHENTICATION_MARKERS = (
    b"authorization failed",
    b"authentication failed",
    b"could not read username",
    b"could not read password",
    b"access denied",
    b"403 forbidden",
)


class RepositoryClientError(RuntimeError):
    """Repository access の失敗を、凭据を含まない安定 code で表す。"""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        """Agent と manifest へ出しても安全な code/message だけを保持する。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class RepositoryCredential:
    """Repository 認証に使う user 名と secret。secret は repr へ出さない。"""

    username: str
    # 例外 traceback や debug 出力へ secret が混入する経路を塞ぐ。
    secret: str = field(repr=False)

    @classmethod
    def from_material(cls, material: str) -> RepositoryCredential:
        """SecretReference 本文を `username:secret` として解釈する。

        colon が無ければ token とみなし user 名を補う。password 側の colon を壊さないよう
        最初の colon だけで分割する。Integration config に user 名 field を増やさずに済み、
        既存の SecretReference 登録経路 (ENVIRONMENT/FILE/MANAGED) をそのまま使える。
        """

        username, separator, secret = material.partition(":")
        if separator and username and secret:
            return cls(username=username, secret=secret)
        return cls(username=_DEFAULT_CREDENTIAL_USERNAME, secret=material)


@dataclass(frozen=True, slots=True)
class RepositoryFileEntry:
    """物化候補となる 1 file の path と byte 数。"""

    path: str
    size: int


@dataclass(frozen=True, slots=True)
class RepositorySkippedEntry:
    """内容として取り込めない entry と理由 (manifest.skipped の材料)。

    「platform が読めない」を「存在しない」と誤認させないため、列挙段階の除外も必ず残す。
    """

    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class RepositoryListing:
    """一つの revision と scope に対する列挙結果。"""

    entries: tuple[RepositoryFileEntry, ...]
    skipped: tuple[RepositorySkippedEntry, ...]


@dataclass(frozen=True, slots=True)
class RepositoryCommit:
    """履歴 1 件の非機密 metadata。

    author は表示名だけを持ち、mail address は取らない。Run workspace へ落ちる text に個人の
    連絡先を増やす利益が無く、変更の追跡には表示名で足りる。
    """

    revision: str
    committed_at: str
    author: str
    summary: str


class RepositorySession(Protocol):
    """解決済み revision に固定された読み取り session。"""

    @property
    def provider(self) -> str:
        """`git` / `svn` のいずれかを返す。"""

        ...

    @property
    def revision(self) -> str:
        """解決済みの具体 revision (git は commit SHA、svn は revision 番号)。"""

        ...

    async def list_files(self, paths: Sequence[str]) -> RepositoryListing:
        """Scope path 配下の file を列挙する。"""

        ...

    async def read_file(self, path: str, *, max_bytes: int) -> bytes:
        """1 file の raw bytes を上限付きで読む。"""

        ...

    async def read_history(
        self, paths: Sequence[str], *, limit: int
    ) -> tuple[RepositoryCommit, ...]:
        """Scope path に触れた commit を新しい順に最大 limit 件返す。"""

        ...


class RepositoryClient(Protocol):
    """Repository 種別ごとの session を開く port。"""

    def open(
        self,
        *,
        uri: str,
        revision: str,
        credential: RepositoryCredential | None,
    ) -> AbstractAsyncContextManager[RepositorySession]:
        """指定 revision に固定した session を開く。"""

        ...


class GitCommandRepositoryClient:
    """`git` command で clone し、ls-tree/cat-file で読む本番 client。"""

    def __init__(self, *, command_timeout_seconds: int, executable: str = "git") -> None:
        """1 command あたりの timeout と実行 file を固定する。"""

        if command_timeout_seconds < 1:
            raise ValueError("Repository command timeout must be positive")
        self._timeout_seconds = command_timeout_seconds
        self._executable = executable

    @asynccontextmanager
    async def open(
        self,
        *,
        uri: str,
        revision: str,
        credential: RepositoryCredential | None,
    ) -> AsyncIterator[RepositorySession]:
        """一時 directory へ clone し、revision を解決した session を貸し出す。

        作業 copy は Run workspace の外に置き、session 終了時に破棄する。Agent から見える場所へ
        置くと `.git/config` や全 revision の内容が scope 裁剪を迂回して露出する。

        clone は任意 revision を解決できるよう履歴を絞らない (shallow/partial にすると branch 名
        以外の指定が解決できない)。大規模 repository では時間と一時領域を要するため、command
        timeout が最終防衛線となる。cache や shallow 化は §19 W5 以降の最適化とする。
        """

        _validate_uri("git", uri)
        _validate_revision(revision)
        with tempfile.TemporaryDirectory(prefix="projectmind-git-") as workspace:
            root = Path(workspace)
            home = root / "home"
            home.mkdir(mode=0o700)
            checkout = root / "repository"
            environment = _git_environment(home=home, credential=credential)
            await _run_command(
                [
                    self._executable,
                    "clone",
                    "--quiet",
                    "--no-checkout",
                    "--no-tags",
                    uri,
                    str(checkout),
                ],
                environment=environment,
                timeout_seconds=self._timeout_seconds,
            )
            resolved = await _run_command(
                [
                    self._executable,
                    "-C",
                    str(checkout),
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"{revision}^{{commit}}",
                ],
                environment=environment,
                timeout_seconds=self._timeout_seconds,
            )
            commit = resolved.decode("utf-8", errors="replace").strip()
            if not commit:
                raise RepositoryClientError(
                    "not_found", "Repository revision was not found", retryable=False
                )
            yield _GitRepositorySession(
                executable=self._executable,
                checkout=checkout,
                revision=commit,
                environment=environment,
                timeout_seconds=self._timeout_seconds,
            )


    @asynccontextmanager
    async def open_writable(
        self,
        *,
        uri: str,
        revision: str,
        credential: RepositoryCredential | None,
    ) -> AsyncIterator[GitWriteSession]:
        """Base revision へ checkout した使い捨て作業 copy を貸し出す (計画 §20 R2/D3)。

        Agent の `input/` 物化樹は使わない。あれは 0o400 の冻结证据であり、書けるようにすると
        「内容が binding revision に対応する」不変式が壊れる。また物化は scope 裁剪済みで、
        patch が触れる file が物化されていない (skip/予算外) 可能性もある。
        """

        _validate_uri("git", uri)
        _validate_revision(revision)
        with tempfile.TemporaryDirectory(prefix="projectmind-git-write-") as workspace:
            root = Path(workspace)
            home = root / "home"
            home.mkdir(mode=0o700)
            checkout = root / "repository"
            environment = _git_environment(home=home, credential=credential)
            await _run_command(
                [self._executable, "clone", "--quiet", "--no-tags", uri, str(checkout)],
                environment=environment,
                timeout_seconds=self._timeout_seconds,
            )
            resolved = await _run_command(
                [
                    self._executable,
                    "-C",
                    str(checkout),
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"{revision}^{{commit}}",
                ],
                environment=environment,
                timeout_seconds=self._timeout_seconds,
            )
            base = resolved.decode("utf-8", errors="replace").strip()
            if not base:
                raise RepositoryClientError(
                    "not_found", "Repository base revision was not found", retryable=False
                )
            await _run_command(
                [self._executable, "-C", str(checkout), "checkout", "--quiet", "--detach", base],
                environment=environment,
                timeout_seconds=self._timeout_seconds,
            )
            yield GitWriteSession(
                executable=self._executable,
                checkout=checkout,
                base_revision=base,
                environment=environment,
                timeout_seconds=self._timeout_seconds,
            )


class _GitRepositorySession:
    """Clone 済み作業 copy に対する読み取り実装。"""

    def __init__(
        self,
        *,
        executable: str,
        checkout: Path,
        revision: str,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> None:
        """解決済み commit と実行環境を保持する。"""

        self._executable = executable
        self._checkout = checkout
        self._revision = revision
        self._environment = environment
        self._timeout_seconds = timeout_seconds

    @property
    def provider(self) -> str:
        """Provider 名を返す。"""

        return "git"

    @property
    def revision(self) -> str:
        """解決済み commit SHA を返す。"""

        return self._revision

    async def list_files(self, paths: Sequence[str]) -> RepositoryListing:
        """Scope path 配下の blob を列挙し、symlink/submodule は skip として残す。"""

        for path in paths:
            _validate_repository_path(path)
        if not paths:
            return RepositoryListing(entries=(), skipped=())
        raw = await _run_command(
            [
                self._executable,
                "-C",
                str(self._checkout),
                "ls-tree",
                "-r",
                "-z",
                "--long",
                self._revision,
                "--",
                *paths,
            ],
            environment=self._environment,
            timeout_seconds=self._timeout_seconds,
        )
        entries: list[RepositoryFileEntry] = []
        skipped: list[RepositorySkippedEntry] = []
        for record in raw.split(b"\0"):
            if not record:
                continue
            if len(entries) + len(skipped) >= _MAX_LISTING_ENTRIES:
                raise RepositoryClientError(
                    "too_large", "Repository listing exceeds the entry limit", retryable=False
                )
            metadata, separator, raw_path = record.partition(b"\t")
            if not separator:
                continue
            fields = metadata.split()
            if len(fields) < 3:
                continue
            mode = fields[0].decode("ascii", errors="replace")
            kind = fields[1].decode("ascii", errors="replace")
            path = raw_path.decode("utf-8", errors="replace")
            if kind == "commit":
                # Submodule は別 repository であり binding の scope 外。存在だけ伝える。
                skipped.append(RepositorySkippedEntry(path=path, reason="submodule"))
                continue
            if kind != "blob":
                continue
            if mode == "120000":
                # Symlink は追うと scope 外の実体へ出るため内容を取り込まない。
                skipped.append(RepositorySkippedEntry(path=path, reason="symlink"))
                continue
            entries.append(RepositoryFileEntry(path=path, size=_entry_size(fields[3:])))
        return RepositoryListing(entries=tuple(entries), skipped=tuple(skipped))

    async def read_file(self, path: str, *, max_bytes: int) -> bytes:
        """`<commit>:<path>` の blob を上限付きで読む。"""

        _validate_repository_path(path)
        return await _run_command(
            [
                self._executable,
                "-C",
                str(self._checkout),
                "cat-file",
                "blob",
                f"{self._revision}:{path}",
            ],
            environment=self._environment,
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=max_bytes,
        )

    async def read_history(
        self, paths: Sequence[str], *, limit: int
    ) -> tuple[RepositoryCommit, ...]:
        """凍結 commit から遡り、scope path に触れた commit を新しい順に返す。

        `-- <paths>` を付けるのは scope 外 file の変更履歴を混ぜないため。author は表示名のみ
        (`%an`)、mail address (`%ae`) は取得しない。
        """

        for path in paths:
            _validate_repository_path(path)
        if not paths or limit < 1:
            return ()
        raw = await _run_command(
            [
                self._executable,
                "-C",
                str(self._checkout),
                "log",
                f"--max-count={limit}",
                "--date=iso-strict",
                "--pretty=format:%H%x1f%ad%x1f%an%x1f%s",
                self._revision,
                "--",
                *paths,
            ],
            environment=self._environment,
            timeout_seconds=self._timeout_seconds,
        )
        commits: list[RepositoryCommit] = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            fields = line.split("\x1f")
            if len(fields) != 4:
                continue
            commits.append(
                RepositoryCommit(
                    revision=fields[0],
                    committed_at=fields[1],
                    author=fields[2],
                    summary=fields[3],
                )
            )
        return tuple(commits)


class GitWritableRepositoryClient(Protocol):
    """承認済み変更を落とすための書き込み session を開く port (計画 §20 R2)。

    読取 port (`RepositoryClient`) と分けるのは、書ける client が読取経路へ注入される事故を
    型で防ぐため。
    """

    def open_writable(
        self,
        *,
        uri: str,
        revision: str,
        credential: RepositoryCredential | None,
    ) -> AbstractAsyncContextManager[GitWriteSession]:
        """Base revision へ checkout した作業 copy を貸し出す。"""

        ...


class GitWriteSession:
    """Base commit へ checkout 済みの作業 copy に対する書き込み session (計画 §20 R2)。

    読取 session と別 class にするのは、書ける session が読取経路へ紛れ込まないようにするため。
    push 先は呼出側が渡す branch のみで、既存 branch の上書きは行わない (衝突は呼出側が判定)。
    """

    def __init__(
        self,
        *,
        executable: str,
        checkout: Path,
        base_revision: str,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> None:
        """作業 copy、解決済み base commit と実行環境を保持する。"""

        self._executable = executable
        self._checkout = checkout
        self._base_revision = base_revision
        self._environment = environment
        self._timeout_seconds = timeout_seconds

    @property
    def base_revision(self) -> str:
        """解決済みの base commit SHA を返す。"""

        return self._base_revision

    async def remote_branch_head(self, branch: str) -> str | None:
        """Remote の branch 先頭 commit を返す。未作成なら None。"""

        _validate_branch(branch)
        raw = await self._run(["ls-remote", "origin", f"refs/heads/{branch}"])
        line = raw.decode("utf-8", errors="replace").strip()
        return line.split("\t")[0] if line else None

    async def read_file_at(self, revision: str, path: str, *, max_bytes: int) -> bytes | None:
        """指定 commit の file を読む。存在しなければ None (REMOVE の read-back に使う)。"""

        _validate_revision(revision)
        _validate_repository_path(path)
        try:
            return await self._run(
                ["cat-file", "blob", f"{revision}:{path}"], max_output_bytes=max_bytes
            )
        except RepositoryClientError as error:
            if error.code == "not_found":
                return None
            raise

    async def fetch_branch(self, branch: str) -> str:
        """Remote から branch を取り直し、その先頭 commit を返す (read-back の起点)。"""

        _validate_branch(branch)
        await self._run(["fetch", "--quiet", "origin", f"refs/heads/{branch}"])
        raw = await self._run(["rev-parse", "--verify", "--quiet", "FETCH_HEAD"])
        revision = raw.decode("utf-8", errors="replace").strip()
        if not revision:
            raise RepositoryClientError(
                "not_found", "Repository branch was not found", retryable=False
            )
        return revision

    async def commit_and_push(
        self,
        *,
        branch: str,
        files: Mapping[str, str | None],
        message: str,
    ) -> str:
        """Base から branch を作り、指定 file を書いて commit し push する。

        author/committer は platform 固定 identity とする。実行した人間の名前を commit へ
        書くと、承認者と実装者の区別が履歴上あいまいになる (承認記録は Approval 側が持つ)。
        """

        _validate_branch(branch)
        if not files:
            raise RepositoryClientError(
                "invalid_request", "Repository change set is empty", retryable=False
            )
        await self._run(["checkout", "--quiet", "-B", branch, self._base_revision])
        for path, content in sorted(files.items()):
            _validate_repository_path(path)
            target = self._resolve_writable(path)
            if content is None:
                # 存在しない file の削除要求は「既に無い」ため成功扱い (冪等)。
                if target.is_file():
                    target.unlink()
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        await self._run(["add", "--all", "--", *sorted(files)])
        await self._run(
            [
                "-c",
                f"user.name={_COMMIT_AUTHOR_NAME}",
                "-c",
                f"user.email={_COMMIT_AUTHOR_EMAIL}",
                "commit",
                "--quiet",
                "--message",
                message,
            ]
        )
        raw = await self._run(["rev-parse", "--verify", "HEAD"])
        commit = raw.decode("utf-8", errors="replace").strip()
        # 新規 branch への push のみ。非 fast-forward は git 自身が拒否し、既存 branch を
        # 巻き戻す余地を残さない (--force 系は使わない)。
        await self._run(["push", "--quiet", "origin", f"{branch}:refs/heads/{branch}"])
        return commit

    def _resolve_writable(self, path: str) -> Path:
        """作業 copy 内の書き込み先を解決し、外へ出る path を拒否する。"""

        base = self._checkout.resolve(strict=True)
        target = (base / path).resolve(strict=False)
        if not target.is_relative_to(base) or target.is_symlink():
            raise RepositoryClientError(
                "invalid_request", "Repository path escapes the working copy", retryable=False
            )
        return target

    async def _run(
        self, arguments: Sequence[str], *, max_output_bytes: int = _MAX_COMMAND_OUTPUT_BYTES
    ) -> bytes:
        """作業 copy を対象に git を実行する。"""

        return await _run_command(
            [self._executable, "-C", str(self._checkout), *arguments],
            environment=self._environment,
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=max_output_bytes,
        )


class SvnWritableRepositoryClient(Protocol):
    """承認済み変更を svn へ落とすための書き込み session を開く port (計画 §20 R4b)。"""

    def open_writable(
        self,
        *,
        uri: str,
        revision: str,
        credential: RepositoryCredential | None,
    ) -> AbstractAsyncContextManager[SvnWriteSession]:
        """Base revision を解決した書き込み session を貸し出す。"""

        ...


class SvnWriteSession:
    """svn の作業 copy に対する書き込み session (計画 §20 R4b)。

    svn の「branch」は server 側 copy であり、その置き場は仓库布局に依存する。ProjectMind は
    **平台固定の約定**として `<repository root>/branches/projectmind/<名前>` を使う (root は
    `svn info` が返す値であり推測ではない)。`branches/` が無い repository では `--parents` が
    作る。direct mode では copy を作らず、束縛 URI へ直接 commit する。
    """

    def __init__(
        self,
        *,
        executable: str,
        uri: str,
        base_revision: str,
        repository_root: str,
        config_dir: Path,
        environment: Mapping[str, str],
        credential: RepositoryCredential | None,
        timeout_seconds: int,
    ) -> None:
        """接続情報、解決済み base revision、仓库 root を保持する。"""

        self._executable = executable
        self._uri = uri.rstrip("/")
        self._base_revision = base_revision
        self._repository_root = repository_root.rstrip("/")
        self._config_dir = config_dir
        self._environment = environment
        self._credential = credential
        self._timeout_seconds = timeout_seconds

    @property
    def base_revision(self) -> str:
        """解決済み base revision 番号を返す。"""

        return self._base_revision

    def branch_url(self, name: str) -> str:
        """平台固定の約定で branch の絶対 URL を組む。"""

        _validate_branch(name)
        return f"{self._repository_root}/{SVN_BRANCH_ROOT}/{quote(name, safe='/')}"

    async def path_exists(self, url: str) -> bool:
        """指定 URL が存在するかを返す。"""

        try:
            await self._run(["info", "--xml", "--depth", "empty", url])
        except RepositoryClientError as error:
            if error.code == "not_found":
                return False
            raise
        return True

    async def read_file_at(self, url: str, path: str, *, max_bytes: int) -> bytes | None:
        """URL 配下の file を読む。存在しなければ None。"""

        _validate_repository_path(path)
        target = f"{url}/{quote(path, safe='/')}"
        try:
            return await self._run(["cat", target], max_output_bytes=max_bytes)
        except RepositoryClientError as error:
            if error.code == "not_found":
                return None
            raise

    async def head_revision(self, url: str) -> str:
        """指定 URL の現在 revision を返す。"""

        raw = await self._run(["info", "--xml", "--depth", "empty", url])
        entry = _svn_element(raw).find("entry")
        revision = entry.get("revision") if entry is not None else None
        if not revision or not revision.isdigit():
            raise RepositoryClientError(
                "unavailable", "Repository revision could not be resolved", retryable=False
            )
        return revision

    async def create_branch(self, name: str, *, message: str) -> str:
        """base revision から branch を server 側 copy で作り、その URL を返す。"""

        url = self.branch_url(name)
        await self._run(
            [
                "copy",
                "--parents",
                f"{self._uri}@{self._base_revision}",
                url,
                "--message",
                message,
            ]
        )
        return url

    async def commit_files(
        self, url: str, *, files: Mapping[str, str | None], message: str
    ) -> str:
        """対象 URL を checkout して変更を書き、commit した revision を返す。

        checkout は base revision で行う。他者が同 path を更新していれば svn 自身が
        「out of date」で commit を拒否するため、これがそのまま CAS になる。
        """

        with tempfile.TemporaryDirectory(prefix="projectmind-svn-write-") as workspace:
            checkout = Path(workspace) / "wc"
            await self._run(["checkout", "--quiet", url, str(checkout)])
            for path, content in sorted(files.items()):
                _validate_repository_path(path)
                target = _resolve_inside(checkout, path)
                if content is None:
                    if target.is_file():
                        await self._run(["delete", "--quiet", str(target)])
                    continue
                created = not target.exists()
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                if created:
                    await self._run(["add", "--quiet", "--parents", str(target)])
            await self._run(["commit", "--quiet", str(checkout), "--message", message])
        return await self.head_revision(url)

    async def _run(
        self, arguments: Sequence[str], *, max_output_bytes: int = _MAX_COMMAND_OUTPUT_BYTES
    ) -> bytes:
        """共通 option と凭据を付けて svn を実行する (password は stdin)。"""

        command = [
            self._executable,
            *arguments,
            "--non-interactive",
            "--no-auth-cache",
            "--config-dir",
            str(self._config_dir),
        ]
        stdin_payload: str | None = None
        if self._credential is not None:
            command.extend(["--username", self._credential.username, "--password-from-stdin"])
            stdin_payload = self._credential.secret
        return await _run_command(
            command,
            environment=self._environment,
            timeout_seconds=self._timeout_seconds,
            stdin_payload=stdin_payload,
            max_output_bytes=max_output_bytes,
        )


def _resolve_inside(base: Path, path: str) -> Path:
    """作業 copy の内側だけを指す絶対 path へ解決する。"""

    root = base.resolve(strict=True)
    target = (root / path).resolve(strict=False)
    if not target.is_relative_to(root) or target.is_symlink():
        raise RepositoryClientError(
            "invalid_request", "Repository path escapes the working copy", retryable=False
        )
    return target


class SvnCommandRepositoryClient:
    """`svn` command で URL を直接読む本番 client (作業 copy を作らない)。"""

    def __init__(self, *, command_timeout_seconds: int, executable: str = "svn") -> None:
        """1 command あたりの timeout と実行 file を固定する。"""

        if command_timeout_seconds < 1:
            raise ValueError("Repository command timeout must be positive")
        self._timeout_seconds = command_timeout_seconds
        self._executable = executable

    @asynccontextmanager
    async def open(
        self,
        *,
        uri: str,
        revision: str,
        credential: RepositoryCredential | None,
    ) -> AsyncIterator[RepositorySession]:
        """Peg revision を具体 revision 番号へ解決した session を貸し出す。"""

        _validate_uri("svn", uri)
        _validate_revision(revision)
        with tempfile.TemporaryDirectory(prefix="projectmind-svn-") as workspace:
            root = Path(workspace)
            config_dir = root / "config"
            config_dir.mkdir(mode=0o700)
            environment = _svn_environment(home=root)
            session = _SvnRepositorySession(
                executable=self._executable,
                uri=uri.rstrip("/"),
                revision=revision,
                config_dir=config_dir,
                environment=environment,
                credential=credential,
                timeout_seconds=self._timeout_seconds,
            )
            await session.resolve()
            yield session


    @asynccontextmanager
    async def open_writable(
        self,
        *,
        uri: str,
        revision: str,
        credential: RepositoryCredential | None,
    ) -> AsyncIterator[SvnWriteSession]:
        """base revision を解決し、仓库 root を確定した書き込み session を貸し出す。"""

        _validate_uri("svn", uri)
        _validate_revision(revision)
        with tempfile.TemporaryDirectory(prefix="projectmind-svn-") as workspace:
            root = Path(workspace)
            config_dir = root / "config"
            config_dir.mkdir(mode=0o700)
            environment = _svn_environment(home=root)
            command = [
                self._executable,
                "info",
                "--xml",
                "--depth",
                "empty",
                f"{uri.rstrip('/')}@{revision}",
                "--non-interactive",
                "--no-auth-cache",
                "--config-dir",
                str(config_dir),
            ]
            stdin_payload: str | None = None
            if credential is not None:
                command.extend(
                    ["--username", credential.username, "--password-from-stdin"]
                )
                stdin_payload = credential.secret
            raw = await _run_command(
                command,
                environment=environment,
                timeout_seconds=self._timeout_seconds,
                stdin_payload=stdin_payload,
            )
            element = _svn_element(raw)
            entry = element.find("entry")
            resolved = entry.get("revision") if entry is not None else None
            repository_root = element.findtext("entry/repository/root")
            if not resolved or not resolved.isdigit() or not repository_root:
                raise RepositoryClientError(
                    "unavailable", "Repository revision could not be resolved", retryable=False
                )
            yield SvnWriteSession(
                executable=self._executable,
                uri=uri,
                base_revision=resolved,
                repository_root=repository_root,
                config_dir=config_dir,
                environment=environment,
                credential=credential,
                timeout_seconds=self._timeout_seconds,
            )


class _SvnRepositorySession:
    """`svn info/list/cat` を URL に対して実行する読み取り実装。"""

    def __init__(
        self,
        *,
        executable: str,
        uri: str,
        revision: str,
        config_dir: Path,
        environment: Mapping[str, str],
        credential: RepositoryCredential | None,
        timeout_seconds: int,
    ) -> None:
        """接続情報と未解決 revision 式を保持する。"""

        self._executable = executable
        self._uri = uri
        self._requested_revision = revision
        self._resolved_revision = revision
        self._config_dir = config_dir
        self._environment = environment
        self._credential = credential
        self._timeout_seconds = timeout_seconds

    @property
    def provider(self) -> str:
        """Provider 名を返す。"""

        return "svn"

    @property
    def revision(self) -> str:
        """解決済み revision 番号を返す。"""

        return self._resolved_revision

    async def resolve(self) -> None:
        """`HEAD` などの式を具体 revision 番号へ解決し、以降の読み取りを固定する。"""

        raw = await self._run(["info", "--xml", "--depth", "empty", self._target("")])
        entry = _svn_element(raw).find("entry")
        resolved = entry.get("revision") if entry is not None else None
        if not resolved or not resolved.isdigit():
            raise RepositoryClientError(
                "unavailable", "Repository revision could not be resolved", retryable=False
            )
        self._resolved_revision = resolved

    async def list_files(self, paths: Sequence[str]) -> RepositoryListing:
        """Scope path ごとに `svn list -R` を実行し、file だけを列挙する。

        列挙 cost は scope path の件数に比例する。存在しない scope path は git の `ls-tree` と
        同じく Run を失敗させず、skip として manifest に残す。
        """

        entries: list[RepositoryFileEntry] = []
        skipped: list[RepositorySkippedEntry] = []
        for path in paths:
            _validate_repository_path(path)
            try:
                kind = await self._entry_kind(path)
            except RepositoryClientError as error:
                if error.code != "not_found":
                    raise
                skipped.append(RepositorySkippedEntry(path=path, reason="not_found"))
                continue
            raw = await self._run(["list", "--xml", "-R", self._target(path)])
            for element in _svn_element(raw).iter("entry"):
                if len(entries) >= _MAX_LISTING_ENTRIES:
                    raise RepositoryClientError(
                        "too_large", "Repository listing exceeds the entry limit", retryable=False
                    )
                if element.get("kind") != "file":
                    continue
                name = element.findtext("name") or ""
                size = element.findtext("size")
                # 対象自体が file のときは `<name>` が basename のみになるため path を採用する。
                entry_path = path if kind == "file" else f"{path}/{name}"
                if not name or (kind != "file" and ".." in PurePosixPath(name).parts):
                    skipped.append(RepositorySkippedEntry(path=entry_path, reason="invalid_path"))
                    continue
                entries.append(
                    RepositoryFileEntry(
                        path=entry_path,
                        size=int(size) if size is not None and size.isdigit() else 0,
                    )
                )
        return RepositoryListing(entries=tuple(entries), skipped=tuple(skipped))

    async def read_file(self, path: str, *, max_bytes: int) -> bytes:
        """`svn cat` で 1 file を上限付きで読む。"""

        _validate_repository_path(path)
        return await self._run(["cat", self._target(path)], max_output_bytes=max_bytes)

    async def read_history(
        self, paths: Sequence[str], *, limit: int
    ) -> tuple[RepositoryCommit, ...]:
        """Scope path ごとに `svn log` を引き、revision 降順へ併合して返す。

        repository root ではなく scope path に対して引くのは、binding が許していない path の
        変更が履歴経由で見えないようにするため。`-v` は付けず、変更 path 一覧も取得しない。
        """

        merged: dict[str, RepositoryCommit] = {}
        for path in paths:
            _validate_repository_path(path)
            try:
                raw = await self._run(
                    ["log", "--xml", "--limit", str(limit), self._target(path)]
                )
            except RepositoryClientError as error:
                if error.code != "not_found":
                    raise
                continue
            for entry in _svn_element(raw).iter("logentry"):
                revision = entry.get("revision")
                if revision is None:
                    continue
                merged[revision] = RepositoryCommit(
                    revision=revision,
                    committed_at=entry.findtext("date") or "",
                    author=entry.findtext("author") or "",
                    summary=_first_line(entry.findtext("msg")),
                )
        ordered = sorted(
            merged.values(),
            key=lambda commit: int(commit.revision) if commit.revision.isdigit() else 0,
            reverse=True,
        )
        return tuple(ordered[:limit])

    async def _entry_kind(self, path: str) -> str:
        """Scope path が file か dir かを判定する (列挙 path の組み立てに要る)。"""

        raw = await self._run(["info", "--xml", "--depth", "empty", self._target(path)])
        entry = _svn_element(raw).find("entry")
        kind = entry.get("kind") if entry is not None else None
        if kind not in {"file", "dir"}:
            raise RepositoryClientError(
                "not_found", "Repository path was not found", retryable=False
            )
        return kind

    def _target(self, path: str) -> str:
        """Peg revision 付き URL を組む。末尾 peg は path 中の `@` の escape も兼ねる。"""

        suffix = f"/{quote(path, safe='/')}" if path else ""
        return f"{self._uri}{suffix}@{self._resolved_revision}"

    async def _run(
        self, arguments: Sequence[str], *, max_output_bytes: int = _MAX_COMMAND_OUTPUT_BYTES
    ) -> bytes:
        """共通 option と凭据を付けて svn を実行する。password は stdin 経由で渡す。"""

        command = [
            self._executable,
            *arguments,
            "--non-interactive",
            "--no-auth-cache",
            "--config-dir",
            str(self._config_dir),
        ]
        stdin_payload: str | None = None
        if self._credential is not None:
            command.extend(["--username", self._credential.username, "--password-from-stdin"])
            stdin_payload = self._credential.secret
        return await _run_command(
            command,
            environment=self._environment,
            timeout_seconds=self._timeout_seconds,
            stdin_payload=stdin_payload,
            max_output_bytes=max_output_bytes,
        )


def _first_line(message: str | None) -> str:
    """Commit message の 1 行目だけを要約として使う (履歴 text を 1 commit 1 行に保つ)。"""

    if not message:
        return ""
    stripped = message.strip().splitlines()
    return stripped[0] if stripped else ""


def _entry_size(fields: Sequence[bytes]) -> int:
    """`ls-tree --long` の size 列を読む。`-` (非 blob) は 0 とみなす。"""

    if not fields:
        return 0
    raw = fields[0].decode("ascii", errors="replace")
    return int(raw) if raw.isdigit() else 0


def _svn_element(raw: bytes) -> ElementTree.Element:
    """svn の XML 出力を解析する。壊れた出力は unavailable として扱う。"""

    try:
        return ElementTree.fromstring(raw.decode("utf-8", errors="replace"))
    except ElementTree.ParseError as error:
        raise RepositoryClientError(
            "unavailable", "Repository response could not be parsed", retryable=False
        ) from error


def _validate_uri(provider: str, uri: str) -> None:
    """登録時と同じ scheme 制限を実行直前にも確認し、凭据入り URI を拒否する。"""

    parsed = urlsplit(uri)
    if parsed.scheme not in REPOSITORY_URI_SCHEMES.get(provider, frozenset()):
        raise RepositoryClientError(
            "unavailable", "Repository URI scheme is not supported", retryable=False
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RepositoryClientError(
            "unavailable", "Repository URI contains forbidden components", retryable=False
        )


def _validate_revision(revision: str) -> None:
    """Revision 式を安全な文字集合へ制限し、先頭 `-` の option 誤認を防ぐ。"""

    if _REVISION_PATTERN.fullmatch(revision) is None:
        raise RepositoryClientError(
            "invalid_request", "Repository revision is invalid", retryable=False
        )


def _validate_branch(branch: str) -> None:
    """Ref として安全な branch 名だけを受理する (`..` や先頭 `-` を拒否)。"""

    if _BRANCH_PATTERN.fullmatch(branch) is None or ".." in branch or branch.endswith(".lock"):
        raise RepositoryClientError(
            "invalid_request", "Repository branch name is invalid", retryable=False
        )


def _validate_repository_path(path: str) -> None:
    """Repository 内 path を相対・非 traversal・印字可能に制限する。"""

    if not path or len(path) > _MAX_REPOSITORY_PATH_LENGTH or "\\" in path:
        raise RepositoryClientError(
            "invalid_request", "Repository path is invalid", retryable=False
        )
    parts = path.split("/")
    if any(part in {"", ".", ".."} or not part.isprintable() for part in parts):
        raise RepositoryClientError(
            "invalid_request", "Repository path is invalid", retryable=False
        )


def _git_environment(*, home: Path, credential: RepositoryCredential | None) -> dict[str, str]:
    """git 用の最小環境を組む。Worker の環境変数 (DB URL や KEK) は継承させない。"""

    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LC_ALL": "C",
        # 認証失敗時に terminal/askpass で待たせない (job が timeout まで固まるのを防ぐ)。
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    if credential is not None:
        material = f"{credential.username}:{credential.secret}".encode()
        token = base64.b64encode(material).decode("ascii")
        # 凭据は argv ではなく環境変数から config へ注入する。argv は他 process から
        # `/proc/<pid>/cmdline` で読めるが、environ は所有者だけが読める。
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}",
            }
        )
    return environment


def _svn_environment(*, home: Path) -> dict[str, str]:
    """svn 用の最小環境を組む。auth cache は `--config-dir` 側で無効化する。"""

    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LC_ALL": "C",
    }


async def _run_command(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout_seconds: int,
    stdin_payload: str | None = None,
    max_output_bytes: int = _MAX_COMMAND_OUTPUT_BYTES,
) -> bytes:
    """外部 command を timeout と出力上限付きで実行し、stdout を返す。

    shell を介さず argv を直接渡す。失敗時は stderr 本文を返さず marker で分類だけ行う
    (stderr には URI や server 応答が混ざり、Agent へ返す面を広げてしまう)。
    """

    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(environment),
        )
    except OSError as error:
        raise RepositoryClientError(
            "unavailable", "Repository command could not be started", retryable=False
        ) from error
    try:
        stdout, stderr = await asyncio.wait_for(
            _communicate(process, stdin_payload=stdin_payload, max_output_bytes=max_output_bytes),
            timeout=timeout_seconds,
        )
    except TimeoutError as error:
        # Timeout した子 process を残すと Worker slot と接続を占有し続ける。
        await _terminate(process)
        raise RepositoryClientError(
            "unavailable", "Repository command timed out", retryable=True
        ) from error
    except RepositoryClientError:
        await _terminate(process)
        raise
    if process.returncode != 0:
        raise _command_failure(stderr)
    return stdout


async def _communicate(
    process: asyncio.subprocess.Process,
    *,
    stdin_payload: str | None,
    max_output_bytes: int,
) -> tuple[bytes, bytes]:
    """stdin を渡し、stdout/stderr を上限付きで同時に読み切る。

    片側だけ読むと pipe buffer が埋まって子 process が停止するため、両方を並行に読む。
    """

    if process.stdout is None or process.stderr is None or process.stdin is None:
        raise RepositoryClientError(
            "unavailable", "Repository command pipes are unavailable", retryable=False
        )
    stdin = process.stdin
    if stdin_payload is not None:
        stdin.write(stdin_payload.encode("utf-8"))
        with suppress(BrokenPipeError, ConnectionResetError):
            await stdin.drain()
    with suppress(BrokenPipeError, ConnectionResetError):
        stdin.close()
    stdout, stderr = await asyncio.gather(
        _read_capped(process.stdout, limit=max_output_bytes),
        _read_capped(process.stderr, limit=65_536),
    )
    await process.wait()
    return stdout, stderr


async def _read_capped(stream: asyncio.StreamReader, *, limit: int) -> bytes:
    """Stream を上限まで読み、超過したら打ち切って fail closed する。"""

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise RepositoryClientError(
                "too_large", "Repository content exceeds the read limit", retryable=False
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """残った子 process を確実に落とす。"""

    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.kill()
    with suppress(Exception):
        await process.wait()


def _command_failure(stderr: bytes) -> RepositoryClientError:
    """stderr 本文を露出せずに、既知の失敗形だけを code へ分類する。"""

    lowered = stderr.lower()
    if any(marker in lowered for marker in _AUTHENTICATION_MARKERS):
        return RepositoryClientError(
            "unavailable", "Repository credential was rejected", retryable=False
        )
    if any(marker in lowered for marker in _NOT_FOUND_MARKERS):
        return RepositoryClientError(
            "not_found", "Repository path or revision was not found", retryable=False
        )
    return RepositoryClientError("unavailable", "Repository command failed", retryable=False)
