"""Deployment/Managed Secret を Worker 内でだけ解決する fail-closed resolver を提供する。"""

from __future__ import annotations

import os
from pathlib import Path

from skillmind.core.secret_crypto import (
    SecretCipher,
    SecretCryptoError,
    managed_secret_aad,
)
from skillmind.integrations.domain import (
    IntegrationStatus,
    ResolvedSecretReference,
    SecretResolver,
)

_MAX_SECRET_BYTES = 65_536


class SecretResolutionError(RuntimeError):
    """Secret locator が無効、未設定、読み出し不能、または復号不能であることを表す。"""


class DeploymentSecretResolver:
    """Environment、`/run/secrets` file、または KEK 封入密文から Secret を都度解決する。"""

    def __init__(self, *, cipher: SecretCipher | None = None) -> None:
        """MANAGED 復号に使う KEK cipher を保持する。未設定なら MANAGED は解決不能となる。"""

        self._cipher = cipher

    def resolve(self, reference: ResolvedSecretReference) -> str:
        """Active reference から Secret 本文を取得し、空値/過大値を拒否する。"""

        if reference.status is not IntegrationStatus.ACTIVE:
            raise SecretResolutionError("SecretReference is disabled")
        if reference.resolver is SecretResolver.ENVIRONMENT:
            value = os.environ.get(reference.locator)
            if value is None:
                raise SecretResolutionError("Deployment Secret is not configured")
        elif reference.resolver is SecretResolver.FILE:
            path = Path(reference.locator)
            try:
                if path.stat().st_size > _MAX_SECRET_BYTES:
                    raise SecretResolutionError("Deployment Secret exceeds the size limit")
                value = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise SecretResolutionError("Deployment Secret could not be read") from error
            value = value.rstrip("\r\n")
        else:
            # 残る唯一の resolver は MANAGED。KEK で密文を復号する。
            value = self._resolve_managed(reference)
        if not value or len(value.encode("utf-8")) > _MAX_SECRET_BYTES:
            raise SecretResolutionError("Deployment Secret is empty or too large")
        return value

    def _resolve_managed(self, reference: ResolvedSecretReference) -> str:
        """KEK と AAD(project_id + secret_reference_id)で密文を復号する。全失敗は fail closed。"""

        if self._cipher is None:
            raise SecretResolutionError("Managed secret decryption is not configured")
        if reference.managed_material is None:
            raise SecretResolutionError("Managed secret material is missing")
        aad = managed_secret_aad(
            project_id=reference.project_id,
            secret_reference_id=reference.secret_reference_id,
        )
        try:
            return self._cipher.decrypt(reference.managed_material, aad=aad)
        except SecretCryptoError as error:
            # KEK 未設定・version 不明・改竄・AAD 不一致をすべて既存の
            # credential-unavailable 路径へ畳み、原因を反射しない。
            raise SecretResolutionError("Managed secret could not be decrypted") from error
