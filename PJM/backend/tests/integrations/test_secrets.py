"""DeploymentSecretResolver の resolver 分岐と MANAGED 復号の fail-closed 挙動を検証する。"""

from __future__ import annotations

import base64
import os
from uuid import UUID, uuid4

import pytest

from projectmind.core.secret_crypto import load_secret_cipher, managed_secret_aad
from projectmind.integrations.domain import (
    IntegrationStatus,
    ResolvedSecretReference,
    SecretResolver,
)
from projectmind.integrations.secrets import (
    DeploymentSecretResolver,
    SecretResolutionError,
)


def _kek() -> str:
    """32 byte 乱数鍵を keyring 形式で返す。"""

    return "v1:" + base64.b64encode(os.urandom(32)).decode()


def _managed_reference(
    *, project_id: UUID, reference_id: UUID, material: object, status: IntegrationStatus
) -> ResolvedSecretReference:
    """MANAGED 用の解決 reference を組み立てる。"""

    return ResolvedSecretReference(
        secret_reference_id=reference_id,
        project_id=project_id,
        provider="redmine",
        resolver=SecretResolver.MANAGED,
        locator="managed",
        key_version="2026-07",
        status=status,
        managed_material=material,  # type: ignore[arg-type]
    )


def test_managed_resolution_decrypts_with_matching_aad() -> None:
    """cipher と密文が揃い AAD が一致すれば明文を復元する。"""

    cipher = load_secret_cipher(_kek())
    assert cipher is not None
    project_id, reference_id = uuid4(), uuid4()
    material = cipher.encrypt(
        "redmine-token",
        aad=managed_secret_aad(project_id=project_id, secret_reference_id=reference_id),
    )
    resolver = DeploymentSecretResolver(cipher=cipher)

    reference = _managed_reference(
        project_id=project_id,
        reference_id=reference_id,
        material=material,
        status=IntegrationStatus.ACTIVE,
    )

    assert resolver.resolve(reference) == "redmine-token"


def test_managed_resolution_without_cipher_fails_closed() -> None:
    """KEK 未設定の Worker は MANAGED を解決できない。"""

    cipher = load_secret_cipher(_kek())
    assert cipher is not None
    project_id, reference_id = uuid4(), uuid4()
    material = cipher.encrypt(
        "token",
        aad=managed_secret_aad(project_id=project_id, secret_reference_id=reference_id),
    )
    resolver = DeploymentSecretResolver(cipher=None)

    with pytest.raises(SecretResolutionError):
        resolver.resolve(
            _managed_reference(
                project_id=project_id,
                reference_id=reference_id,
                material=material,
                status=IntegrationStatus.ACTIVE,
            )
        )


def test_managed_resolution_without_material_fails_closed() -> None:
    """密文行を伴わない MANAGED reference は解決できない。"""

    cipher = load_secret_cipher(_kek())
    assert cipher is not None
    resolver = DeploymentSecretResolver(cipher=cipher)

    with pytest.raises(SecretResolutionError):
        resolver.resolve(
            _managed_reference(
                project_id=uuid4(),
                reference_id=uuid4(),
                material=None,
                status=IntegrationStatus.ACTIVE,
            )
        )


def test_managed_material_from_another_reference_fails_closed() -> None:
    """別 reference 向けに封入した密文は AAD 不一致で復号できない。"""

    cipher = load_secret_cipher(_kek())
    assert cipher is not None
    project_id = uuid4()
    # reference A 向けに封入した密文を、reference B の解決に流用する。
    material = cipher.encrypt(
        "token",
        aad=managed_secret_aad(project_id=project_id, secret_reference_id=uuid4()),
    )
    resolver = DeploymentSecretResolver(cipher=cipher)

    with pytest.raises(SecretResolutionError):
        resolver.resolve(
            _managed_reference(
                project_id=project_id,
                reference_id=uuid4(),
                material=material,
                status=IntegrationStatus.ACTIVE,
            )
        )


def test_disabled_managed_reference_is_rejected() -> None:
    """DISABLED な SecretReference は復号前に拒否する。"""

    cipher = load_secret_cipher(_kek())
    assert cipher is not None
    project_id, reference_id = uuid4(), uuid4()
    material = cipher.encrypt(
        "token",
        aad=managed_secret_aad(project_id=project_id, secret_reference_id=reference_id),
    )
    resolver = DeploymentSecretResolver(cipher=cipher)

    with pytest.raises(SecretResolutionError):
        resolver.resolve(
            _managed_reference(
                project_id=project_id,
                reference_id=reference_id,
                material=material,
                status=IntegrationStatus.DISABLED,
            )
        )


def test_environment_resolution_still_reads_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MANAGED 追加後も ENVIRONMENT resolver は環境変数を読む(回帰)。"""

    monkeypatch.setenv("PROJECTMIND_TEST_SECRET", "env-token")
    resolver = DeploymentSecretResolver()

    reference = ResolvedSecretReference(
        secret_reference_id=uuid4(),
        project_id=uuid4(),
        provider="redmine",
        resolver=SecretResolver.ENVIRONMENT,
        locator="PROJECTMIND_TEST_SECRET",
        key_version="2026-07",
        status=IntegrationStatus.ACTIVE,
    )

    assert resolver.resolve(reference) == "env-token"
