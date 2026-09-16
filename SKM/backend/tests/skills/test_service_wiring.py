"""共有 factory の設定一致と、モデル未設定時に追加の受付門禁がないことを検証する。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skillmind.skills import service_wiring as wiring


@pytest.fixture
def assembly(monkeypatch: pytest.MonkeyPatch):
    """外部 I/O を禁止し、実 factory が constructor に渡す値だけを捕捉する。"""

    settings = SimpleNamespace(contracts_dir="contracts", object_storage_bucket="fixture-bucket")
    sessions = MagicMock(side_effect=AssertionError("No database access during assembly"))
    features = SimpleNamespace(
        write_capabilities=frozenset({"database.write/v1"}),
        document_writes=False,
        provider_enabled=lambda cap, provider: provider != "disabled-provider",
    )
    monkeypatch.setattr(wiring, "configured_execution_features", lambda _: features)
    monkeypatch.setattr(wiring, "INSTALLED_PROVIDER_CAPABILITIES", {
        "repository.read/v1": frozenset({"git", "disabled-provider"}),
    })
    service = MagicMock(side_effect=lambda *args, **kwargs: SimpleNamespace(args=args, **kwargs))
    monkeypatch.setattr(wiring, "SkillService", service)
    monkeypatch.setattr(
        wiring, "DocumentResourceCatalog",
        lambda session, **kwargs: ("document", session, kwargs["library_target"]),
    )
    monkeypatch.setattr(wiring, "IntegrationResourceCatalog", lambda session: ("integration", session))
    monkeypatch.setattr(wiring, "CompositeProjectResourceCatalog", lambda items: items)
    return settings, sessions, features, service


def test_api_and_worker_receive_identical_settings_without_io(assembly) -> None:
    """入力・資源・実 Interpreter identity の組を、両 consumer に同じまま注入する。"""

    settings, sessions, _, _ = assembly
    storage, library = object(), object()
    components = (object(), object(), object(), "test-model")
    args = dict(
        session_factory=sessions, file_storage=storage, document_library_target=library,
        interpreter_components=components,
    )
    api = wiring.build_skill_service(settings, **args)
    worker = wiring.build_skill_service(settings, **args)
    assert vars(api) == vars(worker)
    assert api.interpreter is components[0]
    assert api.interpreter_identity is components[2]
    assert api.file_storage is storage and worker.file_storage is storage
    assert api.installed_provider_capabilities["repository.read/v1"] == frozenset({"git"})
    sessions.assert_not_called()


@pytest.mark.parametrize("components", [None, (None, None, None, None)])
def test_import_and_published_tasks_keep_a_service_without_interpreter(assembly, components) -> None:
    """未設定と保守 process の両方で、従来の deterministic service を構築できる。"""

    settings, sessions, _, _ = assembly
    value = wiring.build_skill_service(
        settings, session_factory=sessions, file_storage=None, document_library_target=None,
        interpreter_components=components,
    )
    assert value.interpreter is None and value.interpreter_identity is None
    assert value.capability_catalog is None and value.default_model is None
    assert value.args == (sessions, "contracts")
    assert value.resource_catalog
    sessions.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("configured", [False, True])
def test_document_writer_registration_needs_flag_and_library(assembly, enabled, configured) -> None:
    """factory は既存の Provider 条件を保ち、同期を理由に write を増やさない。"""

    settings, sessions, features, _ = assembly
    features.document_writes = enabled
    value = wiring.build_skill_service(
        settings, session_factory=sessions, file_storage=None,
        document_library_target=object() if configured else None,
    )
    assert (wiring.DOCUMENT_WRITE_CAPABILITY in value.installed_provider_capabilities) is (
        enabled and configured
    )
    assert value.registered_write_capabilities == features.write_capabilities
    for capability in wiring.DOCUMENT_CAPABILITIES:
        assert value.installed_provider_capabilities[capability] == frozenset({wiring.DOCUMENT_PROVIDER})
