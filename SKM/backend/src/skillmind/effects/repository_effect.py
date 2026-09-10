"""承認済み repository 変更を git branch + commit として落とす Provider (計画 §20 R2)。

`observe → propose → apply` の apply 側。Agent はここへ到達せず、凭据は Provider 境界内でだけ
解決される。落とすのは **base revision から作った platform 予約 namespace の新規 branch** に
限り、既存 branch の上書き・既定 branch への push は行わない。

三つの不変式を守る:

- base-revision CAS: 提案が凍結した base commit が現在の解決値と一致しなければ apply しない
  (§20.2)。分岐が動いていれば、提案は当時の内容を前提に書かれているため作り直させる。
- read-back 検証: push 後に remote から取り直した commit の内容を hash で照合する。push が
  通ったことと、意図した内容が remote にあることは別。
- 冪等: 同じ branch が既に提案どおりの内容なら再 push せず replay として閉じる。branch が
  在って内容が違えば、上書きせず conflict として失敗させる。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from skillmind.agent.repository_client import (
    GitWritableRepositoryClient,
    GitWriteSession,
    RepositoryClientError,
    RepositoryCredential,
    SvnWritableRepositoryClient,
    SvnWriteSession,
)
from skillmind.core.hashing import sha256_hex
from skillmind.effects.domain import (
    ChangeProposalValidationError,
    ClaimedEffectExecution,
    EffectEvidenceDraft,
    EffectProviderResult,
)
from skillmind.effects.forge import ForgeTransport, PullRequestRef
from skillmind.effects.redmine import (
    EffectProviderStaleError,
    EffectProviderTransportError,
    EffectProviderVerificationError,
)
from skillmind.effects.repository_write import REPOSITORY_WRITE_CAPABILITY
from skillmind.integrations.domain import (
    REPOSITORY_WRITE_BRANCH_PREFIX,
    REPOSITORY_WRITE_MODE_DEFAULT,
    REPOSITORY_WRITE_MODE_DIRECT,
)

# read-back で 1 file から読む上限。提案側の per-file 上限と揃える。
_MAX_FILE_BYTES = 1_048_576


class GitRepositoryWriteProvider:
    """承認済み変更を branch commit として落とし、remote から読み戻して検証する。"""

    def __init__(
        self,
        client: GitWritableRepositoryClient,
        *,
        forge_transport: ForgeTransport | None = None,
    ) -> None:
        """書き込み可能な git client と、任意の forge transport を注入する。

        ``forge_transport`` 未注入、または Integration が forge を設定していない場合は PR を
        開かず branch と commit だけを残す。PR は承認の代替ではないため、無くても apply の
        安全性は変わらない (承認は既に済んでいる)。
        """

        self._client = client
        self._forge_transport = forge_transport

    async def apply(
        self, execution: ClaimedEffectExecution, *, credential: str | None
    ) -> EffectProviderResult:
        """CAS → commit/push → read-back を実行し、replay と衝突を区別して返す。"""

        if (
            execution.capability_version != REPOSITORY_WRITE_CAPABILITY
            or execution.provider != "git"
        ):
            raise ValueError("EffectExecution does not target the git repository Provider")
        branch = execution.target.get("locator")
        base_revision = execution.precondition.get("revision")
        uri = execution.integration_config.get("repository_uri")
        if not isinstance(uri, str) or not isinstance(branch, str):
            raise ValueError("EffectExecution Integration/target snapshot is invalid")
        if not isinstance(base_revision, str) or not base_revision:
            raise ValueError("EffectExecution precondition snapshot is invalid")
        mode = execution.integration_config.get("write_mode", REPOSITORY_WRITE_MODE_DEFAULT)
        if mode == REPOSITORY_WRITE_MODE_DIRECT:
            # direct は「承認したものを本流へ入れる」運用。書ける先は設定済み既定 branch のみで、
            # 任意 branch を直接書く余地は残さない (最終防衛線)。
            if branch != execution.integration_config.get("default_revision"):
                raise ChangeProposalValidationError(
                    "Direct write target must be the Integration default branch"
                )
        else:
            configured_prefix = execution.integration_config.get("write_branch_prefix")
            required_prefix = (
                configured_prefix
                if isinstance(configured_prefix, str)
                else REPOSITORY_WRITE_BRANCH_PREFIX
            )
            if not branch.startswith(REPOSITORY_WRITE_BRANCH_PREFIX) or not branch.startswith(
                required_prefix
            ):
                # 承認済み snapshot でも、予約 namespace と Integration の収窄 prefix の外へは
                # 絶対に書かない (最終防衛線)。
                raise ChangeProposalValidationError(
                    "Repository target branch is outside the reserved namespace"
                )
        files = _desired_files(execution.changes)
        parsed = RepositoryCredential.from_material(credential) if credential else None

        try:
            async with self._client.open_writable(
                uri=uri, revision=base_revision, credential=parsed
            ) as session:
                if session.base_revision != base_revision:
                    # 提案は当時の base 内容を前提にしている。動いていれば作り直させる。
                    raise EffectProviderStaleError("Repository base revision has moved")
                existing = await session.remote_branch_head(branch)
                if mode == REPOSITORY_WRITE_MODE_DIRECT:
                    # 本流へ入れる場合の CAS は「提案が凍結した base が今も本流の先頭か」。
                    # 動いていれば、提案は既に古い内容を前提にしている。
                    if existing is None:
                        raise EffectProviderTransportError(
                            "target_branch_conflict", retryable=False
                        )
                    if existing != base_revision:
                        if await _matches(session, existing, files):
                            return _result(
                                execution,
                                base_revision=base_revision,
                                branch=branch,
                                commit=existing,
                                files=files,
                                replayed=True,
                                pull_request=None,
                                mode=mode,
                            )
                        raise EffectProviderStaleError("Repository branch has moved")
                elif existing is not None:
                    matched = await _matches(session, existing, files)
                    if matched:
                        replay_pull_request = await self._open_pull_request(
                            execution, branch=branch, credential=parsed
                        )
                        return _result(
                            execution,
                            base_revision=base_revision,
                            branch=branch,
                            commit=existing,
                            files=files,
                            replayed=True,
                            pull_request=replay_pull_request,
                            mode=mode,
                        )
                    raise EffectProviderTransportError("target_branch_conflict", retryable=False)
                commit = await session.commit_and_push(
                    branch=branch, files=files, message=_commit_message(execution)
                )
                pushed = await session.fetch_branch(branch)
                if pushed != commit or not await _matches(session, pushed, files):
                    raise EffectProviderVerificationError(
                        "Repository read-back did not match the approved change"
                    )
        except RepositoryClientError as error:
            # Client の安定 code をそのまま effect の失敗分類へ渡す (本文は反射しない)。
            raise EffectProviderTransportError(error.code, retryable=error.retryable) from error
        pull_request = (
            None
            if mode == REPOSITORY_WRITE_MODE_DIRECT
            else await self._open_pull_request(execution, branch=branch, credential=parsed)
        )
        return _result(
            execution,
            base_revision=base_revision,
            branch=branch,
            commit=commit,
            files=files,
            replayed=False,
            pull_request=pull_request,
            mode=mode,
        )

    async def _open_pull_request(
        self,
        execution: ClaimedEffectExecution,
        *,
        branch: str,
        credential: RepositoryCredential | None,
    ) -> PullRequestRef | None:
        """Forge 設定が在れば PR を開く (無ければ何もしない)。

        再実行時に重複 PR を作らないよう、transport 側が同一 source branch の open PR を
        先に探して再利用する。PR の失敗は commit を巻き戻さない——commit は既に read-back で
        検証済みであり、PR は人へ渡す入口に過ぎない。
        """

        config = execution.integration_config
        kind = config.get("forge_kind")
        api_base_url = config.get("forge_api_base_url")
        project = config.get("forge_project")
        if self._forge_transport is None or not isinstance(kind, str):
            return None
        if not isinstance(api_base_url, str) or not isinstance(project, str):
            raise ValueError("EffectExecution forge configuration is invalid")
        if credential is None:
            raise EffectProviderTransportError("credential_unavailable", retryable=False)
        target = config.get("default_revision")
        if not isinstance(target, str) or not target:
            raise ValueError("EffectExecution forge target branch is invalid")
        display = execution.target.get("display")
        title = display if isinstance(display, str) and display else "Skillmind change"
        return await self._forge_transport.ensure_pull_request(
            kind=kind,
            api_base_url=api_base_url,
            project=project,
            token=credential.secret,
            source_branch=branch,
            target_branch=target,
            title=title,
            body=(
                f"Applied by Skillmind after approval.\n\n"
                f"Skillmind-Proposal: {execution.proposal_ref}\n"
            ),
        )


def _desired_files(changes: tuple[dict[str, Any], ...]) -> dict[str, str | None]:
    """凍結 changes を「repository path → 全文 (削除は None)」へ落とす。"""

    files: dict[str, str | None] = {}
    for change in changes:
        path = change.get("path")
        action = change.get("action")
        value = change.get("value")
        if not isinstance(path, str) or not path.startswith("/files/"):
            raise ValueError("EffectExecution change path is invalid")
        repository_path = path.removeprefix("/files/")
        if action == "REMOVE":
            files[repository_path] = None
            continue
        if action != "SET" or not isinstance(value, str):
            raise ValueError("EffectExecution change is not an idempotent text SET")
        files[repository_path] = value
    if not files:
        raise ValueError("EffectExecution change set is empty")
    return files


async def _matches(
    session: GitWriteSession, revision: str, files: Mapping[str, str | None]
) -> bool:
    """指定 commit の内容が提案どおりかを file 単位の hash で判定する。"""

    for path, content in files.items():
        actual = await session.read_file_at(revision, path, max_bytes=_MAX_FILE_BYTES)
        if content is None:
            if actual is not None:
                return False
            continue
        if actual is None or sha256_hex(actual) != sha256_hex(content.encode("utf-8")):
            return False
    return True


def _commit_message(execution: ClaimedEffectExecution) -> str:
    """Commit message に提案 ref を残し、履歴から承認記録へ辿れるようにする。"""

    summary = execution.target.get("display")
    headline = summary if isinstance(summary, str) and summary else "Apply approved change"
    return f"{headline}\n\nSkillmind-Proposal: {execution.proposal_ref}\n"


def _result(
    execution: ClaimedEffectExecution,
    *,
    base_revision: str,
    branch: str,
    commit: str,
    files: Mapping[str, str | None],
    replayed: bool,
    pull_request: PullRequestRef | None = None,
    mode: str = REPOSITORY_WRITE_MODE_DEFAULT,
) -> EffectProviderResult:
    """before/after Evidence と read-back 結果を組み立てる。

    before は「どの base から作ったか」、after は「どの commit として残ったか」。両者があって
    初めて、履歴上の変更を提案・承認まで遡って説明できる (§20.2「産出即 Evidence」)。
    """

    paths = [f"/files/{path}" for path in sorted(files)]
    verification: dict[str, Any] = {
        "method": "READ_BACK",
        "matched_paths": paths,
        "base_revision": base_revision,
        "commit_revision": commit,
        "target_branch": branch,
        "write_mode": mode,
        "replayed": replayed,
    }
    if pull_request is not None:
        # PR の URL は「人が評審に入る入口」。Evidence と同じく監査対象に残す。
        verification["pull_request_url"] = pull_request.url
        verification["pull_request_id"] = pull_request.identifier
    return EffectProviderResult(
        before=_evidence(execution, revision=base_revision, branch=branch, phase="before"),
        after=_evidence(execution, revision=commit, branch=branch, phase="after"),
        verification=verification,
        replayed=replayed,
    )


def _evidence(
    execution: ClaimedEffectExecution, *, revision: str, branch: str, phase: str
) -> EffectEvidenceDraft:
    """接続 URI と凭据を含まない論理 Evidence を作る。"""

    content = {
        "integration_id": str(execution.integration_id),
        "target_branch": branch,
        "revision": revision,
        "phase": phase,
    }
    return EffectEvidenceDraft(
        evidence_type="source_code",
        source_uri=f"git://integration/{execution.integration_id}/{revision}",
        source_locator={"branch": branch, "revision": revision},
        content=content,
        excerpt=f"{phase} {branch}@{revision}",
        metadata={"provider": "git", "capability": REPOSITORY_WRITE_CAPABILITY},
    )


class SvnRepositoryWriteProvider:
    """承認済み変更を svn の予約 branch (または既定 path) へ commit する (計画 §20 R4b)。

    git と同じ闸門・同じ CAS・同じ read-back を使う。branch の置き場は平台固定の約定
    (`<repository root>/branches/skillmind/<名前>`) であり、repository root は `svn info` の
    応答から取るため仓库布局を推測しない。direct mode では copy を作らず束縛 path へ直接
    commit し、他者が先に更新していれば svn 自身が out-of-date で拒否する。
    """

    def __init__(self, client: SvnWritableRepositoryClient) -> None:
        """書き込み可能な svn client を注入する。"""

        self._client = client

    async def apply(
        self, execution: ClaimedEffectExecution, *, credential: str | None
    ) -> EffectProviderResult:
        """CAS → commit → read-back を実行し、replay と衝突を区別して返す。"""

        if (
            execution.capability_version != REPOSITORY_WRITE_CAPABILITY
            or execution.provider != "svn"
        ):
            raise ValueError("EffectExecution does not target the svn repository Provider")
        target = execution.target.get("locator")
        base_revision = execution.precondition.get("revision")
        uri = execution.integration_config.get("repository_uri")
        mode = execution.integration_config.get("write_mode", REPOSITORY_WRITE_MODE_DEFAULT)
        if not isinstance(uri, str) or not isinstance(target, str):
            raise ValueError("EffectExecution Integration/target snapshot is invalid")
        if not isinstance(base_revision, str) or not base_revision:
            raise ValueError("EffectExecution precondition snapshot is invalid")
        if mode == REPOSITORY_WRITE_MODE_DIRECT:
            if target != execution.integration_config.get("default_revision"):
                raise ChangeProposalValidationError(
                    "Direct write target must be the Integration default branch"
                )
        elif not target.startswith(REPOSITORY_WRITE_BRANCH_PREFIX):
            raise ChangeProposalValidationError(
                "Repository target branch is outside the reserved namespace"
            )
        files = _desired_files(execution.changes)
        parsed = RepositoryCredential.from_material(credential) if credential else None

        try:
            async with self._client.open_writable(
                uri=uri, revision=base_revision, credential=parsed
            ) as session:
                if session.base_revision != base_revision:
                    raise EffectProviderStaleError("Repository base revision has moved")
                if mode == REPOSITORY_WRITE_MODE_DIRECT:
                    url = uri.rstrip("/")
                else:
                    name = target.removeprefix(REPOSITORY_WRITE_BRANCH_PREFIX)
                    url = session.branch_url(name)
                    if await session.path_exists(url):
                        if await _svn_matches(session, url, files):
                            return _result(
                                execution,
                                base_revision=base_revision,
                                branch=target,
                                commit=await session.head_revision(url),
                                files=files,
                                replayed=True,
                                mode=mode,
                            )
                        raise EffectProviderTransportError(
                            "target_branch_conflict", retryable=False
                        )
                    await session.create_branch(name, message=_commit_message(execution))
                committed = await session.commit_files(
                    url, files=files, message=_commit_message(execution)
                )
                if not await _svn_matches(session, url, files):
                    raise EffectProviderVerificationError(
                        "Repository read-back did not match the approved change"
                    )
        except RepositoryClientError as error:
            raise EffectProviderTransportError(error.code, retryable=error.retryable) from error
        return _result(
            execution,
            base_revision=base_revision,
            branch=target,
            commit=committed,
            files=files,
            replayed=False,
            mode=mode,
        )


async def _svn_matches(
    session: SvnWriteSession, url: str, files: Mapping[str, str | None]
) -> bool:
    """指定 URL の内容が提案どおりかを file 単位の hash で判定する。"""

    for path, content in files.items():
        actual = await session.read_file_at(url, path, max_bytes=_MAX_FILE_BYTES)
        if content is None:
            if actual is not None:
                return False
            continue
        if actual is None or sha256_hex(actual) != sha256_hex(content.encode("utf-8")):
            return False
    return True
