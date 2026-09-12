"""成果 writer が既存 storage の namespace/接続/配額と一致することを無接続で検証する。"""

from __future__ import annotations

from unittest.mock import Mock
from uuid import uuid4

import pytest
from skillmind.core.settings import Settings
from skillmind.effects.release import configured_execution_features
from skillmind.storage import InMemoryFileStorage
from skillmind.storage.factory import create_document_write_source, create_file_storage


def settings(**overrides):
    """既存 dotenv や環境の宛先を使わず、非機密の合成 connection 設定を作る。"""
    values = {
        "object_storage_endpoint": "https://STORAGE.EXAMPLE.TEST:443/",
        "object_storage_bucket": "fixture-documents",
        "object_storage_namespace_id": uuid4(),
        "object_storage_access_key": "fixture-access",
        "object_storage_secret_key": "fixture-secret",
    }
    return Settings(_env_file=None, **{**values, **overrides})


def test_writer_uses_existing_canonical_storage_and_rotated_credentials(monkeypatch):
    """新規 URL/Integration を受け取らず、同じ世代で credentials の構成だけを更新する。"""
    sdk = Mock()
    monkeypatch.setattr("skillmind.storage.s3.Minio", Mock(return_value=sdk))
    original = settings()
    storage = create_file_storage(original)
    current = original.model_copy(update={"object_storage_secret_key": "fixture-rotated"})
    writer = create_document_write_source(current, storage=storage)
    assert writer.namespace == storage.namespace
    assert writer._endpoint == "https://storage.example.test"
    assert writer._credentials.access_key == current.object_storage_access_key
    assert writer._credentials.secret_key == "fixture-rotated"
    assert not sdk.mock_calls


@pytest.mark.parametrize("mutation", ["unbound", "memory", "namespace", "bucket", "endpoint"])
def test_unknown_or_different_library_refuses_writer_before_network(monkeypatch, mutation):
    """既存文書庫の所属と異なる writer を起動時に拒否し、namespace を後付けしない。"""
    sdk = Mock()
    monkeypatch.setattr("skillmind.storage.s3.Minio", Mock(return_value=sdk))
    original = settings()
    storage = create_file_storage(original)
    changes = {
        "unbound": {"object_storage_namespace_id": None},
        "memory": {},
        "namespace": {"object_storage_namespace_id": uuid4()},
        "bucket": {"object_storage_bucket": "other-documents"},
        "endpoint": {"object_storage_endpoint": "https://other.example.test"},
    }
    if mutation == "memory":
        storage = InMemoryFileStorage()
    with pytest.raises(ValueError):
        create_document_write_source(original.model_copy(update=changes[mutation]), storage=storage)
    assert not sdk.mock_calls


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("database", [False, True])
@pytest.mark.parametrize("document", [False, True])
def test_production_feature_conversion_keeps_three_write_limits_independent(
    monkeypatch, deferred, database, document
):
    """明示環境変数から CREATE だけを配備し、他 switch は権限の代用にしない。"""
    monkeypatch.setenv("SKILLMIND_DOCUMENT_WRITES_ENABLED", str(document).lower())
    features = configured_execution_features(
        settings(
            deferred_features_enabled=deferred,
            database_writes_enabled=database,
        )
    )
    assert features.deferred is deferred and features.database_writes is database
    assert features.document_writes is document
    assert features.effect_enabled("document.write/v1", "CREATE") is document
    assert not features.effect_enabled("document.write/v1", "UPDATE")
    assert not features.effect_enabled("document.write/v1", "DELETE")
    assert features.effect_enabled("database.write/v1", "INSERT") is database
    assert features.capability_enabled("subagent.dispatch/v1") is deferred


def test_document_write_default_remains_closed(monkeypatch):
    """既存配備の環境変数省略を成果書込の許可へ変えない。"""
    monkeypatch.delenv("SKILLMIND_DOCUMENT_WRITES_ENABLED", raising=False)
    assert not configured_execution_features(settings()).document_writes
