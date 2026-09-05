"""凭据が argv へ漏れないことを、実際の子 process 起動で検証する (計画 §19 W4 / §20)。

`/proc/<pid>/cmdline` は同一 host の他 process から読める。したがって「secret を argv へ置かない」
はコメントで守る約束ではなく、実行時の性質として固定する必要がある。本 test は client の
`executable` を stub へ差し替え、実際に起動された argv・環境変数・stdin を記録して検査する。
HTTPS server との疎通そのものは本機で再現できないが、**渡し方**はここで完全に閉じられる。
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from projectmind.agent.repository_client import (
    GitCommandRepositoryClient,
    RepositoryCredential,
    SvnCommandRepositoryClient,
)

_SECRET = "s3cr3t-token-value"
_USERNAME = "deploy-user"

# 子 process へ渡してよい環境変数。Worker が持つ DB URL や KEK が混ざっていないことを、
# 名前の allowlist ではなく「この集合以外が無い」ことで確認する。
_ALLOWED_GIT_ENV = {
    "PATH",
    "HOME",
    "LC_ALL",
    "GIT_TERMINAL_PROMPT",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
}
_ALLOWED_SVN_ENV = {"PATH", "HOME", "LC_ALL"}


def _stub(root: Path, *, name: str, responses: dict[str, str]) -> Path:
    """argv/env/stdin を記録し、部分一致で固定出力を返す stub 実行 file を作る。"""

    record = root / f"{name}-invocations.jsonl"
    script = root / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"record = {str(record)!r}\n"
        f"responses = {responses!r}\n"
        "payload = sys.stdin.read()\n"
        "with open(record, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({\n"
        "        'argv': sys.argv,\n"
        "        'env': dict(os.environ),\n"
        "        'stdin': payload,\n"
        "    }) + '\\n')\n"
        "for marker, output in responses.items():\n"
        "    if marker in sys.argv:\n"
        "        sys.stdout.write(output)\n"
        "        break\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR)
    return record


def _invocations(record: Path) -> list[dict[str, Any]]:
    """記録された起動を古い順で読む。"""

    if not record.is_file():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line]


def _subcommands(invocations: list[dict[str, Any]]) -> list[str]:
    """各起動の subcommand を取り出す (`-C <dir>` などの大域 option を読み飛ばす)。"""

    known = {"clone", "rev-parse", "checkout", "ls-remote", "fetch", "info", "list", "cat"}
    found: list[str] = []
    for item in invocations:
        found.extend(argument for argument in item["argv"][1:] if argument in known)
    return found


@pytest.mark.asyncio
async def test_git_credential_is_injected_through_the_environment_not_argv(
    tmp_path: Path,
) -> None:
    """git の凭据は環境変数の Authorization header として渡り、argv には現れない。"""

    record = _stub(
        tmp_path,
        name="git-stub",
        responses={"rev-parse": "a" * 40 + "\n"},
    )
    client = GitCommandRepositoryClient(
        command_timeout_seconds=30, executable=str(tmp_path / "git-stub")
    )

    async with client.open(
        uri="https://git.example.invalid/project.git",
        revision="main",
        credential=RepositoryCredential(username=_USERNAME, secret=_SECRET),
    ) as session:
        assert session.revision == "a" * 40

    invocations = _invocations(record)
    assert _subcommands(invocations) == ["clone", "rev-parse"]
    for item in invocations:
        # secret も user:secret の連結も argv には一切現れない。
        assert not any(_SECRET in argument for argument in item["argv"])
        assert not any(f"{_USERNAME}:{_SECRET}" in argument for argument in item["argv"])
        # 最小環境のみ。Worker の設定 (DB URL、KEK 等) は継承しない。
        assert set(item["env"]) <= _ALLOWED_GIT_ENV
        assert not any(key.startswith("PROJECTMIND_") for key in item["env"])
    header = invocations[0]["env"]
    assert header["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert header["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    # header は user:secret の base64。復号して一致を確認する (形だけの検査にしない)。
    encoded = header["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
    assert base64.b64decode(encoded).decode("utf-8") == f"{_USERNAME}:{_SECRET}"
    assert header["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.asyncio
async def test_git_without_credential_injects_no_authorization_header(tmp_path: Path) -> None:
    """凭据が無い binding では Authorization header 自体を作らない。"""

    record = _stub(tmp_path, name="git-stub", responses={"rev-parse": "b" * 40 + "\n"})
    client = GitCommandRepositoryClient(
        command_timeout_seconds=30, executable=str(tmp_path / "git-stub")
    )

    async with client.open(
        uri="https://git.example.invalid/project.git", revision="main", credential=None
    ):
        pass

    for item in _invocations(record):
        assert "GIT_CONFIG_KEY_0" not in item["env"]
        assert "GIT_CONFIG_COUNT" not in item["env"]


@pytest.mark.asyncio
async def test_svn_password_travels_through_stdin_only(tmp_path: Path) -> None:
    """svn の password は stdin だけを通り、argv には user 名しか現れない。"""

    record = _stub(
        tmp_path,
        name="svn-stub",
        responses={
            "info": '<?xml version="1.0"?><info><entry revision="7" kind="dir"/></info>'
        },
    )
    client = SvnCommandRepositoryClient(
        command_timeout_seconds=30, executable=str(tmp_path / "svn-stub")
    )

    async with client.open(
        uri="https://svn.example.invalid/project",
        revision="HEAD",
        credential=RepositoryCredential(username=_USERNAME, secret=_SECRET),
    ) as session:
        assert session.revision == "7"

    invocations = _invocations(record)
    assert invocations
    for item in invocations:
        assert "--password-from-stdin" in item["argv"]
        assert "--no-auth-cache" in item["argv"]
        assert not any(_SECRET in argument for argument in item["argv"])
        assert item["stdin"] == _SECRET
        assert set(item["env"]) <= _ALLOWED_SVN_ENV
        assert not any(key.startswith("PROJECTMIND_") for key in item["env"])
    assert _USERNAME in invocations[0]["argv"]


@pytest.mark.asyncio
async def test_svn_without_credential_sends_no_stdin_payload(tmp_path: Path) -> None:
    """凭据が無ければ password option も stdin payload も付けない。"""

    record = _stub(
        tmp_path,
        name="svn-stub",
        responses={
            "info": '<?xml version="1.0"?><info><entry revision="3" kind="dir"/></info>'
        },
    )
    client = SvnCommandRepositoryClient(
        command_timeout_seconds=30, executable=str(tmp_path / "svn-stub")
    )

    async with client.open(
        uri="https://svn.example.invalid/project", revision="HEAD", credential=None
    ):
        pass

    for item in _invocations(record):
        assert "--password-from-stdin" not in item["argv"]
        assert item["stdin"] == ""


@pytest.mark.asyncio
async def test_write_session_reuses_the_same_credential_boundary(tmp_path: Path) -> None:
    """承認済み書き込み (§20) も同じ環境変数注入を通り、argv へ落ちない。"""

    record = _stub(tmp_path, name="git-stub", responses={"rev-parse": "c" * 40 + "\n"})
    client = GitCommandRepositoryClient(
        command_timeout_seconds=30, executable=str(tmp_path / "git-stub")
    )

    async with client.open_writable(
        uri="https://git.example.invalid/project.git",
        revision="main",
        credential=RepositoryCredential(username=_USERNAME, secret=_SECRET),
    ) as session:
        assert session.base_revision == "c" * 40

    invocations = _invocations(record)
    assert _subcommands(invocations) == ["clone", "rev-parse", "checkout"]
    for item in invocations:
        assert not any(_SECRET in argument for argument in item["argv"])
        assert item["env"]["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        assert set(item["env"]) <= _ALLOWED_GIT_ENV


def test_stub_environment_does_not_leak_worker_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker の環境変数が設定されていても、子 process へは継承されない。"""

    monkeypatch.setenv("PROJECTMIND_DATABASE_URL", "postgresql://user:password@db/projectmind")
    monkeypatch.setenv("PROJECTMIND_MANAGED_SECRET_KEK", "v1:" + "A" * 44)

    from projectmind.agent.repository_client import _git_environment, _svn_environment

    git_environment = _git_environment(
        home=Path("/tmp"), credential=RepositoryCredential(username="u", secret="p")
    )
    svn_environment = _svn_environment(home=Path("/tmp"))

    for environment in (git_environment, svn_environment):
        assert not any(key.startswith("PROJECTMIND_") for key in environment)
        assert all("password@db" not in value for value in environment.values())
    assert os.environ["PROJECTMIND_DATABASE_URL"].startswith("postgresql://")
