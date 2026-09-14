"""承認済み repository 変更の apply を本物の git remote に対して検証する (計画 §20 R2)。

bare repository を remote に見立てて `file://` で push する。branch 作成・内容・read-back・
冪等 replay・衝突・CAS の失敗はすべて実 git の挙動で確認し、mock で置き換えない——ここは
「本当に書き込む」層であり、置き換えると検証したい性質そのものが消える。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from skillmind.agent.repository_client import (
    GitCommandRepositoryClient,
    SvnCommandRepositoryClient,
)
from skillmind.effects.domain import ClaimedEffectExecution, EffectExecutionStatus
from skillmind.effects.redmine import (
    EffectProviderStaleError,
    EffectProviderTransportError,
)
from skillmind.effects.repository_effect import (
    GitRepositoryWriteProvider,
    SvnRepositoryWriteProvider,
)
from skillmind.effects.repository_write import REPOSITORY_WRITE_CAPABILITY

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git command is not installed"
)

_BRANCH = "skillmind/run-7a1c/fix-null-guard"


def _run(arguments: list[str], *, home: Path) -> str:
    """テスト fixture 用に git を同期実行する (HOME は tmp 配下へ隔離)。"""

    completed = subprocess.run(
        arguments,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
        },
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout


def _remote_repository(root: Path) -> tuple[str, str]:
    """push 先の bare repository を作り、(URI, base commit) を返す。"""

    home = root / "home"
    home.mkdir(exist_ok=True)
    bare = root / "remote.git"
    seed = root / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "handler.py").write_text("def handle():\n    return 0\n", encoding="utf-8")
    (seed / "src" / "legacy.py").write_text("# legacy\n", encoding="utf-8")
    _run(["git", "init", "--quiet", "--bare", "-b", "main", str(bare)], home=home)
    _run(["git", "init", "--quiet", "-b", "main", str(seed)], home=home)
    _run(["git", "-C", str(seed), "add", "-A"], home=home)
    _run(
        [
            "git",
            "-c",
            "user.email=seed@example.invalid",
            "-c",
            "user.name=Seed",
            "-C",
            str(seed),
            "commit",
            "--quiet",
            "-m",
            "seed",
        ],
        home=home,
    )
    _run(["git", "-C", str(seed), "remote", "add", "origin", f"file://{bare}"], home=home)
    _run(["git", "-C", str(seed), "push", "--quiet", "origin", "main"], home=home)
    base = _run(["git", "-C", str(seed), "rev-parse", "HEAD"], home=home).strip()
    return f"file://{bare}", base


def _execution(*, uri: str, base_revision: str, changes: tuple[dict[str, Any], ...]) -> Any:
    """承認済み snapshot に相当する ClaimedEffectExecution を組む。"""

    return ClaimedEffectExecution(
        effect_execution_id=uuid4(),
        proposal_id=uuid4(),
        proposal_ref="cp_9f2c1d4b8a6e5307",
        approval_id=uuid4(),
        run_id=uuid4(),
        run_segment_id=uuid4(),
        run_attempt_id=uuid4(),
        agent_session_id=uuid4(),
        project_id=uuid4(),
        integration_id=uuid4(),
        binding_id=uuid4(),
        capability_version=REPOSITORY_WRITE_CAPABILITY,
        operation="commit",
        target={"locator": _BRANCH, "display": "Guard against a null payload"},
        changes=changes,
        precondition={"revision": base_revision},
        verification={"method": "READ_BACK", "paths": ["/files/src/handler.py"]},
        idempotency_key="cp-7a1c9d2e-repository",
        request_fingerprint="1" * 64,
        provider="git",
        integration_revision=1,
        integration_scope={"paths": ["src"], "revisions": ["main"]},
        # 既定 mode は direct (§20 D6)。予約 branch の挙動を見る test 群は branch を明示する。
        integration_config={
            "repository_uri": uri,
            "default_revision": "main",
            "write_mode": "branch",
        },
        secret_reference_id=None,
        lease_token="lease",
        lease_expires_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        attempt_no=1,
    )


async def _authorize(execution: ClaimedEffectExecution, credential: str | None) -> None:
    """実 Git fixture は認可済み snapshot を注入し、認可失効は個別 test で再現する。"""


def _provider() -> GitRepositoryWriteProvider:
    """実 git client を使う Provider を作る。"""

    return GitRepositoryWriteProvider(
        GitCommandRepositoryClient(command_timeout_seconds=60), authorize=_authorize
    )


def _branch_content(root: Path, uri: str, path: str) -> str | None:
    """push 済み branch の file 内容を remote から直接読む。"""

    home = root / "home"
    check = root / f"check-{uuid4().hex[:8]}"
    _run(["git", "clone", "--quiet", "--branch", _BRANCH, uri, str(check)], home=home)
    target = check / path
    return target.read_text(encoding="utf-8") if target.is_file() else None


@requires_git
@pytest.mark.asyncio
async def test_approved_change_lands_as_a_new_branch_commit(tmp_path: Path) -> None:
    """承認済み変更が予約 namespace の新 branch へ commit され、remote から読み戻せる。"""

    uri, base = _remote_repository(tmp_path)
    changes = (
        {
            "path": "/files/src/handler.py",
            "action": "SET",
            "value": "def handle():\n    return 1\n",
        },
        {"path": "/files/src/legacy.py", "action": "REMOVE", "value": None},
    )

    result = await _provider().apply(
        _execution(uri=uri, base_revision=base, changes=changes), credential=None
    )

    assert result.replayed is False
    assert result.verification["base_revision"] == base
    assert result.verification["target_branch"] == _BRANCH
    assert result.verification["matched_paths"] == [
        "/files/src/handler.py",
        "/files/src/legacy.py",
    ]
    assert result.before.source_locator["revision"] == base
    assert result.after.source_locator["revision"] == result.verification["commit_revision"]
    # remote に実際の内容が在る (read-back が Provider 内で通っただけでなく外からも確認)。
    assert _branch_content(tmp_path, uri, "src/handler.py") == "def handle():\n    return 1\n"
    assert _branch_content(tmp_path, uri, "src/legacy.py") is None
    # 既定 branch は触れていない。
    home = tmp_path / "home"
    main = _run(["git", "ls-remote", uri, "refs/heads/main"], home=home).split()[0]
    assert main == base


@requires_git
@pytest.mark.asyncio
async def test_reapplying_the_same_change_is_a_replay_not_a_second_commit(
    tmp_path: Path,
) -> None:
    """同一提案の再実行は replay として閉じ、branch を書き換えない (冪等)。"""

    uri, base = _remote_repository(tmp_path)
    changes = (
        {
            "path": "/files/src/handler.py",
            "action": "SET",
            "value": "def handle():\n    return 1\n",
        },
    )
    execution = _execution(uri=uri, base_revision=base, changes=changes)

    first = await _provider().apply(execution, credential=None)
    second = await _provider().apply(execution, credential=None)

    assert first.replayed is False
    assert second.replayed is True
    assert second.verification["commit_revision"] == first.verification["commit_revision"]


@requires_git
@pytest.mark.asyncio
async def test_existing_branch_with_other_content_is_a_conflict_not_an_overwrite(
    tmp_path: Path,
) -> None:
    """同名 branch が別内容なら上書きせず衝突として失敗する。"""

    uri, base = _remote_repository(tmp_path)
    home = tmp_path / "home"
    other = tmp_path / "other"
    _run(["git", "clone", "--quiet", uri, str(other)], home=home)
    _run(["git", "-C", str(other), "checkout", "--quiet", "-b", _BRANCH], home=home)
    (other / "src" / "handler.py").write_text("def handle():\n    return 99\n", encoding="utf-8")
    _run(["git", "-C", str(other), "add", "-A"], home=home)
    _run(
        [
            "git",
            "-c",
            "user.email=other@example.invalid",
            "-c",
            "user.name=Other",
            "-C",
            str(other),
            "commit",
            "--quiet",
            "-m",
            "someone else",
        ],
        home=home,
    )
    _run(["git", "-C", str(other), "push", "--quiet", "origin", _BRANCH], home=home)

    changes = (
        {
            "path": "/files/src/handler.py",
            "action": "SET",
            "value": "def handle():\n    return 1\n",
        },
    )

    with pytest.raises(EffectProviderTransportError) as error:
        await _provider().apply(
            _execution(uri=uri, base_revision=base, changes=changes), credential=None
        )

    assert error.value.code == "target_branch_conflict"
    assert error.value.retryable is False
    # 他者の内容は保たれる。
    assert _branch_content(tmp_path, uri, "src/handler.py") == "def handle():\n    return 99\n"


@requires_git
@pytest.mark.asyncio
async def test_moved_base_revision_is_refused_as_stale(tmp_path: Path) -> None:
    """凍結した base revision が解決できなければ apply しない (base-revision CAS)。"""

    uri, _ = _remote_repository(tmp_path)
    changes = (
        {
            "path": "/files/src/handler.py",
            "action": "SET",
            "value": "def handle():\n    return 1\n",
        },
    )

    with pytest.raises((EffectProviderStaleError, EffectProviderTransportError)):
        await _provider().apply(
            _execution(uri=uri, base_revision="0" * 40, changes=changes), credential=None
        )


@requires_git
@pytest.mark.asyncio
async def test_branch_outside_the_reserved_namespace_is_refused(tmp_path: Path) -> None:
    """承認済み snapshot でも予約 namespace の外へは書かない (最終防衛線)。"""

    uri, base = _remote_repository(tmp_path)
    execution = _execution(
        uri=uri,
        base_revision=base,
        changes=(
            {
                "path": "/files/src/handler.py",
                "action": "SET",
                "value": "def handle():\n    return 1\n",
            },
        ),
    )
    tampered = ClaimedEffectExecution(
        **{
            **{field: getattr(execution, field) for field in execution.__slots__},
            "target": {"locator": "main", "display": "main"},
        }
    )

    with pytest.raises(Exception) as error:
        await _provider().apply(tampered, credential=None)

    assert "reserved namespace" in str(error.value)
    assert EffectExecutionStatus.APPLIED  # 状態列挙が読み込めることの sanity check


def _direct_config(uri: str) -> dict[str, str]:
    """承認後に本流へ直接入れる Integration config を返す。"""

    return {"repository_uri": uri, "default_revision": "main", "write_mode": "direct"}


@requires_git
@pytest.mark.asyncio
async def test_direct_mode_lands_the_change_on_the_default_branch(tmp_path: Path) -> None:
    """direct では承認済み変更が既定 branch へそのまま入る (計画 §20 R5)。"""

    uri, base = _remote_repository(tmp_path)
    execution = replace(
        _execution(
            uri=uri,
            base_revision=base,
            changes=(
                {
                    "path": "/files/src/handler.py",
                    "action": "SET",
                    "value": "def handle():\n    return 1\n",
                },
            ),
        ),
        target={"locator": "main", "display": "Guard against a null payload"},
        integration_config=_direct_config(uri),
    )

    result = await _provider().apply(execution, credential=None)

    assert result.verification["write_mode"] == "direct"
    assert result.verification["target_branch"] == "main"
    # 本流の先頭が新しい commit になっている (fast-forward、force は使わない)。
    home = tmp_path / "home"
    head = _run(["git", "ls-remote", uri, "refs/heads/main"], home=home).split()[0]
    assert head == result.verification["commit_revision"]
    assert head != base
    # PR は開かない (既に本流に入っているため)。
    assert "pull_request_url" not in result.verification
    # 予約 namespace の branch は作られない。
    assert _BRANCH not in _run(["git", "ls-remote", "--heads", uri], home=home)


@requires_git
@pytest.mark.asyncio
async def test_direct_mode_refuses_when_the_default_branch_moved(tmp_path: Path) -> None:
    """本流が提案後に動いていれば apply しない (base-revision CAS)。"""

    uri, base = _remote_repository(tmp_path)
    home = tmp_path / "home"
    other = tmp_path / "other"
    _run(["git", "clone", "--quiet", uri, str(other)], home=home)
    (other / "src" / "handler.py").write_text("def handle():\n    return 42\n", encoding="utf-8")
    _run(["git", "-C", str(other), "add", "-A"], home=home)
    _run(
        [
            "git",
            "-c",
            "user.email=other@example.invalid",
            "-c",
            "user.name=Other",
            "-C",
            str(other),
            "commit",
            "--quiet",
            "-m",
            "someone else moved main",
        ],
        home=home,
    )
    _run(["git", "-C", str(other), "push", "--quiet", "origin", "main"], home=home)
    moved = _run(["git", "ls-remote", uri, "refs/heads/main"], home=home).split()[0]

    execution = replace(
        _execution(
            uri=uri,
            base_revision=base,
            changes=(
                {"path": "/files/src/handler.py", "action": "SET", "value": "x\n"},
            ),
        ),
        target={"locator": "main", "display": "stale change"},
        integration_config=_direct_config(uri),
    )

    with pytest.raises(EffectProviderStaleError):
        await _provider().apply(execution, credential=None)

    # 他者の commit は残り、本流は書き換わっていない。
    assert _run(["git", "ls-remote", uri, "refs/heads/main"], home=home).split()[0] == moved


@requires_git
@pytest.mark.asyncio
async def test_direct_mode_cannot_target_another_branch(tmp_path: Path) -> None:
    """direct でも既定 branch 以外へは書かせない (最終防衛線)。"""

    uri, base = _remote_repository(tmp_path)
    execution = replace(
        _execution(
            uri=uri,
            base_revision=base,
            changes=({"path": "/files/src/handler.py", "action": "SET", "value": "x\n"},),
        ),
        target={"locator": "release", "display": "release"},
        integration_config=_direct_config(uri),
    )

    with pytest.raises(Exception) as error:
        await _provider().apply(execution, credential=None)

    assert "default branch" in str(error.value)


requires_svn = pytest.mark.skipif(
    shutil.which("svn") is None or shutil.which("svnadmin") is None,
    reason="svn commands are not installed",
)


def _svn_remote(root: Path) -> tuple[str, str]:
    """trunk 配下に seed を持つ svn repository を作り、(trunk URI, base revision) を返す。"""

    home = root / "home"
    home.mkdir(exist_ok=True)
    repository = root / "svnrepo"
    payload = root / "svnseed" / "trunk" / "src"
    payload.mkdir(parents=True)
    (payload / "handler.py").write_text("def handle():\n    return 0\n", encoding="utf-8")
    (payload / "legacy.py").write_text("# legacy\n", encoding="utf-8")
    _run(["svnadmin", "create", str(repository)], home=home)
    _run(
        ["svn", "import", "-q", "-m", "seed", str(root / "svnseed"), f"file://{repository}"],
        home=home,
    )
    return f"file://{repository}/trunk", "1"


def _svn_execution(*, uri: str, base_revision: str, target: str, mode: str) -> Any:
    """svn 向けの承認済み snapshot を組む。"""

    return replace(
        _execution(
            uri=uri,
            base_revision=base_revision,
            changes=(
                {
                    "path": "/files/src/handler.py",
                    "action": "SET",
                    "value": "def handle():\n    return 1\n",
                },
            ),
        ),
        provider="svn",
        target={"locator": target, "display": "Guard against a null payload"},
        integration_config={
            "repository_uri": uri,
            "default_revision": "HEAD",
            "write_mode": mode,
        },
    )


@requires_svn
@pytest.mark.asyncio
async def test_svn_branch_mode_commits_under_the_fixed_convention(tmp_path: Path) -> None:
    """svn は `branches/skillmind/` 配下へ copy して commit する (計画 §20 D5)。"""

    uri, base = _svn_remote(tmp_path)
    provider = SvnRepositoryWriteProvider(
        SvnCommandRepositoryClient(command_timeout_seconds=60)
    )

    result = await provider.apply(
        _svn_execution(uri=uri, base_revision=base, target=_BRANCH, mode="branch"),
        credential=None,
    )

    assert result.verification["write_mode"] == "branch"
    home = tmp_path / "home"
    branch_url = f"file://{tmp_path / 'svnrepo'}/branches/skillmind/run-7a1c/fix-null-guard"
    body = _run(["svn", "cat", f"{branch_url}/src/handler.py"], home=home)
    assert body == "def handle():\n    return 1\n"
    # trunk は触れていない。
    trunk = _run(["svn", "cat", f"{uri}/src/handler.py"], home=home)
    assert trunk == "def handle():\n    return 0\n"


@requires_svn
@pytest.mark.asyncio
async def test_svn_direct_mode_commits_to_the_bound_path(tmp_path: Path) -> None:
    """direct では承認済み変更が束縛 path (trunk) へそのまま入る。"""

    uri, base = _svn_remote(tmp_path)
    provider = SvnRepositoryWriteProvider(
        SvnCommandRepositoryClient(command_timeout_seconds=60)
    )

    result = await provider.apply(
        _svn_execution(uri=uri, base_revision=base, target="HEAD", mode="direct"),
        credential=None,
    )

    assert result.verification["write_mode"] == "direct"
    home = tmp_path / "home"
    assert _run(["svn", "cat", f"{uri}/src/handler.py"], home=home) == (
        "def handle():\n    return 1\n"
    )


@requires_svn
@pytest.mark.asyncio
async def test_svn_existing_branch_with_other_content_is_a_conflict(tmp_path: Path) -> None:
    """同名 branch が別内容なら上書きせず衝突として失敗する。"""

    uri, base = _svn_remote(tmp_path)
    home = tmp_path / "home"
    branch_url = f"file://{tmp_path / 'svnrepo'}/branches/skillmind/run-7a1c/fix-null-guard"
    _run(
        ["svn", "copy", "--parents", "-m", "someone else", f"{uri}@1", branch_url],
        home=home,
    )
    provider = SvnRepositoryWriteProvider(
        SvnCommandRepositoryClient(command_timeout_seconds=60)
    )

    with pytest.raises(EffectProviderTransportError) as error:
        await provider.apply(
            _svn_execution(uri=uri, base_revision=base, target=_BRANCH, mode="branch"),
            credential=None,
        )

    assert error.value.code == "target_branch_conflict"


@requires_git
async def test_other_effect_with_identical_content_is_not_replayed(tmp_path: Path) -> None:
    """同じ byte でも別 Effect の commit を原成功に転用しない。"""
    uri, base = _remote_repository(tmp_path)
    execution = _execution(uri=uri, base_revision=base, changes=(
        {"path": "/files/src/handler.py", "action": "SET", "value": "same\n"},
    ))
    await _provider().apply(execution, credential=None)
    with pytest.raises(EffectProviderTransportError, match="target_branch_conflict"):
        await _provider().apply(replace(execution, effect_execution_id=uuid4()), credential=None)


@requires_git
@pytest.mark.parametrize("mode", ["branch", "direct"])
async def test_push_rejects_ref_created_or_rewound_after_observation(tmp_path: Path, mode) -> None:
    """GET 後の branch 作成/本流巻戻しを、push 自身の advertisement で拒否する。"""
    uri, base = _remote_repository(tmp_path)
    home, seed, bare = tmp_path / "home", tmp_path / "seed", tmp_path / "remote.git"
    initial = base
    if mode == "direct":
        _run(["git", "-C", str(seed), "-c", "user.name=Seed", "-c",
              "user.email=seed@example.invalid", "commit", "--allow-empty", "-m", "second"],
             home=home)
        _run(["git", "-C", str(seed), "push", "origin", "main"], home=home)
        base = _run(["git", "-C", str(seed), "rev-parse", "HEAD"], home=home).strip()
    branch = "main" if mode == "direct" else _BRANCH
    execution = replace(_execution(uri=uri, base_revision=base, changes=(
        {"path": "/files/src/handler.py", "action": "SET", "value": "new\n"},
    )), target={"locator": branch}, integration_config={
        "repository_uri": uri, "default_revision": "main", "write_mode": mode,
    })
    checks = 0

    async def race(claimed, credential):
        """Push 前の再認可時点で別 actor の ref 更新を再現する。"""
        nonlocal checks
        checks += 1
        if checks == 2:
            _run(["git", "--git-dir", str(bare), "update-ref", f"refs/heads/{branch}", initial],
                 home=home)

    provider = GitRepositoryWriteProvider(
        GitCommandRepositoryClient(command_timeout_seconds=30), authorize=race
    )
    with pytest.raises(EffectProviderTransportError):
        await provider.apply(execution, credential=None)
    assert _run(["git", "ls-remote", uri, f"refs/heads/{branch}"], home=home).split()[0] == initial


@requires_git
async def test_revoked_approval_before_push_leaves_remote_unchanged(tmp_path: Path) -> None:
    """長い clone 中に失効した批准で push しない。"""
    uri, base = _remote_repository(tmp_path)
    checks = 0

    async def revoke(claimed, credential):
        """最初は有効、push 直前に失効する認可 port。"""
        nonlocal checks
        checks += 1
        if checks == 2:
            raise PermissionError("revoked")

    execution = _execution(uri=uri, base_revision=base, changes=(
        {"path": "/files/src/handler.py", "action": "SET", "value": "new\n"},
    ))
    provider = GitRepositoryWriteProvider(
        GitCommandRepositoryClient(command_timeout_seconds=30), authorize=revoke
    )
    with pytest.raises(EffectProviderTransportError, match="effect_authority_revoked"):
        await provider.apply(execution, credential=None)
    assert not _run(["git", "ls-remote", uri, f"refs/heads/{_BRANCH}"], home=tmp_path / "home")


@requires_git
async def test_lost_response_retry_reads_original_commit_and_never_resends(tmp_path, monkeypatch):
    """送信応答を失っても原 Effect を照合し、不在なら二度目の push を実行しない。"""
    from skillmind.agent.repository_client import GitWriteSession

    uri, base = _remote_repository(tmp_path)
    execution = _execution(uri=uri, base_revision=base, changes=(
        {"path": "/files/src/テスト計画.json", "action": "SET", "value": '{"計画":1}\n'},
    ))
    first = await _provider().apply(execution, credential=None)

    async def forbidden(*args, **kwargs):
        """再試行が push 経路へ進むと test を失敗させる。"""
        raise AssertionError("Unexpected resend")

    monkeypatch.setattr(GitWriteSession, "commit_and_push", forbidden)
    replay = await _provider().apply(replace(execution, attempt_no=2), credential=None)
    assert replay.replayed
    assert replay.verification == {**first.verification, "replayed": True}
    assert _branch_content(tmp_path, uri, "src/テスト計画.json") == '{"計画":1}\n'
    _run(["git", "--git-dir", str(tmp_path / "remote.git"), "update-ref", "-d",
          f"refs/heads/{_BRANCH}"], home=tmp_path / "home")
    with pytest.raises(EffectProviderTransportError, match="repository_effect_unconfirmed"):
        await _provider().apply(replace(execution, attempt_no=2), credential=None)


@requires_git
async def test_repository_symlink_alias_cannot_write_another_path(tmp_path):
    """同じ repository 内を指す symlink でも批准外 path への別名 write を拒否する。"""
    uri, _ = _remote_repository(tmp_path)
    home, seed = tmp_path / "home", tmp_path / "seed"
    (seed / "src/alias.py").symlink_to("handler.py")
    _run(["git", "-C", str(seed), "add", "src/alias.py"], home=home)
    _run(["git", "-C", str(seed), "-c", "user.name=Seed", "-c",
          "user.email=seed@example.invalid", "commit", "-m", "alias"], home=home)
    _run(["git", "-C", str(seed), "push", "origin", "main"], home=home)
    base = _run(["git", "-C", str(seed), "rev-parse", "HEAD"], home=home).strip()
    with pytest.raises(EffectProviderTransportError, match="invalid_request"):
        await _provider().apply(_execution(uri=uri, base_revision=base, changes=(
            {"path": "/files/src/alias.py", "action": "SET", "value": "changed\n"},
        )), credential=None)
    assert not _run(["git", "ls-remote", uri, f"refs/heads/{_BRANCH}"], home=home)
