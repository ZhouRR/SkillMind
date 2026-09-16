"""配備診断は生成 identity を読むだけで、資格情報やモデル実行を必要としない。"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.ops import runtime_identity as diagnostic


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch):
    """実行用 method を持たない宣言 fake と、機密を含む未参照設定を用意する。"""

    settings = SimpleNamespace(
        agent_sdk="codex", queue_name="fixture:runs", worker_dispatch_enabled=True,
        scheduling_enabled=False, database_url="must-not-appear", managed_secret_kek="private",
    )
    features = SimpleNamespace(deferred=False, database_writes=False, document_writes=True, git_writes=False)
    identity = {"prompt_checksum": "sha256:" + "a" * 64}
    profile = {"model": "test-model", "reasoning_effort": "medium"}
    components = (
        SimpleNamespace(runtime_profile=profile), SimpleNamespace(checksum="catalog"),
        SimpleNamespace(to_dict=lambda: deepcopy(identity)), "test-model",
    )
    monkeypatch.setattr(diagnostic, "build_skill_interpreter", lambda _: components)
    monkeypatch.setattr(diagnostic, "configured_execution_features", lambda _: features)
    return settings, features, identity, profile


def test_same_configuration_has_same_signature_without_credentials(runtime) -> None:
    """container の役割や credential を比較項目にせず、同じ構成を同じ値へ写す。"""

    settings, _, _, _ = runtime
    first = diagnostic.runtime_report(settings)
    second = diagnostic.runtime_report(settings)
    assert first == second and first["interpreter_available"] is True
    text = canonical_json(first)
    assert "must-not-appear" not in text and "private" not in text
    assert "database_url" not in text and "managed_secret_kek" not in text


@pytest.mark.parametrize("change", ["prompt", "effort", "queue", "dispatch", "feature"])
def test_real_configuration_drift_changes_signature(runtime, change) -> None:
    """公開 identity、生成設定、配送先/機能の差を取りこぼさない。"""

    settings, features, identity, profile = runtime
    before = sha256_hex(canonical_json(diagnostic.runtime_report(settings)))
    if change == "prompt":
        identity["prompt_checksum"] = "sha256:" + "b" * 64
    elif change == "effort":
        profile["reasoning_effort"] = "high"
    elif change == "queue":
        settings.queue_name = "another:runs"
    elif change == "dispatch":
        settings.worker_dispatch_enabled = False
    else:
        features.document_writes = False
    assert before != sha256_hex(canonical_json(diagnostic.runtime_report(settings)))


def test_unavailable_interpreter_is_reported_not_promoted_to_runtime_gate(runtime, monkeypatch) -> None:
    """未設定を明示しつつ、診断のために import/旧 Run を拒否しない。"""

    settings, _, _, _ = runtime
    monkeypatch.setattr(diagnostic, "build_skill_interpreter", lambda _: (None, None, None, None))
    report = diagnostic.runtime_report(settings)
    assert report["interpreter_available"] is False
    assert report["interpreter"] is None and report["runtime_profile"] is None
    assert report["queue_name"] == settings.queue_name
