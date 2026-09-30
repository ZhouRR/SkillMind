"""同梱 catalog cache の失効・競合・file 検証と取消後の再準備を検証する。"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from skillmind.agent import codex_catalog, codex_runtime
from skillmind.agent.codex_catalog import CodexCatalogError, platform_model_catalog
from skillmind.agent.codex_runtime import CodexRuntimeConfiguration, prepare_codex_config
from skillmind.agent.warm_codex import WarmCodexSession


def _entry(model="fixture-model", efforts=("high", "medium")):
    """Tool 制約以外の未知 metadata も保存する合成 model を返す。"""
    return {
        "slug": model,
        "supported_reasoning_levels": [{"effort": effort} for effort in efforts],
        "context_window": 272000,
        "default_reasoning_level": "medium",
        "tool_mode": "code_mode_only",
        "extra_metadata": {"nested": ["original"]},
    }


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    """前後の test や既存 process cache に依存せず、全 test を cold 状態から始める。"""
    monkeypatch.setattr(codex_catalog, "_BUNDLED_CACHE", OrderedDict())
    monkeypatch.setattr(codex_catalog, "_BUNDLED_LOCK", threading.Lock())
    monkeypatch.setattr(codex_catalog, "_BUNDLED_FLIGHTS", {})
    yield
    assert not codex_catalog._BUNDLED_FLIGHTS


@pytest.fixture
def binary(tmp_path):
    """実行せず、identity の検証にだけ使う通常 executable を作る。"""
    path = tmp_path / "codex-fixture"
    path.write_bytes(b"synthetic executable v1\n")
    path.chmod(0o700)
    return path


@pytest.fixture
def catalog_process(monkeypatch):
    """外部 process を起動せず、discovery の入力と合成応答を保持する。"""
    process = SimpleNamespace(models=[_entry(), _entry("other-model")], calls=[])

    def command(args, **kwargs):
        """呼出し時点の CLI 引数・環境を記録する。"""
        process.calls.append((args, kwargs))
        return SimpleNamespace(stdout=json.dumps({"models": process.models}).encode())

    monkeypatch.setattr(codex_catalog.subprocess, "run", command)
    return process


def _catalog(binary, cwd, *, model="fixture-model", effort="high", version="fixture-v1",
             environment=None):
    """Cache key の各軸だけを変えられる共通の公開入口。"""
    return platform_model_catalog(
        cli=str(binary), cwd=cwd, environment={} if environment is None else environment,
        model=model, effort=effort, cli_version=version,
    )


def test_repeated_hit_invokes_bundled_discovery_once(binary, tmp_path, catalog_process):
    """同一準備は subprocess を省き、同じ内容と read-only file を返す。"""
    environment = {"CODEX_HOME": str(tmp_path / "home"), "HTTPS_PROXY": "http://proxy.invalid"}
    first = _catalog(binary, tmp_path, environment=environment)
    content = first.read_bytes()
    second = _catalog(binary, tmp_path, environment=environment)

    assert first == second
    assert second.read_bytes() == content
    assert stat.S_IMODE(first.stat().st_mode) == 0o400
    assert catalog_process.calls == [(
        [str(binary), "debug", "models", "--bundled"],
        {"cwd": tmp_path, "env": environment, "capture_output": True, "check": True,
         "timeout": 15},
    )]
    assert json.loads(content)["models"] == [
        {**catalog_process.models[0], **codex_catalog._PLATFORM_TOOL_PROFILE}
    ]
    assert list(codex_catalog._BUNDLED_CACHE.values()) == [content]
    assert isinstance(next(iter(codex_catalog._BUNDLED_CACHE.values())), bytes)


def test_cache_shares_only_bytes_across_homes_and_recreates_missing_file(
    binary, tmp_path, catalog_process,
):
    """別 home/環境にも immutable bytes だけを再利用し、欠けた file は再公開する。"""
    first_home, second_home = tmp_path / "first", tmp_path / "second"
    first_home.mkdir()
    second_home.mkdir()
    first = _catalog(binary, first_home, environment={"CODEX_HOME": str(first_home)})
    content = first.read_bytes()
    first.unlink()
    second = _catalog(binary, second_home, environment={"CODEX_HOME": str(second_home)})
    rebuilt = _catalog(binary, first_home, environment={"CODEX_HOME": str(first_home)})

    assert second.parent == second_home and rebuilt.parent == first_home
    assert second.read_bytes() == rebuilt.read_bytes() == content
    assert len(catalog_process.calls) == 1
    assert not list(tmp_path.rglob(".model-*"))


@pytest.mark.parametrize("axis", ["version", "model", "effort", "profile"])
def test_semantic_key_changes_trigger_one_new_discovery(
    binary, tmp_path, catalog_process, monkeypatch, axis,
):
    """CLI version/model/effort/Tool profile の変更を以前の成功と混同しない。"""
    _catalog(binary, tmp_path)
    options = {}
    if axis == "profile":
        monkeypatch.setattr(codex_catalog, "_PLATFORM_TOOL_PROFILE", {
            **codex_catalog._PLATFORM_TOOL_PROFILE, "experimental_supported_tools": ["fixture"],
        })
    else:
        options[axis] = {"version": "fixture-v2", "model": "other-model",
                         "effort": "medium"}[axis]
    changed = _catalog(binary, tmp_path, **options)
    assert _catalog(binary, tmp_path, **options) == changed
    assert len(catalog_process.calls) == 2
    assert len(codex_catalog._BUNDLED_CACHE) == 2


@pytest.mark.parametrize("change", ["replace", "write", "mode", "path"])
def test_executable_identity_changes_invalidate_cache(
    binary, tmp_path, catalog_process, change,
):
    """同パスの inode 交換・内容/権限変更・別絶対パスを新しい CLI と扱う。"""
    _catalog(binary, tmp_path)
    before = binary.stat()
    if change in {"replace", "path"}:
        replacement = tmp_path / "replacement"
        replacement.write_bytes(binary.read_bytes())
        replacement.chmod(stat.S_IMODE(before.st_mode))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        if change == "replace":
            replacement.replace(binary)
        else:
            binary = replacement
    elif change == "write":
        binary.write_bytes(b"synthetic executable v2\n")
        os.utime(binary, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    else:
        binary.chmod(0o500)
    _catalog(binary, tmp_path)
    _catalog(binary, tmp_path)
    assert len(catalog_process.calls) == 2


@pytest.mark.parametrize("kind", ["relative", "missing", "symlink", "directory"])
def test_unresolved_or_nonregular_executable_is_never_cached(
    binary, tmp_path, catalog_process, kind,
):
    """解決済み通常 file 以外の CLI は成功しても後続 discovery を省略しない。"""
    if kind == "relative":
        cli = "codex-fixture"
    elif kind == "missing":
        cli = tmp_path / "missing"
    elif kind == "directory":
        cli = tmp_path
    else:
        cli = tmp_path / "linked-cli"
        cli.symlink_to(binary)
    _catalog(cli, tmp_path)
    _catalog(cli, tmp_path)
    assert len(catalog_process.calls) == 2
    assert not codex_catalog._BUNDLED_CACHE


def test_binary_replaced_during_discovery_fails_without_poisoning_cache(
    binary, tmp_path, monkeypatch,
):
    """Discovery 中に実体が変わった結果を保存せず、次の安定した準備で回復する。"""
    calls = []

    def command(args, **kwargs):
        """最初の応答直前だけ同パスの executable を交換する。"""
        calls.append(args)
        if len(calls) == 1:
            replacement = tmp_path / "replacement"
            replacement.write_bytes(binary.read_bytes())
            replacement.chmod(0o700)
            replacement.replace(binary)
        return SimpleNamespace(stdout=json.dumps({"models": [_entry()]}))

    monkeypatch.setattr(codex_catalog.subprocess, "run", command)
    with pytest.raises(CodexCatalogError, match="cli_changed"):
        _catalog(binary, tmp_path)
    assert not codex_catalog._BUNDLED_CACHE
    assert not list(tmp_path.glob("platform-model-*"))
    _catalog(binary, tmp_path)
    _catalog(binary, tmp_path)
    assert len(calls) == 2


def test_lru_is_bounded_and_hits_refresh_recency(binary, tmp_path, catalog_process, monkeypatch):
    """LRU の上限と hit の更新により、最古の未使用 entry だけを再取得する。"""
    monkeypatch.setattr(codex_catalog, "_CACHE_LIMIT", 2)
    catalog_process.models = [_entry(name) for name in ("a", "b", "c")]
    for model in ("a", "b", "a", "c"):
        _catalog(binary, tmp_path, model=model)
    assert len(catalog_process.calls) == 3
    assert [key[3] for key in codex_catalog._BUNDLED_CACHE] == ["a", "c"]
    _catalog(binary, tmp_path, model="a")
    _catalog(binary, tmp_path, model="b")
    assert len(catalog_process.calls) == 4
    assert [key[3] for key in codex_catalog._BUNDLED_CACHE] == ["a", "b"]


@pytest.mark.parametrize("over_limit", [False, True])
def test_entry_size_limit_includes_boundary_but_does_not_cache_oversized_results(
    binary, tmp_path, catalog_process, monkeypatch, over_limit,
):
    """上限と同じ bytes は保存し、超過時も正しい結果を返すが保存しない。"""
    size = len(codex_catalog._model_content(catalog_process.models[0]))
    monkeypatch.setattr(codex_catalog, "_CACHE_ENTRY_BYTES", size - int(over_limit))
    first = _catalog(binary, tmp_path)
    assert len(first.read_bytes()) == size
    assert _catalog(binary, tmp_path) == first
    assert len(catalog_process.calls) == (2 if over_limit else 1)
    assert len(codex_catalog._BUNDLED_CACHE) == (0 if over_limit else 1)


@pytest.mark.parametrize("failure", [
    "timeout", "process", "os", "json", "shape", "duplicate", "reasoning",
])
def test_failed_or_invalid_bundled_discovery_is_retried(
    binary, tmp_path, monkeypatch, failure,
):
    """失敗を cache に残さず、再呼出しでは必ず公式 discovery を再試行する。"""
    calls = []

    def command(args, **kwargs):
        """最初だけ分類済み失敗を返し、次回以降は正常 metadata を返す。"""
        calls.append(args)
        if len(calls) == 1:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 15, output=b"private-fixture")
            if failure == "process":
                raise subprocess.CalledProcessError(1, args, stderr=b"private-fixture")
            if failure == "os":
                raise OSError("private-fixture")
            if failure == "json":
                return SimpleNamespace(stdout=b"private-fixture")
            invalid = {
                "shape": {"models": ["private-fixture"]},
                "duplicate": {"models": [_entry(), _entry()]},
                "reasoning": {"models": [{"slug": "fixture-model"}]},
            }
            return SimpleNamespace(stdout=json.dumps(invalid[failure]))
        return SimpleNamespace(stdout=json.dumps({"models": [_entry()]}))

    monkeypatch.setattr(codex_catalog.subprocess, "run", command)
    with pytest.raises(CodexCatalogError) as caught:
        _catalog(binary, tmp_path)
    assert "private-fixture" not in str(caught.value)
    assert not codex_catalog._BUNDLED_CACHE
    assert not list(tmp_path.glob("platform-model-*"))
    _catalog(binary, tmp_path)
    _catalog(binary, tmp_path)
    assert len(calls) == 2


@pytest.mark.parametrize("bundled", [[], [_entry(efforts=("low",))]])
def test_remote_model_and_reasoning_metadata_are_refreshed_every_call(
    binary, tmp_path, monkeypatch, bundled,
):
    """同梱不足の remote 応答は毎回取得し、以前の model/effort 成功を流用しない。"""
    calls = []

    def command(args, **kwargs):
        """Remote metadata を毎回変え、動的応答の stale reuse を検出する。"""
        calls.append((args, kwargs))
        models = bundled if "--bundled" in args else [{**_entry(), "revision": len(calls)}]
        return SimpleNamespace(stdout=json.dumps({"models": models}))

    monkeypatch.setattr(codex_catalog.subprocess, "run", command)
    environment = {"CODEX_HOME": str(tmp_path / "dedicated"), "HTTPS_PROXY": "http://proxy.invalid"}
    first = _catalog(binary, tmp_path, environment=environment)
    second = _catalog(binary, tmp_path, environment=environment)
    assert json.loads(first.read_bytes())["models"][0]["revision"] == 2
    assert json.loads(second.read_bytes())["models"][0]["revision"] == 4
    assert ["--bundled" in args for args, _ in calls] == [True, False, True, False]
    assert all(kwargs["env"] == environment for _, kwargs in calls)
    assert calls[1] == (
        [str(binary), "debug", "models", "-c", 'cli_auth_credentials_store="file"',
         "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"'],
        {"cwd": tmp_path, "env": environment, "capture_output": True, "check": True,
         "timeout": 30},
    )
    assert not codex_catalog._BUNDLED_CACHE


@pytest.mark.parametrize("reason", ["model_not_found", "effort_unsupported", "invalid"])
def test_failed_remote_discovery_does_not_poison_later_success(
    binary, tmp_path, monkeypatch, reason,
):
    """同梱不足と remote 失敗を保存せず、後から利用可能になるモデルを取得する。"""
    calls = []

    def command(args, **kwargs):
        """一度だけ remote の能力不足または不正 metadata を返す。"""
        calls.append(args)
        if "--bundled" in args:
            models = []
        elif len(calls) == 2:
            models = {"model_not_found": [], "effort_unsupported": [_entry(efforts=("low",))],
                      "invalid": [_entry(), _entry()]}[reason]
        else:
            models = [_entry()]
        return SimpleNamespace(stdout=json.dumps({"models": models}))

    monkeypatch.setattr(codex_catalog.subprocess, "run", command)
    with pytest.raises(CodexCatalogError, match=reason):
        _catalog(binary, tmp_path)
    assert not codex_catalog._BUNDLED_CACHE
    _catalog(binary, tmp_path)
    _catalog(binary, tmp_path)
    assert len(calls) == 6
    assert not codex_catalog._BUNDLED_CACHE


def test_concurrent_cold_preparations_share_one_discovery_and_complete_file(
    binary, tmp_path, monkeypatch,
):
    """同時 cold 呼出しを一本化し、公開 file を全 reader が完全な bytes で読む。"""
    workers = 6
    ready = threading.Barrier(workers + 1)
    started, release = threading.Event(), threading.Event()
    calls = []

    def command(args, **kwargs):
        """主 thread の解除まで最初の bundled discovery を保持する。"""
        calls.append(args)
        started.set()
        assert release.wait(5), "discovery was not released"
        return SimpleNamespace(stdout=json.dumps({"models": [_entry()]}))

    def prepare():
        """全 worker の開始位置を合わせる。"""
        ready.wait(timeout=5)
        path = _catalog(binary, tmp_path)
        return path, path.read_bytes()

    monkeypatch.setattr(codex_catalog.subprocess, "run", command)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(prepare) for _ in range(workers)]
        try:
            ready.wait(timeout=5)
            assert started.wait(5)
            assert len(calls) == 1
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
    assert len(calls) == 1
    assert len(set(results)) == 1
    assert stat.S_IMODE(results[0][0].stat().st_mode) == 0o400
    assert not list(tmp_path.glob(".model-*"))


def test_mutable_source_metadata_cannot_contaminate_cached_bytes(binary, tmp_path, monkeypatch):
    """元の dict/list や出力を decode した dict の変更を共有 bytes に反映させない。"""
    original, calls = _entry(), []

    def read(*args, **kwargs):
        """同じ可変 metadata を返し、cache の参照共有を検出する。"""
        calls.append(kwargs)
        return [original]

    monkeypatch.setattr(codex_catalog, "_read_catalog", read)
    first = _catalog(binary, tmp_path)
    content = first.read_bytes()
    original["extra_metadata"]["nested"].append("mutated")
    original["supported_reasoning_levels"].clear()
    decoded = json.loads(content)
    decoded["models"][0]["extra_metadata"]["nested"].clear()
    assert _catalog(binary, tmp_path).read_bytes() == content
    assert len(calls) == 1
    assert json.loads(content)["models"][0]["extra_metadata"]["nested"] == ["original"]


@pytest.mark.parametrize("replacement", ["same_size", "truncated", "appended", "symlink",
                                         "dangling", "directory"])
def test_cache_hits_revalidate_generated_file_and_reject_tampering(
    binary, tmp_path, catalog_process, replacement,
):
    """Cache hit でも改変・symlink・非通常 file を拒否し、危険な既存 path を上書きしない。"""
    path = _catalog(binary, tmp_path)
    content = path.read_bytes()
    path.unlink()
    target = tmp_path / "target"
    if replacement in {"symlink", "dangling"}:
        if replacement == "symlink":
            target.write_bytes(content)
        path.symlink_to(target)
    elif replacement == "directory":
        path.mkdir()
    else:
        changed = {"same_size": b"x" * len(content), "truncated": content[:-1],
                   "appended": content + b"x"}[replacement]
        path.write_bytes(changed)
    before = path.lstat()
    with pytest.raises(ValueError, match="catalog changed"):
        _catalog(binary, tmp_path)
    assert path.lstat() == before
    assert len(catalog_process.calls) == 1
    if replacement == "symlink":
        assert target.read_bytes() == content
    elif replacement == "dangling":
        assert not target.exists()


@pytest.mark.parametrize("racer", ["valid_file", "tampered_file", "symlink", "dangling"])
def test_publication_revalidates_a_path_created_during_temporary_file_preparation(
    binary, tmp_path, catalog_process, monkeypatch, racer,
):
    """二度目の存在確認前に現れた path を維持し、正常な通常 file だけを採用する。"""
    original_chmod = Path.chmod
    target = tmp_path / "target"
    published = []

    def chmod(source, mode, *args, **kwargs):
        """完成済み temporary file の chmod 後に競合 path を作る。"""
        result = original_chmod(source, mode, *args, **kwargs)
        if source.name.startswith(".model-"):
            content = source.read_bytes()
            assert stat.S_IMODE(source.stat().st_mode) == 0o400
            destination = tmp_path / f"platform-model-{codex_catalog.sha256_hex(content)}.json"
            published.append((destination, content))
            if racer in {"symlink", "dangling"}:
                if racer == "symlink":
                    target.write_bytes(content)
                destination.symlink_to(target)
            else:
                destination.write_bytes(content if racer == "valid_file" else b"racer")
        return result

    def unexpected_replace(*args, **kwargs):
        """競合 path が存在する場合に上書きへ進んだら検出する。"""
        raise AssertionError("Existing catalog must be validated without replacement")

    monkeypatch.setattr(Path, "chmod", chmod)
    monkeypatch.setattr(codex_catalog.os, "replace", unexpected_replace)
    if racer == "valid_file":
        result = _catalog(binary, tmp_path)
        assert result.read_bytes() == published[0][1]
    else:
        with pytest.raises(ValueError, match="catalog changed"):
            _catalog(binary, tmp_path)
    destination, content = published[0]
    if racer in {"symlink", "dangling"}:
        assert destination.is_symlink()
        if racer == "symlink":
            assert target.read_bytes() == content
        else:
            assert not target.exists()
    elif racer == "tampered_file":
        assert destination.read_bytes() == b"racer"
    assert len(catalog_process.calls) == 1
    assert not list(tmp_path.glob(".model-*"))


def test_publication_failure_cleans_up_temporary_file_and_allows_retry(
    binary, tmp_path, catalog_process, monkeypatch,
):
    """既存 publication 操作の失敗では未完成 file を残さず、cache bytes で再試行できる。"""
    original_replace = os.replace

    def denied_replace(*args, **kwargs):
        """通常の filesystem failure を模擬する。"""
        raise PermissionError("fixture publication denied")

    monkeypatch.setattr(codex_catalog.os, "replace", denied_replace)
    with pytest.raises(PermissionError, match="publication denied"):
        _catalog(binary, tmp_path)
    assert not list(tmp_path.glob(".model-*"))
    assert not list(tmp_path.glob("platform-model-*"))
    monkeypatch.setattr(codex_catalog.os, "replace", original_replace)
    assert _catalog(binary, tmp_path).is_file()
    assert len(catalog_process.calls) == 1


@pytest.mark.parametrize("racer", ["same_file", "changed_file", "symlink"])
def test_validation_rechecks_path_replacement_between_lstat_and_open(
    binary, tmp_path, catalog_process, monkeypatch, racer,
):
    """同内容の inode 交換は再検証し、異なる内容と symlink は拒否する。"""
    path = _catalog(binary, tmp_path)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"changed" if racer == "changed_file" else path.read_bytes())
    original_open = os.open
    raced = []

    def open_file(target, flags, *args, **kwargs):
        """対象 file の open 直前にだけ inode を交換する。"""
        if Path(target) == path and not raced:
            raced.append(True)
            if racer in {"same_file", "changed_file"}:
                replacement.replace(path)
            else:
                path.unlink()
                path.symlink_to(replacement)
        return original_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(codex_catalog.os, "open", open_file)
    if racer == "same_file":
        assert _catalog(binary, tmp_path) == path
    else:
        with pytest.raises((ValueError, OSError)):
            _catalog(binary, tmp_path)
    assert raced and len(catalog_process.calls) == 1


@pytest.mark.parametrize("racer", ["same_file", "changed_file", "symlink"])
def test_validation_rechecks_path_replacement_after_open(
    binary, tmp_path, catalog_process, monkeypatch, racer,
):
    """読取中に交換された path は再検証し、安定した同内容の通常 file だけを返す。"""
    path = _catalog(binary, tmp_path)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"changed" if racer == "changed_file" else path.read_bytes())
    original_fstat = os.fstat
    raced = []

    def fstat(descriptor):
        """最初の fd 照合後に別 inode または symlink を公開する。"""
        value = original_fstat(descriptor)
        if not raced:
            raced.append(True)
            if racer == "symlink":
                path.unlink()
                path.symlink_to(replacement)
            else:
                replacement.replace(path)
        return value

    monkeypatch.setattr(codex_catalog.os, "fstat", fstat)
    if racer == "same_file":
        assert _catalog(binary, tmp_path) == path
    else:
        with pytest.raises(ValueError, match="catalog changed"):
            _catalog(binary, tmp_path)
    assert raced and len(catalog_process.calls) == 1


def test_validation_bounds_retries_for_a_continuously_replaced_path(
    binary, tmp_path, catalog_process, monkeypatch,
):
    """同じ bytes でも inode が安定しない場合は三回で止め、無限に待機しない。"""
    path = _catalog(binary, tmp_path)
    content = path.read_bytes()
    original_fstat = os.fstat
    replacements = []

    def fstat(descriptor):
        """検証のたびに公開 path を交換し、再試行の上限を数える。"""
        value = original_fstat(descriptor)
        replacement = tmp_path / "replacement"
        replacement.write_bytes(content)
        replacement.replace(path)
        replacements.append(True)
        return value

    monkeypatch.setattr(codex_catalog.os, "fstat", fstat)
    with pytest.raises(ValueError, match="catalog changed"):
        _catalog(binary, tmp_path)
    assert len(replacements) == 3 and len(catalog_process.calls) == 1


@pytest.mark.parametrize("discovery_fails", [False, True])
async def test_cancelled_preparation_drains_discovery_without_poisoning_warm_client(
    binary, tmp_path, monkeypatch, discovery_fails,
):
    """取消を維持して discovery を回収し、次の取得へ client/失敗を持ち越さない。"""
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls, clients = [], []
    configuration = CodexRuntimeConfiguration("fixture-model", "high", tmp_path / "home")
    holder = WarmCodexSession(uuid4(), object())

    def command(args, **kwargs):
        """最初の同期 discovery だけを明示的な解除まで待機させる。"""
        calls.append(args)
        if len(calls) == 1:
            started.set()
            try:
                assert release.wait(5), "discovery was not released"
                if discovery_fails:
                    raise subprocess.TimeoutExpired(args, 15)
            finally:
                finished.set()
        return SimpleNamespace(stdout=json.dumps({"models": [_entry()]}))

    async def factory():
        """準備完了後だけ SDK 相当の client を作る。"""
        config = await prepare_codex_config(configuration)
        client = SimpleNamespace(config=config, closed=False)

        def close():
            """実 process を持たない client の解放を記録する。"""
            client.closed = True

        client.close = close
        clients.append(client)
        return client

    monkeypatch.setattr(codex_runtime, "pinned_codex_cli", lambda: str(binary))
    monkeypatch.setattr(codex_catalog.subprocess, "run", command)
    pending = asyncio.create_task(holder.acquire(factory, None))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        assert holder.busy and holder.client is None and not clients
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert finished.is_set()
    assert holder.client is None and not holder.busy and not clients
    assert len(codex_catalog._BUNDLED_CACHE) == (0 if discovery_fails else 1)

    client, is_new = await holder.acquire(factory, None)
    assert is_new and clients == [client]
    assert len(calls) == (2 if discovery_fails else 1)
    assert len(codex_catalog._BUNDLED_CACHE) == 1
    await holder.close_client()
    assert client.closed


def test_cached_catalog_does_not_reuse_client_environment_or_mcp_configuration(
    binary, tmp_path, catalog_process, monkeypatch,
):
    """Catalog の共用は新規 config と呼出しごとの資格・MCP 設定の生成を妨げない。"""
    pinned_calls = []

    def pinned_cli():
        """Cache hit でも配布 version/checksum の guard に到達することを記録する。"""
        pinned_calls.append(True)
        return str(binary)

    monkeypatch.setattr(codex_runtime, "pinned_codex_cli", pinned_cli)
    first_runtime = CodexRuntimeConfiguration("fixture-model", "high", tmp_path / "first")
    second_runtime = CodexRuntimeConfiguration("fixture-model", "high", tmp_path / "second")
    first = first_runtime.client_config(mcp={"url": "http://127.0.0.1:10001/mcp"})
    first.env["CODEX_HOME"] = "mutated"
    second = second_runtime.client_config(mcp={"url": "http://127.0.0.1:10002/mcp"})
    assert len(catalog_process.calls) == 1 and len(pinned_calls) == 2
    assert first is not second and first.env is not second.env
    assert second.env["CODEX_HOME"] == str(second_runtime.home)
    assert second.cwd == str(second_runtime.home / "runtime")
    assert 'mcp_servers.skillmind.url="http://127.0.0.1:10001/mcp"' not in second.config_overrides
    assert 'mcp_servers.skillmind.url="http://127.0.0.1:10002/mcp"' in second.config_overrides


@pytest.mark.parametrize("limit", [1, 2])
def test_different_keys_do_not_wait_and_inflight_tracking_is_bounded(
    binary, tmp_path, monkeypatch, limit,
):
    """別 key は進行中 discovery を待たず、追跡上限では元の uncached 読取に戻る。"""
    started, release = threading.Event(), threading.Event()
    calls = []
    first_home, other_home = tmp_path / "first", tmp_path / "other"
    first_home.mkdir()
    other_home.mkdir()
    monkeypatch.setattr(codex_catalog, "_CACHE_LIMIT", limit)

    def command(args, **kwargs):
        """最初の key だけを停止し、他方の応答は即座に返す。"""
        calls.append(kwargs["cwd"])
        if kwargs["cwd"] == first_home:
            started.set()
            assert release.wait(5), "first key was not released"
        assert len(codex_catalog._BUNDLED_FLIGHTS) <= limit
        return SimpleNamespace(stdout=json.dumps({"models": [_entry(), _entry("other-model")]}))

    monkeypatch.setattr(codex_catalog.subprocess, "run", command)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_catalog, binary, first_home)
        try:
            assert started.wait(5)
            other = pool.submit(_catalog, binary, other_home, model="other-model")
            assert other.result(timeout=2).is_file()
            assert not first.done()
            assert len(codex_catalog._BUNDLED_FLIGHTS) == 1
            assert len(codex_catalog._BUNDLED_CACHE) == (0 if limit == 1 else 1)
        finally:
            release.set()
        assert first.result(timeout=5).is_file()
    assert not codex_catalog._BUNDLED_FLIGHTS
    _catalog(binary, other_home, model="other-model")
    assert calls.count(other_home) == (2 if limit == 1 else 1)


def test_same_key_wait_timeout_falls_back_without_caching_unowned_result(
    binary, tmp_path, catalog_process, monkeypatch,
):
    """先行取得が期限内に終わらなくても拒否せず、独立 discovery の結果を返す。"""
    monkeypatch.setattr(codex_catalog, "_BUNDLED_WAIT_SECONDS", 0.0)
    key = codex_catalog._bundled_key(str(binary), "fixture-v1", "fixture-model", "high")
    flight = threading.Event()
    codex_catalog._BUNDLED_FLIGHTS[key] = flight
    try:
        assert _catalog(binary, tmp_path).is_file()
        assert len(catalog_process.calls) == 1
        assert not codex_catalog._BUNDLED_CACHE
        assert list(codex_catalog._BUNDLED_FLIGHTS.items()) == [(key, flight)]
        assert not flight.is_set()
    finally:
        codex_catalog._BUNDLED_FLIGHTS.pop(key)
        flight.set()
    _catalog(binary, tmp_path)
    _catalog(binary, tmp_path)
    assert len(catalog_process.calls) == 2
    assert len(codex_catalog._BUNDLED_CACHE) == 1


def test_failed_owner_releases_waiter_to_retry_without_sharing_failure(
    binary, tmp_path, monkeypatch,
):
    """先行 discovery の失敗を待機者へ保存・伝播せず、独立した再取得を許す。"""
    started, release, waiting = threading.Event(), threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(codex_catalog, "_BUNDLED_WAIT_SECONDS", 5.0)

    def command(args, **kwargs):
        """所有者だけを失敗させ、待機者の再取得には有効な catalog を返す。"""
        calls.append(args)
        if len(calls) == 1:
            started.set()
            assert release.wait(5), "owner was not released"
            raise subprocess.TimeoutExpired(args, 15)
        return SimpleNamespace(stdout=json.dumps({"models": [_entry()]}))

    monkeypatch.setattr(codex_catalog.subprocess, "run", command)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(_catalog, binary, tmp_path)
        try:
            assert started.wait(5)
            flight = next(iter(codex_catalog._BUNDLED_FLIGHTS.values()))
            original_wait = flight.wait

            def wait(timeout=None):
                """待機者が既存 flight へ到達したことを主 thread に知らせる。"""
                waiting.set()
                return original_wait(timeout)

            monkeypatch.setattr(flight, "wait", wait)
            waiter = pool.submit(_catalog, binary, tmp_path)
            assert waiting.wait(5)
        finally:
            release.set()
        with pytest.raises(CodexCatalogError, match="timeout"):
            owner.result(timeout=5)
        assert waiter.result(timeout=5).is_file()
    assert len(calls) == 2
    assert not codex_catalog._BUNDLED_CACHE and not codex_catalog._BUNDLED_FLIGHTS
    _catalog(binary, tmp_path)
    _catalog(binary, tmp_path)
    assert len(calls) == 3


async def test_cancelled_waiter_fallback_drains_without_poisoning_owner_or_client(
    binary, tmp_path, monkeypatch,
):
    """同 key の待機超過後を取消しても同期取得を回収し、所有者の cache を壊さない。"""
    owner_started, owner_release = threading.Event(), threading.Event()
    waiter_started, waiter_release = threading.Event(), threading.Event()
    calls, clients = [], []
    configuration = CodexRuntimeConfiguration("fixture-model", "high", tmp_path / "home")
    holder = WarmCodexSession(uuid4(), object())
    monkeypatch.setattr(codex_catalog, "_BUNDLED_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(codex_runtime, "pinned_codex_cli", lambda: str(binary))

    def command(args, **kwargs):
        """Owner と fallback waiter を別々のイベントで制御する。"""
        calls.append(args)
        if len(calls) == 1:
            owner_started.set()
            assert owner_release.wait(5), "owner was not released"
        elif len(calls) == 2:
            waiter_started.set()
            assert waiter_release.wait(5), "waiter was not released"
        return SimpleNamespace(stdout=json.dumps({"models": [_entry()]}))

    async def factory():
        """Catalog 準備の取消後に client を作っていないことを確認する。"""
        config = await prepare_codex_config(configuration)
        client = SimpleNamespace(config=config, close=lambda: None)
        clients.append(client)
        return client

    monkeypatch.setattr(codex_catalog.subprocess, "run", command)
    owner = asyncio.create_task(asyncio.to_thread(
        _catalog, binary, tmp_path, version=codex_runtime.CODEX_CLI_VERSION,
    ))
    waiter = None
    try:
        assert await asyncio.to_thread(owner_started.wait, 5)
        waiter = asyncio.create_task(holder.acquire(factory, None))
        assert await asyncio.to_thread(waiter_started.wait, 5)
        waiter.cancel()
        await asyncio.sleep(0)
        assert not waiter.done() and holder.busy and not clients
        waiter_release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert holder.client is None and not holder.busy and not clients
        assert len(codex_catalog._BUNDLED_FLIGHTS) == 1
        assert not codex_catalog._BUNDLED_CACHE
    finally:
        waiter_release.set()
        owner_release.set()
        await owner
        if waiter is not None and not waiter.done():
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
    assert not codex_catalog._BUNDLED_FLIGHTS
    assert len(codex_catalog._BUNDLED_CACHE) == 1
    client, is_new = await holder.acquire(factory, None)
    assert is_new and clients == [client] and len(calls) == 2
    await holder.close_client()


@pytest.mark.parametrize("forbidden", ["home_config", "project_config", "home_symlink",
                                       "runtime_symlink"])
def test_cache_hit_still_rejects_new_host_configuration_or_symlink(
    binary, tmp_path, catalog_process, monkeypatch, forbidden,
):
    """成功済み catalog があっても各準備で host 設定・runtime/home 境界を再検証する。"""
    monkeypatch.setattr(codex_runtime, "pinned_codex_cli", lambda: str(binary))
    configuration = CodexRuntimeConfiguration("fixture-model", "high", tmp_path / "home")
    configuration.client_config()
    configuration.client_config()
    if forbidden == "home_config":
        (configuration.home / "config.toml").write_text("# forbidden host configuration\n")
    elif forbidden == "project_config":
        (configuration.home / "runtime" / ".codex").mkdir()
    else:
        target = (configuration.home if forbidden == "home_symlink"
                  else configuration.home / "runtime")
        moved = tmp_path / "moved-directory"
        target.rename(moved)
        target.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="must not contain"):
        configuration.client_config()
    assert len(catalog_process.calls) == 1


def test_cache_hit_does_not_bypass_pinned_distribution_failure(
    binary, tmp_path, catalog_process, monkeypatch,
):
    """配布実体の guard が失敗したら以前の catalog 成功を理由に準備を続行しない。"""
    monkeypatch.setattr(codex_runtime, "pinned_codex_cli", lambda: str(binary))
    configuration = CodexRuntimeConfiguration("fixture-model", "high", tmp_path / "home")
    configuration.client_config()

    def unavailable():
        """Version/checksum guard の拒否を実 CLI/モデルなしで模擬する。"""
        raise RuntimeError("Pinned Codex SDK/CLI distribution is unavailable")

    monkeypatch.setattr(codex_runtime, "pinned_codex_cli", unavailable)
    with pytest.raises(RuntimeError, match="distribution is unavailable"):
        configuration.client_config()
    assert len(catalog_process.calls) == 1



def test_waiter_rechecks_binary_identity_before_consuming_owner_cache(
    binary, tmp_path, monkeypatch,
):
    """待機解除と cache 読取の間に変わった executable へ古い成功 bytes を渡さない。"""
    started, release, waiting = threading.Event(), threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(codex_catalog, "_BUNDLED_WAIT_SECONDS", 5.0)

    def command(args, **kwargs):
        """最初の取得だけを待機者の登録まで保持する。"""
        calls.append(args)
        if len(calls) == 1:
            started.set()
            assert release.wait(5), "owner was not released"
        return SimpleNamespace(stdout=json.dumps({"models": [_entry()]}))

    monkeypatch.setattr(codex_catalog.subprocess, "run", command)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(_catalog, binary, tmp_path)
        try:
            assert started.wait(5)
            flight = next(iter(codex_catalog._BUNDLED_FLIGHTS.values()))
            original_wait = flight.wait

            def wait(timeout=None):
                """所有者の cache 公開後、待機者が読む前に実体を確実に交換する。"""
                waiting.set()
                assert original_wait(timeout), "owner did not finish"
                replacement = tmp_path / "replacement"
                replacement.write_bytes(binary.read_bytes())
                replacement.chmod(0o700)
                replacement.replace(binary)
                return True

            monkeypatch.setattr(flight, "wait", wait)
            waiter = pool.submit(_catalog, binary, tmp_path)
            assert waiting.wait(5)
        finally:
            release.set()
        assert owner.result(timeout=5).is_file()
        with pytest.raises(CodexCatalogError, match="cli_changed"):
            waiter.result(timeout=5)
    assert len(calls) == 1 and len(codex_catalog._BUNDLED_CACHE) == 1
    assert not codex_catalog._BUNDLED_FLIGHTS
    _catalog(binary, tmp_path)
    _catalog(binary, tmp_path)
    assert len(calls) == 2 and len(codex_catalog._BUNDLED_CACHE) == 2
