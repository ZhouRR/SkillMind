"""Envelope 暗号化の往復、AAD 束縛、KEK rotation と fail-closed 挙動を検証する。"""

from __future__ import annotations

import base64
import os
from uuid import uuid4

import pytest

from skillmind.core.secret_crypto import (
    EncryptedSecret,
    SecretCryptoError,
    load_secret_cipher,
    managed_secret_aad,
)


def _kek(version: str) -> str:
    """32 byte 乱数鍵を ``version:base64`` 形式で返す。"""

    return f"{version}:{base64.b64encode(os.urandom(32)).decode()}"


def test_round_trip_recovers_plaintext_with_matching_aad() -> None:
    """同じ AAD で封入した密文は元の明文へ復号できる。"""

    cipher = load_secret_cipher(_kek("v1"))
    assert cipher is not None
    aad = managed_secret_aad(project_id=uuid4(), secret_reference_id=uuid4())

    material = cipher.encrypt("redmine-token", aad=aad)

    assert material.kek_version == "v1"
    assert cipher.decrypt(material, aad=aad) == "redmine-token"


def test_ciphertext_moved_to_another_reference_fails_to_decrypt() -> None:
    """project/reference を跨いだ AAD では復号が fail closed で失敗する。"""

    cipher = load_secret_cipher(_kek("v1"))
    assert cipher is not None
    project_id = uuid4()
    material = cipher.encrypt(
        "token", aad=managed_secret_aad(project_id=project_id, secret_reference_id=uuid4())
    )

    other_reference = managed_secret_aad(
        project_id=project_id, secret_reference_id=uuid4()
    )
    other_project = managed_secret_aad(
        project_id=uuid4(), secret_reference_id=uuid4()
    )

    with pytest.raises(SecretCryptoError):
        cipher.decrypt(material, aad=other_reference)
    with pytest.raises(SecretCryptoError):
        cipher.decrypt(material, aad=other_project)


def test_tampered_ciphertext_is_rejected() -> None:
    """GCM tag により改竄された密文は復号できない。"""

    cipher = load_secret_cipher(_kek("v1"))
    assert cipher is not None
    aad = managed_secret_aad(project_id=uuid4(), secret_reference_id=uuid4())
    material = cipher.encrypt("token", aad=aad)

    tampered = EncryptedSecret(
        kek_version=material.kek_version,
        nonce=material.nonce,
        ciphertext=material.ciphertext[:-1] + bytes([material.ciphertext[-1] ^ 0x01]),
    )

    with pytest.raises(SecretCryptoError):
        cipher.decrypt(tampered, aad=aad)


def test_keyring_rotation_reads_legacy_and_writes_active() -> None:
    """新 active + 旧鍵の keyring は旧密文を復号でき、新規は active version で封入する。"""

    legacy_entry = _kek("v1")
    legacy = load_secret_cipher(legacy_entry)
    assert legacy is not None
    aad = managed_secret_aad(project_id=uuid4(), secret_reference_id=uuid4())
    old_material = legacy.encrypt("token", aad=aad)

    rotated = load_secret_cipher(f"{_kek('v2')},{legacy_entry}")
    assert rotated is not None and rotated.active_version == "v2"

    # 旧鍵で封入した密文をそのまま復号できる。
    assert rotated.decrypt(old_material, aad=aad) == "token"
    # 新規封入は active version(v2)で行う。
    assert rotated.encrypt("token", aad=aad).kek_version == "v2"


def test_decrypt_with_unknown_kek_version_fails_closed() -> None:
    """keyring から外れた KEK version の密文は復号を拒否する。"""

    cipher = load_secret_cipher(_kek("v2"))
    assert cipher is not None
    aad = managed_secret_aad(project_id=uuid4(), secret_reference_id=uuid4())
    orphan = EncryptedSecret(kek_version="v1", nonce=os.urandom(12), ciphertext=b"x" * 32)

    with pytest.raises(SecretCryptoError):
        cipher.decrypt(orphan, aad=aad)


def test_empty_and_oversized_plaintext_are_rejected() -> None:
    """空・上限超過の明文は封入時に拒否する。"""

    cipher = load_secret_cipher(_kek("v1"))
    assert cipher is not None
    aad = managed_secret_aad(project_id=uuid4(), secret_reference_id=uuid4())

    with pytest.raises(SecretCryptoError):
        cipher.encrypt("", aad=aad)
    with pytest.raises(SecretCryptoError):
        cipher.encrypt("x" * 70_000, aad=aad)


def test_missing_kek_yields_no_cipher() -> None:
    """未設定/空文字の KEK は None(MANAGED 無効)を返す。"""

    assert load_secret_cipher(None) is None
    assert load_secret_cipher("   ") is None


@pytest.mark.parametrize(
    "raw",
    [
        "notbase64!!!",
        "v1:@@@",
        "onlyversion",
        "v1:" + base64.b64encode(os.urandom(16)).decode(),  # 16 byte = AES-256 不適合
        ":" + base64.b64encode(os.urandom(32)).decode(),  # version 空
    ],
)
def test_broken_kek_configuration_fails_closed(raw: str) -> None:
    """壊れた KEK 設定は暗号化なし起動を許さず例外を送出する。"""

    with pytest.raises(SecretCryptoError):
        load_secret_cipher(raw)
