"""実 factory と API/Worker 装配をつなぎ、設定の namespace が同じ consumer に届くか検証する。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, call
from uuid import UUID

import pytest
from fastapi import FastAPI
from pydantic_settings import SettingsConfigDict

from projectmind.agent.claude import ClaudeRuntimeConfiguration
from projectmind.api import main
from projectmind.core.settings import Settings
from projectmind.documents.service import DocumentService
from projectmind.documents.source import DatabaseProjectDocumentSource
from projectmind.skills.service import SkillService
from projectmind.storage import FileStorage
from projectmind.storage.factory import create_file_storage
from projectmind.storage.namespace import make_s3_namespace
from projectmind.worker import settings as worker

_NAMESPACE = UUID("11111111-2222-4333-8444-555555555555")


class OfflineSettings(Settings):
    """継承した本番 field/env prefix を検証しつつ、既存 dotenv の読取だけを無効にする。"""

    model_config = SettingsConfigDict(env_file=None)


def _settings(tmp_path: Path, namespace_id: UUID | None) -> Settings:
    """外部設定 file と実宛先を使わず、constructor に必要な値を明示する。"""

    return OfflineSettings(
        environment="test",
        contracts_dir=Path(__file__).resolve().parents[3] / "contracts",
        run_workspace_root=tmp_path / "runs",
        object_storage_endpoint="https://STORAGE.INVALID:443/",
        object_storage_bucket="synthetic-documents",
        object_storage_access_key="synthetic-access",
        object_storage_secret_key="synthetic-secret",
        object_storage_namespace_id=namespace_id,
        managed_secret_kek=None,
        redis_url="redis://127.0.0.1:6379/15",
    )


@pytest.mark.parametrize("namespace_id", [None, _NAMESPACE])
async def test_api_and_worker_inject_same_factory_namespace_into_real_consumers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, namespace_id: UUID | None,
) -> None:
    """DB/SDK/queue を完全に止めたまま、公開 upload と Worker source/Skill の実装配を通す。"""

    settings = _settings(tmp_path, namespace_id)
    sessions = MagicMock(side_effect=AssertionError("Composition must not query a database"))
    engine = MagicMock()
    engine.dispose = AsyncMock()
    redis = MagicMock()
    redis.aclose = AsyncMock()
    queue = MagicMock()
    queue.aclose = AsyncMock()
    sdk = Mock(spec=["put_object", "get_object", "remove_object", "stat_object"])
    sdk_constructor = Mock(return_value=sdk)
    monkeypatch.setattr("projectmind.storage.s3.Minio", sdk_constructor)
    monkeypatch.setattr(main, "create_redis_client", lambda _: redis)
    monkeypatch.setattr(main, "create_pool", AsyncMock(return_value=queue))
    monkeypatch.setattr(
        ClaudeRuntimeConfiguration, "from_environ",
        lambda: ClaudeRuntimeConfiguration(environment={}),
    )
    storages: list[FileStorage] = []

    def storage_factory(supplied: Settings) -> FileStorage:
        """両起動入口が同じ Settings を本物 factory に渡すことを記録する。"""

        assert supplied is settings
        storage = create_file_storage(supplied)
        storages.append(storage)
        return storage

    for module in (main, worker):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
        monkeypatch.setattr(module, "configure_logging", lambda _: None)
        monkeypatch.setattr(module, "create_database_engine", lambda _: engine)
        monkeypatch.setattr(module, "create_session_factory", lambda _: sessions)
        monkeypatch.setattr(module, "create_file_storage", storage_factory)
        monkeypatch.setattr(
            module, "build_skill_interpreter",
            Mock(return_value=(None, None, None, "synthetic-model")),
        )
    api_document = Mock(wraps=DocumentService)
    api_skill = Mock(wraps=SkillService)
    worker_document = Mock(wraps=DatabaseProjectDocumentSource)
    worker_skill = Mock(wraps=SkillService)
    monkeypatch.setattr(main, "DocumentService", api_document)
    monkeypatch.setattr(main, "SkillService", api_skill)
    monkeypatch.setattr(worker, "DatabaseProjectDocumentSource", worker_document)
    monkeypatch.setattr(worker, "SkillService", worker_skill)
    app = FastAPI()
    context: dict[str, Any] = {"redis": redis}
    async with main.lifespan(app):
        try:
            await worker.startup(context)
            assert len(storages) == 3
            expected = None if namespace_id is None else make_s3_namespace(
                namespace_id=namespace_id, endpoint="https://storage.invalid",
                bucket="synthetic-documents",
            )
            assert [storage.namespace for storage in storages] == [expected] * 3
            assert app.state.file_storage is storages[0]
            assert api_document.call_args.kwargs["file_storage"] is storages[0]
            assert api_skill.call_args.kwargs["file_storage"] is storages[0]
            assert worker_document.call_args.kwargs["file_storage"] is storages[1]
            assert worker_skill.call_args.kwargs["file_storage"] is storages[2]
            assert isinstance(app.state.document_service, DocumentService)
            assert isinstance(context["skill_service"], SkillService)
        finally:
            await worker.shutdown(context)
    assert sdk_constructor.call_args_list == [
        call(
            "storage.invalid", access_key="synthetic-access", secret_key="synthetic-secret",
            secure=True,
        )
    ] * 3
    assert not sdk.method_calls
    sessions.assert_not_called()
    queue.aclose.assert_awaited_once()
    redis.aclose.assert_awaited_once()
    assert engine.dispose.await_count == 2


def test_namespace_setting_is_unbound_by_default_and_environment_uuid_reaches_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既定値は None、明示 environment だけが UUID として factory へ届き、DB 値を読まない。"""

    assert Settings.model_fields["object_storage_namespace_id"].default is None
    monkeypatch.setenv("PROJECTMIND_OBJECT_STORAGE_NAMESPACE_ID", str(_NAMESPACE))
    sdk = Mock()
    monkeypatch.setattr("projectmind.storage.s3.Minio", Mock(return_value=sdk))
    settings = OfflineSettings(
        object_storage_endpoint="https://storage.invalid",
        object_storage_bucket="synthetic-documents", object_storage_access_key="synthetic-access",
        object_storage_secret_key="synthetic-secret",
    )
    assert settings.object_storage_namespace_id == _NAMESPACE
    storage = create_file_storage(settings)
    assert storage.namespace == make_s3_namespace(
        namespace_id=_NAMESPACE, endpoint="https://storage.invalid", bucket="synthetic-documents"
    )
    assert not sdk.method_calls
