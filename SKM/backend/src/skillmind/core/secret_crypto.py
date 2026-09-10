"""KEK による application 層 envelope 暗号化を提供する fail-closed モジュール。

MANAGED SecretReference の明文は AES-256-GCM で封入して保存し、KEK は環境変数
(base64 keyring) からのみ読み取る。AAD へ project_id と secret_reference_id を束縛
するため、暗号文を別 Project/別 reference へ移送しても復号は失敗する。KEK 未設定・
version 不一致・改竄・AAD 不一致はすべて fail closed とし、平文や空値へ降級しない。
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AES-256 の鍵長と GCM 標準 nonce 長。平文上限は secrets.py の _MAX_SECRET_BYTES と揃える。
_KEK_BYTES = 32
_NONCE_BYTES = 12
_MAX_PLAINTEXT_BYTES = 65_536
# KEK version ラベルの上限。keyring の区切り(":"/",")と衝突しない短い識別子へ限定する。
_KEK_VERSION_MAX = 32


class SecretCryptoError(RuntimeError):
    """KEK 未設定・不正、version 不明、または復号失敗を表す fail-closed 例外。"""


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    """AES-256-GCM の nonce と ciphertext(GCM tag 連結)、および封入した KEK version。"""

    kek_version: str
    nonce: bytes
    ciphertext: bytes


def managed_secret_aad(*, project_id: UUID, secret_reference_id: UUID) -> bytes:
    """暗号文を project と reference に束縛する Additional Authenticated Data を組み立てる。

    復号側は保存済み行の project_id/secret_reference_id から同じ AAD を再構成する。
    別 Project や別 reference へ暗号文を移送すると AAD が変わり復号は InvalidTag で失敗する。
    """

    return f"skillmind:managed-secret:v1:{project_id}:{secret_reference_id}".encode()


class SecretCipher:
    """KEK keyring で managed secret を封入/開封する application 層 cipher。

    keyring の先頭が暗号化にも使う active key、残りは復号専用の旧 key。行ごとの
    ``kek_version`` で復号鍵を選ぶため、rotation 中も旧鍵で読みつつ新鍵で書き戻せる。
    """

    def __init__(self, keyring: tuple[tuple[str, bytes], ...]) -> None:
        """``(version, 32byte key)`` の非空 keyring を検証して保持する。"""

        if not keyring:
            raise SecretCryptoError("KEK keyring is empty")
        ciphers: dict[str, AESGCM] = {}
        for version, key in keyring:
            if not version or len(version) > _KEK_VERSION_MAX:
                raise SecretCryptoError("KEK version label is invalid")
            if len(key) != _KEK_BYTES:
                raise SecretCryptoError("KEK must be 32 bytes for AES-256-GCM")
            if version in ciphers:
                raise SecretCryptoError("KEK version is duplicated")
            ciphers[version] = AESGCM(key)
        self._ciphers = ciphers
        # 先頭 entry を active key として固定する。新規封入は常にこの version で行う。
        self._active_version = keyring[0][0]

    @property
    def active_version(self) -> str:
        """新規封入に使う active KEK version を返す。"""

        return self._active_version

    def encrypt(self, plaintext: str, *, aad: bytes) -> EncryptedSecret:
        """明文を active KEK で封入する。空・過大な平文は拒否する。"""

        raw = plaintext.encode("utf-8")
        if not raw or len(raw) > _MAX_PLAINTEXT_BYTES:
            raise SecretCryptoError("Managed secret is empty or exceeds the size limit")
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._ciphers[self._active_version].encrypt(nonce, raw, aad)
        return EncryptedSecret(
            kek_version=self._active_version, nonce=nonce, ciphertext=ciphertext
        )

    def decrypt(self, material: EncryptedSecret, *, aad: bytes) -> str:
        """封入時の KEK version で復号する。version 不明・改竄・AAD 不一致は fail closed。"""

        cipher = self._ciphers.get(material.kek_version)
        if cipher is None:
            # 旧 KEK が keyring から外れている等。原因を反射せず鍵の入替を促す fail closed。
            raise SecretCryptoError("Managed secret KEK version is not configured")
        try:
            raw = cipher.decrypt(material.nonce, material.ciphertext, aad)
        except InvalidTag as error:
            # AAD 不一致・KEK 取り違え・改竄はすべてここへ集約し、平文や原因を反射しない。
            raise SecretCryptoError("Managed secret could not be decrypted") from error
        return raw.decode("utf-8")


def load_secret_cipher(raw: str | None) -> SecretCipher | None:
    """KEK 設定文字列から SecretCipher を組み立てる。未設定なら None を返す。

    形式は ``version:base64key`` を "," で連ねた keyring で、先頭が active key。空文字は
    「未設定」とみなし None(= MANAGED 無効)を返す。値が壊れている場合は fail closed で
    例外を送出し、暗号化なしの MANAGED 起動を許さない。
    """

    if raw is None or not raw.strip():
        return None
    entries: list[tuple[str, bytes]] = []
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        version, separator, encoded = item.partition(":")
        if not separator or not version.strip() or not encoded.strip():
            raise SecretCryptoError("KEK entry must be formatted as 'version:base64key'")
        try:
            key = base64.b64decode(encoded.strip(), validate=True)
        except (binascii.Error, ValueError) as error:
            raise SecretCryptoError("KEK base64 payload is invalid") from error
        entries.append((version.strip(), key))
    if not entries:
        raise SecretCryptoError("KEK keyring is empty")
    return SecretCipher(tuple(entries))
