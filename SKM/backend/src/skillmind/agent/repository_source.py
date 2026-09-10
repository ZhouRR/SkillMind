"""Run 凍結 binding から認証済み repository session を開く境界 (計画 §19 W4)。

物化器 (`workspace_materializer`) と `repository.read/v1` Provider は、この単一実装を通じてのみ
外部 repository へ触れる。ここで凭据を解決し、binding の path scope と revision 許可を強制する
ため、上位層は「どの path/revision まで読めるか」を再実装しなくてよい。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.repository_client import (
    RepositoryClient,
    RepositoryClientError,
    RepositoryCommit,
    RepositoryCredential,
    RepositoryListing,
    RepositorySession,
)
from skillmind.agent.run_binding import (
    BoundRunResource,
    RunBindingError,
    load_bound_run_resource,
    resolve_binding_secret,
)
from skillmind.integrations.domain import (
    PROVIDER_DEFINITIONS,
    path_within_scope,
    scope_values_allow,
)
from skillmind.integrations.secrets import DeploymentSecretResolver

REPOSITORY_READ_CAPABILITY = "repository.read/v1"


@dataclass(frozen=True, slots=True)
class RepositoryBindingRef:
    """Run に凍結された repository binding の識別子。

    scope と接続情報は DB の binding/Integration 行が正本であり、ここには持たせない
    (Run snapshot 側の値を信じると、行と snapshot がずれたときに緩い方が勝ってしまう)。
    """

    provider: str
    integration_id: UUID
    binding_id: UUID


@dataclass(frozen=True, slots=True)
class RepositorySnapshotBinding:
    """cache 再利用時にも Secret/remote なしで照合する binding の公開されない摘要。"""

    checksum: str
    scope_paths: tuple[str, ...]


class ScopedRepositorySession:
    """Binding の path scope 外を物理的に読めなくする session wrapper。

    scope 裁剪は「物化した後で捨てる」のではなく「取得前に拒否する」。上位層が path を組み立て
    間違えても、binding が許した部分木の外へは出られない。
    """

    def __init__(
        self,
        *,
        session: RepositorySession,
        scope_paths: tuple[str, ...],
        binding_checksum: str = "",
    ) -> None:
        """委譲先 session と、Run に凍結された許可 path・binding checksum を保持する。"""

        self._session = session
        self._scope_paths = scope_paths
        self._binding_checksum = binding_checksum

    @property
    def provider(self) -> str:
        """Provider 名 (`git` / `svn`) を返す。"""

        return self._session.provider

    @property
    def revision(self) -> str:
        """解決済みの具体 revision を返す。"""

        return self._session.revision

    @property
    def scope_paths(self) -> tuple[str, ...]:
        """Run に凍結された許可 path の一覧を返す。"""

        return self._scope_paths

    @property
    def binding_checksum(self) -> str:
        """物化 manifest へ残す binding checksum を返す (docs/06 §6.4)。"""

        return self._binding_checksum

    async def list_files(self, paths: Sequence[str] | None = None) -> RepositoryListing:
        """許可 path (既定は scope 全体) 配下の file を列挙する。"""

        targets = tuple(paths) if paths is not None else self._scope_paths
        for target in targets:
            self._ensure_in_scope(target)
        return await self._session.list_files(targets)

    async def read_file(self, path: str, *, max_bytes: int) -> bytes:
        """許可 path 配下の 1 file を上限付きで読む。"""

        self._ensure_in_scope(path)
        return await self._session.read_file(path, max_bytes=max_bytes)

    async def read_history(self, *, limit: int) -> tuple[RepositoryCommit, ...]:
        """凍結 scope に触れた commit 履歴を新しい順に返す (計画 §19 W5)。

        対象は常に scope 全体とする。呼び出し側が path を選べると、binding が許していない
        path の履歴を引く経路をもう一つ作ることになる。
        """

        return await self._session.read_history(self._scope_paths, limit=limit)

    def _ensure_in_scope(self, path: str) -> None:
        """Path が凍結 scope のいずれかの配下かを判定する。"""

        if not path_within_scope(path, self._scope_paths):
            raise RepositoryClientError(
                "invalid_request",
                "Repository path is outside the frozen binding scope",
                retryable=False,
            )


class RepositorySnapshotSource(Protocol):
    """Run binding から認証済み repository session を開く port。"""

    async def inspect(
        self, *, project_id: UUID, run_id: UUID, binding: RepositoryBindingRef
    ) -> RepositorySnapshotBinding:
        """Run binding と現在の Integration を再検証し、remote は読み直さない。"""

        ...

    def open(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        binding: RepositoryBindingRef,
        requested_revision: str | None = None,
    ) -> AbstractAsyncContextManager[ScopedRepositorySession]:
        """凍結 scope に閉じた session を開く。"""

        ...


class IntegrationRepositorySnapshotSource:
    """Integration/SecretReference を解決して repository client を開く本番実装。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clients: Mapping[str, RepositoryClient],
        secret_resolver: DeploymentSecretResolver,
    ) -> None:
        """Provider 名ごとの client と Secret resolver を保持する。"""

        self._session_factory = session_factory
        self._clients = dict(clients)
        self._secret_resolver = secret_resolver

    async def inspect(
        self, *, project_id: UUID, run_id: UUID, binding: RepositoryBindingRef
    ) -> RepositorySnapshotBinding:
        """cache があっても binding の失効や scope 漂移を見逃さない。"""

        async with self._session_factory() as session:
            bound = await self._load_binding(
                session, project_id=project_id, run_id=run_id, binding=binding
            )
        return RepositorySnapshotBinding(
            checksum=bound.checksum, scope_paths=_scope_strings(bound.scope, key="paths")
        )

    async def _load_binding(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        run_id: UUID,
        binding: RepositoryBindingRef,
    ) -> BoundRunResource:
        """取得と cache 再利用の双方を既存の単一 binding gate へ通す。"""

        definition = PROVIDER_DEFINITIONS.get(binding.provider)
        if (
            definition is None
            or definition.kind != "repository"
            or binding.provider not in self._clients
        ):
            raise RepositoryClientError(
                "unavailable", "Repository Provider is not installed", retryable=False
            )
        try:
            return await load_bound_run_resource(
                session,
                project_id=project_id,
                run_id=run_id,
                binding_id=binding.binding_id,
                integration_id=binding.integration_id,
                provider=binding.provider,
                capability=REPOSITORY_READ_CAPABILITY,
            )
        except RunBindingError as error:
            raise RepositoryClientError(
                error.code, error.message, retryable=error.retryable
            ) from error

    @asynccontextmanager
    async def open(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        binding: RepositoryBindingRef,
        requested_revision: str | None = None,
    ) -> AsyncIterator[ScopedRepositorySession]:
        """Binding を再検証し、凭据を解決した上で scope 付き session を貸し出す。"""

        client = self._clients.get(binding.provider)
        if client is None:
            raise RepositoryClientError(
                "unavailable",
                "Repository Provider is not installed",
                retryable=False,
            )
        access = await self._resolve_access(project_id=project_id, run_id=run_id, binding=binding)
        expression = _open_revision(access, requested_revision=requested_revision)
        async with client.open(
            uri=access.uri, revision=expression, credential=access.credential
        ) as session:
            # 具体 revision (物化 manifest が示す SHA/番号) を Agent が指定した場合の許可判定は、
            # 解決後にしか行えない。凍結式で開いた session の解決値と一致すれば同一内容である。
            if (
                requested_revision is not None
                and not _revision_allowed(requested_revision, access.allowed_revisions)
                and session.revision != requested_revision
            ):
                raise RepositoryClientError(
                    "invalid_request",
                    "Repository revision is outside the frozen binding scope",
                    retryable=False,
                )
            yield ScopedRepositorySession(
                session=session,
                scope_paths=access.paths,
                binding_checksum=access.binding_checksum,
            )

    async def _resolve_access(
        self, *, project_id: UUID, run_id: UUID, binding: RepositoryBindingRef
    ) -> _RepositoryAccess:
        """Binding 行と Integration config/Secret から接続に必要な値だけを取り出す。"""

        definition = PROVIDER_DEFINITIONS.get(binding.provider)
        if definition is None or definition.kind != "repository":
            raise RepositoryClientError(
                "unavailable", "Repository Provider is not registered", retryable=False
            )
        try:
            async with self._session_factory() as session:
                bound = await self._load_binding(
                    session,
                    project_id=project_id,
                    run_id=run_id,
                    binding=binding,
                )
                material = await resolve_binding_secret(
                    session,
                    resolver=self._secret_resolver,
                    integration=bound.integration,
                    required=definition.requires_secret,
                )
        except RunBindingError as error:
            raise RepositoryClientError(
                error.code, error.message, retryable=error.retryable
            ) from error
        uri = bound.integration.config.get("repository_uri")
        default_revision = bound.integration.config.get("default_revision", "HEAD")
        if not isinstance(uri, str) or not isinstance(default_revision, str):
            raise RepositoryClientError(
                "unavailable", "Repository Integration config is invalid", retryable=False
            )
        return _RepositoryAccess(
            uri=uri,
            default_revision=default_revision,
            binding_checksum=bound.checksum,
            paths=_scope_strings(bound.scope, key="paths"),
            allowed_revisions=_scope_strings(bound.scope, key="revisions"),
            credential=(
                RepositoryCredential.from_material(material) if material is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class _RepositoryAccess:
    """Binding から導いた接続先、許可 scope、凭据。"""

    uri: str
    default_revision: str
    binding_checksum: str
    paths: tuple[str, ...]
    allowed_revisions: tuple[str, ...]
    credential: RepositoryCredential | None


def select_frozen_revision(*, allowed_revisions: Sequence[str], default_revision: str) -> str:
    """物化と live 読取が同じ内容を指すための revision 式を一意に決める。

    binding の `revisions` は「読んでよい revision の allowlist」であり、UI 上は任意項目
    (既定 `HEAD`)。したがって空 list は revision を絞らない指定とみなし、Integration の
    `default_revision` を採る。1 件ならそれ、複数件なら既定が含まれるときだけ既定を採り、
    含まれないときは指すべき内容が定まらないため fail closed する (勝手に 1 件目を選ぶと、
    利用者が意図していない revision の内容が Run に凍結されてしまう)。
    """

    if len(allowed_revisions) == 1:
        return allowed_revisions[0]
    if not allowed_revisions:
        return default_revision
    if default_revision in allowed_revisions:
        return default_revision
    raise RepositoryClientError(
        "invalid_request",
        "Repository binding does not identify a single revision",
        retryable=False,
    )


def _open_revision(access: _RepositoryAccess, *, requested_revision: str | None) -> str:
    """Session を開く revision 式を決める。許可外の要求は凍結式へ落として後段で照合する。"""

    if requested_revision is not None and _revision_allowed(
        requested_revision, access.allowed_revisions
    ):
        return requested_revision
    return select_frozen_revision(
        allowed_revisions=access.allowed_revisions,
        default_revision=access.default_revision,
    )


def _revision_allowed(revision: str, allowed_revisions: Sequence[str]) -> bool:
    """空 allowlist は revision を絞らない指定として扱う (path scope が硬境界)。"""

    if not allowed_revisions:
        return True
    return scope_values_allow(list(allowed_revisions), revision)


def _scope_strings(scope: Mapping[str, Any], *, key: str) -> tuple[str, ...]:
    """Binding scope の string list を防御的に取り出す。"""

    values = scope.get(key)
    if not isinstance(values, list):
        return ()
    return tuple(item for item in values if isinstance(item, str) and item)
