"""保存先 identity の厳密性と構成・照合を、設定 source や外部 I/O なしで検証する。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from typing import cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from projectmind.core.settings import Settings
from projectmind.storage.blob import FileStorage, FileStorageError, StorageNamespace
from projectmind.storage.factory import create_file_storage
from projectmind.storage.memory import InMemoryFileStorage
from projectmind.storage.namespace import (
    canonical_s3_endpoint,
    make_s3_namespace,
    require_storage_namespace,
)

_ID = UUID("00000000-0000-4000-8000-000000000001")
_CHECKSUM = "sha256:" + "a" * 64


@pytest.mark.parametrize("namespace_id", [None, str(_ID), "invalid", 1, True, UUID(int=0)])
def test_namespace_rejects_missing_zero_or_coerced_identity(namespace_id: object) -> None:
    """UUID 文字列やゼロ UUID を、正しい保存先の代わりに暗黙採用しない。"""

    with pytest.raises(FileStorageError, match=r"^Storage namespace is invalid$"):
        StorageNamespace(cast(UUID, namespace_id), _CHECKSUM, True)


@pytest.mark.parametrize(
    "checksum", [None, 1, b"sha256", "", "a" * 64, "SHA256:" + "a" * 64,
                 "sha256:" + "A" * 64, "sha256:" + "a" * 63, "sha256:" + "a" * 65,
                 _CHECKSUM + "\n", " " + _CHECKSUM],
)
def test_namespace_rejects_noncanonical_descriptor_checksum(checksum: object) -> None:
    """不完全・別形式・制御字付き checksum を正規化して同一 identity にしない。"""

    with pytest.raises(FileStorageError, match=r"^Storage namespace is invalid$"):
        StorageNamespace(_ID, cast(str, checksum), True)


@pytest.mark.parametrize("durable", [None, 0, 1, "true", "false"])
def test_namespace_requires_actual_boolean_durability(durable: object) -> None:
    """DB 由来の数値や文字列を永続保存の印へ変換しない。"""

    with pytest.raises(FileStorageError, match=r"^Storage namespace is invalid$"):
        StorageNamespace(_ID, _CHECKSUM, cast(bool, durable))


@pytest.mark.parametrize("durable", [False, True])
def test_namespace_is_an_immutable_value(durable: bool) -> None:
    """構築後の書換えを許さず、同じ三つの事実だけを同一値として比較する。"""

    namespace = StorageNamespace(_ID, _CHECKSUM, durable)
    assert namespace == StorageNamespace(_ID, _CHECKSUM, durable)
    assert hash(namespace) == hash(StorageNamespace(_ID, _CHECKSUM, durable))
    for field, value in (
        ("namespace_id", uuid4()), ("descriptor_checksum", "sha256:" + "b" * 64),
        ("durable", not durable),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(namespace, field, value)


@pytest.mark.parametrize(
    "endpoint,canonical", [
        ("HTTP://Storage.Invalid:80/", "http://storage.invalid"),
        ("https://STORAGE.INVALID:443", "https://storage.invalid"),
        ("http://storage.invalid:00080", "http://storage.invalid"),
        ("http://storage.invalid:65535/", "http://storage.invalid:65535"),
        ("https://storage.invalid:9000", "https://storage.invalid:9000"),
        ("http://[2001:0DB8:0:0::1]:80/", "http://[2001:db8::1]"),
        ("https://[::1]:9443", "https://[::1]:9443"),
    ],
)
def test_canonical_endpoint_preserves_exact_supported_destination(
    endpoint: str, canonical: str,
) -> None:
    """大小文字・標準 port・IPv6 の表記差だけをまとめ、実宛先の差は残す。"""

    assert canonical_s3_endpoint(endpoint) == canonical
    assert canonical_s3_endpoint(canonical) == canonical


@pytest.mark.parametrize(
    "endpoint", [None, 1, b"http://storage.invalid", "", "storage.invalid", "//storage.invalid",
                 "ftp://storage.invalid", "http://", "http:///storage.invalid",
                 "http://storage.invalid/path", "http://storage.invalid//",
                 "http://storage.invalid?", "http://storage.invalid?key=value",
                 "http://storage.invalid#", "http://storage.invalid#fragment",
                 "http://user@storage.invalid", "http://user:password@storage.invalid",
                 "http://@storage.invalid", "http://storage.invalid%40outside.invalid",
                 "http://%73torage.invalid", "http://storage.invalid\\outside.invalid",
                 " http://storage.invalid", "http://storage.invalid ",
                 "http://storage.invalid\n", "http://stor\tage.invalid",
                 "http://stor\rage.invalid", "http://storage.invalid\x00",
                 "http://storage.invalid\x1f", "http://storage.invalid\x7f",
                 "http://ストレージ.invalid", "http://storage.invalid:",
                 "http://storage.invalid:0", "http://storage.invalid:-1",
                 "http://storage.invalid:65536", "http://storage.invalid:abc",
                 "http://storage.invalid:80:90", "http://[::1]:", "http://[::1]:65536",
                 "http://[::1]garbage", "http://[::1]garbage:9000", "http://[::1]x:80",
                 "http://[::1", "http://::1", "http://[::1%25eth0]",
                 "http://[v1.name]", "http://-storage.invalid", "http://storage..invalid"],
)
def test_ambiguous_or_unsupported_endpoint_is_a_static_refusal(endpoint: object) -> None:
    """urlsplit が黙って捨てる authority suffix と credential・制御字も拒否する。"""

    with pytest.raises(FileStorageError, match=r"^Object storage endpoint is invalid$"):
        canonical_s3_endpoint(cast(str, endpoint))


@pytest.mark.parametrize(
    "left,right", [
        ("http://[::ffff:192.0.2.1]", "http://[::ffff:c000:201]"),
        ("https://[2001:db8::192.0.2.1]:443/", "https://[2001:db8::c000:201]"),
    ],
)
def test_ipv4_embedded_ipv6_has_the_same_canonical_identity(left: str, right: str) -> None:
    """合法 IPv6 の埋込 IPv4 表記も、同じ address の十六進表記と同一にする。"""

    assert canonical_s3_endpoint(left) == canonical_s3_endpoint(right)


def test_s3_namespace_equivalence_only_normalizes_endpoint_spelling() -> None:
    """同じ実 endpoint/bucket/世代は一致し、credential は identity の入力にしない。"""

    expected = make_s3_namespace(namespace_id=_ID, endpoint="https://storage.invalid", bucket="one")
    assert expected.durable is True
    assert make_s3_namespace(
        namespace_id=_ID, endpoint="HTTPS://STORAGE.INVALID:443/", bucket="one"
    ) == expected
    for endpoint, bucket, namespace_id in (
        ("http://storage.invalid", "one", _ID),
        ("https://other.invalid", "one", _ID),
        ("https://storage.invalid:9443", "one", _ID),
        ("https://storage.invalid", "two", _ID),
        ("https://storage.invalid", "one", uuid4()),
    ):
        assert make_s3_namespace(
            namespace_id=namespace_id, endpoint=endpoint, bucket=bucket
        ) != expected


@pytest.mark.parametrize("namespace_id", [None, _ID])
def test_factory_passes_configured_namespace_without_reading_settings_sources(
    monkeypatch: pytest.MonkeyPatch, namespace_id: UUID | None,
) -> None:
    """明示構築した Settings の新 field をそのまま渡し、未指定は None のままにする。"""

    settings = Settings.model_construct(
        object_storage_endpoint="https://storage.invalid:9443", object_storage_bucket="test",
        object_storage_access_key="test-key", object_storage_secret_key="test-secret",
    )
    if namespace_id is not None:
        settings = settings.model_copy(update={"object_storage_namespace_id": namespace_id})
    assert settings.object_storage_namespace_id == namespace_id
    storage = Mock(spec=FileStorage)
    constructor = Mock(return_value=storage)
    monkeypatch.setattr("projectmind.storage.factory.S3FileStorage", constructor)
    assert create_file_storage(settings) is storage
    constructor.assert_called_once_with(
        endpoint="https://storage.invalid:9443", bucket="test", access_key="test-key",
        secret_key="test-secret", namespace_id=namespace_id,
    )
    assert not storage.method_calls


def test_credential_rotation_does_not_rebind_factory_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """factory から実 adapter まで通し、SDK を fake にして鍵の変更だけを分離する。"""

    client = Mock(spec=["put_object", "get_object", "stat_object", "remove_object"])
    constructor = Mock(return_value=client)
    monkeypatch.setattr("projectmind.storage.s3.Minio", constructor)
    original = Settings.model_construct(
        object_storage_endpoint="https://STORAGE.INVALID:443/", object_storage_bucket="test",
        object_storage_access_key="original", object_storage_secret_key="original",
        object_storage_namespace_id=_ID,
    )
    rotated = original.model_copy(update={
        "object_storage_endpoint": "https://storage.invalid",
        "object_storage_access_key": "rotated", "object_storage_secret_key": "rotated",
    })
    first, second = create_file_storage(original), create_file_storage(rotated)
    assert first.namespace == second.namespace == make_s3_namespace(
        namespace_id=_ID, endpoint="https://storage.invalid", bucket="test"
    )
    assert constructor.call_count == 2 and not client.method_calls


async def test_memory_namespace_belongs_only_to_the_original_instance() -> None:
    """空の新 instance や同内容の別辞書は、元 instance の回復先として扱わない。"""

    first, second = InMemoryFileStorage(), InMemoryFileStorage()
    first_namespace = first.namespace
    assert first_namespace.durable is second.namespace.durable is False
    assert first_namespace.namespace_id != second.namespace.namespace_id
    assert first_namespace.descriptor_checksum == second.namespace.descriptor_checksum
    await first.put("same/key", b"same", content_type="text/plain")
    assert await second.exists("same/key") is False
    await second.put("same/key", b"same", content_type="text/plain")
    assert first.namespace is first_namespace
    assert first.namespace != second.namespace
    with pytest.raises(FileStorageError, match=r"^Document storage namespace is unavailable$"):
        require_storage_namespace(second, first.namespace)


@pytest.mark.parametrize("difference", ["both_unbound", "unbound", "missing", "id", "checksum",
                                       "durability", "invalid_expected"])
def test_namespace_mismatch_is_rejected_without_any_storage_io(difference: str) -> None:
    """同じ key の存在確認にも進まず、未绑定・不一致・部分不正を静的 error に閉じる。"""

    namespace = StorageNamespace(_ID, _CHECKSUM, True)
    expected: StorageNamespace | None = namespace
    actual: StorageNamespace | None = expected
    if difference == "both_unbound":
        actual = expected = None
    elif difference == "unbound":
        expected = None
    elif difference == "missing":
        actual = None
    elif difference == "id":
        actual = replace(namespace, namespace_id=uuid4())
    elif difference == "checksum":
        actual = replace(namespace, descriptor_checksum="sha256:" + "b" * 64)
    elif difference == "durability":
        actual = replace(namespace, durable=False)
    else:
        expected = cast(StorageNamespace, {"namespace_id": str(_ID)})
    storage = Mock(spec=FileStorage)
    storage.namespace = actual
    with pytest.raises(FileStorageError, match=r"^Document storage namespace is unavailable$"):
        require_storage_namespace(storage, expected)
    assert not storage.method_calls


def test_namespace_match_is_a_local_comparison_not_remote_verification() -> None:
    """一致は I/O を追加せず、遠端の継続性や保存済み byte を証明したとも扱わない。"""

    expected = StorageNamespace(_ID, _CHECKSUM, True)
    storage = Mock(spec=FileStorage)
    storage.namespace = StorageNamespace(_ID, _CHECKSUM, True)
    assert require_storage_namespace(storage, expected) is expected
    assert not storage.method_calls
