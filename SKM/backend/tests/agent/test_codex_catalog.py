"""同梱外 model の discovery と、原能力・資格・取消境界を検証する。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from skillmind.agent import codex_catalog, codex_runtime
from skillmind.agent.codex_catalog import CodexCatalogError, platform_model_catalog
from skillmind.agent.codex_runtime import CodexRuntimeConfiguration, prepare_codex_config


def entry(model="gpt-6-sol", effort="high"):
    """モデル識別と能力を変更していないことを検証する合成 metadata。"""
    return {
        "slug": model,
        "supported_reasoning_levels": [{"effort": effort}],
        "context_window": 272000,
        "default_reasoning_level": "medium",
        "tool_mode": "code_mode_only",
        "multi_agent_version": "v2",
        "use_responses_lite": True,
        "extra_metadata": {"preserved": True},
    }


def test_discovery_uses_same_credentials_proxy_and_preserves_remote_model(tmp_path, monkeypatch):
    """新 model の全能力を公式 entry から保持し、変更するのは Tool surface だけ。"""
    seen = []
    original = entry()
    environment = {"CODEX_HOME": str(tmp_path), "HTTPS_PROXY": "http://proxy.invalid:8080"}

    def command(args, **kwargs):
        """初回同梱には無いが、認証付き discovery で公開された model を返す。"""
        seen.append((args, kwargs))
        return SimpleNamespace(
            stdout=json.dumps({"models": [] if "--bundled" in args else [original]})
        )

    monkeypatch.setattr(codex_catalog.subprocess, "run", command)
    path = platform_model_catalog(
        cli="fixture", cwd=tmp_path, environment=environment, model="gpt-6-sol", effort="high"
    )
    assert len(seen) == 2 and "--bundled" not in seen[1][0]
    assert all(kwargs["env"] == environment and kwargs["cwd"] == tmp_path for _, kwargs in seen)
    assert seen[1][1]["timeout"] == 30
    assert 'forced_login_method="chatgpt"' in seen[1][0]
    actual = json.loads(path.read_text())["models"][0]
    assert actual == {**original, **codex_catalog._PLATFORM_TOOL_PROFILE}
    assert actual["slug"] == "gpt-6-sol" and actual["default_reasoning_level"] == "medium"
    assert path.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize(
    "models,reason",
    [
        ([], "model_not_found"),
        ([entry("different")], "model_not_found"),
        ([entry(effort="low")], "effort_unsupported"),
        ([entry(), entry()], "invalid"),
        ([{"slug": "gpt-6-sol"}], "invalid"),
    ],
)
def test_missing_ambiguous_or_unsupported_model_never_uses_another(
    tmp_path, monkeypatch, models, reason
):
    """更新失敗を別 model・推論低下・推定値で補わず、派生 catalog も作らない。"""
    monkeypatch.setattr(
        codex_catalog.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(
            stdout=json.dumps({"models": [] if "--bundled" in args else models})
        ),
    )
    with pytest.raises(CodexCatalogError, match=reason):
        platform_model_catalog(
            cli="fixture", cwd=tmp_path, environment={}, model="gpt-6-sol", effort="high"
        )
    assert not list(tmp_path.glob("platform-model-*"))


@pytest.mark.parametrize("failure", ["timeout", "process", "json"])
def test_discovery_errors_do_not_expose_command_output(tmp_path, monkeypatch, failure):
    """CLI stdout/stderr の秘密本文を public diagnostic に反射しない。"""

    def command(args, **kwargs):
        """同梱読取後の更新だけを失敗させる。"""
        if "--bundled" in args:
            return SimpleNamespace(stdout='{"models": []}')
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 30, output=b"private-fixture")
        if failure == "process":
            raise subprocess.CalledProcessError(1, args, stderr=b"private-fixture")
        return SimpleNamespace(stdout="private-fixture")

    monkeypatch.setattr(codex_catalog.subprocess, "run", command)
    with pytest.raises(CodexCatalogError) as caught:
        platform_model_catalog(
            cli="fixture", cwd=tmp_path, environment={}, model="selected", effort="high"
        )
    assert "private-fixture" not in str(caught.value)
    assert caught.value.detail.startswith("codex:model_catalog_")


def test_login_does_not_require_a_discoverable_model(tmp_path, monkeypatch):
    """未認証で見えない model を設定していてもログインを阻害しない。"""

    def unexpected(**kwargs):
        """ログイン準備が model discovery を呼んだら検出する。"""
        raise AssertionError("Login must not discover models")

    monkeypatch.setattr(codex_runtime, "platform_model_catalog", unexpected)
    monkeypatch.setattr(codex_runtime, "pinned_codex_cli", lambda: "fixture")
    config = CodexRuntimeConfiguration("gpt-6-sol", "high", tmp_path / "home").client_config(
        for_login=True
    )
    assert not any(value.startswith("model_catalog_json=") for value in config.config_overrides)
    assert 'model="gpt-6-sol"' in config.config_overrides
    assert 'model_reasoning_effort="high"' in config.config_overrides
    assert 'sandbox_mode="read-only"' in config.config_overrides


async def test_discovery_keeps_event_loop_alive_and_drains_on_cancel():
    """取消を普通の失敗に変えず、有界の同期準備を回収してから外へ戻す。"""
    started, released, finished = threading.Event(), threading.Event(), threading.Event()

    def configure():
        """明示解除まで待つ catalog subprocess の seam。"""
        started.set()
        try:
            assert released.wait(2)
            raise CodexCatalogError("codex:model_catalog_timeout")
        finally:
            finished.set()

    pending = asyncio.create_task(prepare_codex_config(SimpleNamespace(client_config=configure)))
    assert await asyncio.to_thread(started.wait, 1)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert finished.is_set()
