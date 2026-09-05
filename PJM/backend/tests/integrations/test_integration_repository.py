"""IntegrationRepository の永続化順序に関する不変条件を検証する。"""

from __future__ import annotations

import base64
import os
from typing import Any
from uuid import uuid4

import pytest

from projectmind.core.secret_crypto import SecretCryptoError, load_secret_cipher
from projectmind.db.models import ManagedSecretMaterial, SecretReference
from projectmind.integrations.domain import (
    MANAGED_SECRET_LOCATOR,
    CreateSecretReferenceCommand,
    SecretResolver,
)
from projectmind.integrations.repository import IntegrationRepository


class _EmptyResult:
    """名前重複検査を「既存なし」で満たす scalars 結果。"""

    def one_or_none(self) -> None:
        """既存 SecretReference が無いことを表す。"""

        return None


class _RecordingSession:
    """add/flush の呼び出し順序だけを記録する最小の AsyncSession 代役。"""

    def __init__(self) -> None:
        """記録用の呼び出し列を初期化する。"""

        self.calls: list[str] = []

    async def scalars(self, *_args: Any, **_kwargs: Any) -> _EmptyResult:
        """名前重複検査へ空結果を返す。"""

        return _EmptyResult()

    def add(self, instance: Any) -> None:
        """追加された ORM instance の型名を順序付きで記録する。"""

        self.calls.append(f"add:{type(instance).__name__}")

    async def flush(self) -> None:
        """親行確定のための flush を記録する。"""

        self.calls.append("flush")


@pytest.mark.asyncio
async def test_managed_material_is_added_only_after_parent_row_is_flushed() -> None:
    """MANAGED 作成では親 SecretReference を flush してから密文行を追加する。

    両 model は密文を read model へ lazy load させないため relationship を持たない。
    その結果 unit of work は mapper 間の INSERT 順序を保証せず、flush を挟まないと実 DB で
    子行が先に挿入され FK 違反(fk_managed_secret_material_reference)になる。実 DB を要する
    回帰テストは PostgreSQL の無い環境では skip されるため、順序自体をここで固定する。
    """

    cipher = load_secret_cipher("v1:" + base64.b64encode(os.urandom(32)).decode())
    assert cipher is not None
    session = _RecordingSession()
    repository = IntegrationRepository(session, secret_cipher=cipher)  # type: ignore[arg-type]

    await repository.create_secret_reference(
        CreateSecretReferenceCommand(
            project_id=uuid4(),
            name="Managed token",
            provider="redmine",
            resolver=SecretResolver.MANAGED,
            locator=MANAGED_SECRET_LOCATOR,
            key_version="v1",
            created_by=uuid4(),
            secret_value="redmine-api-key-1234567890",
        )
    )

    assert session.calls == [
        f"add:{SecretReference.__name__}",
        "flush",
        f"add:{ManagedSecretMaterial.__name__}",
    ]


@pytest.mark.asyncio
async def test_managed_creation_without_cipher_fails_closed_as_crypto_error() -> None:
    """KEK 未配線での MANAGED 作成は SecretCryptoError で fail closed する。

    request の不備ではなく配備側の設定不足のため、422 ではなく 503 へ写る例外型を使う
    (route の `_managed_secret_unavailable` がこれを受ける)。密文行も書かれない。
    """

    session = _RecordingSession()
    repository = IntegrationRepository(session)  # type: ignore[arg-type]

    with pytest.raises(SecretCryptoError, match="not configured"):
        await repository.create_secret_reference(
            CreateSecretReferenceCommand(
                project_id=uuid4(),
                name="Managed token",
                provider="redmine",
                resolver=SecretResolver.MANAGED,
                locator=MANAGED_SECRET_LOCATOR,
                key_version="v1",
                created_by=uuid4(),
                secret_value="redmine-api-key-1234567890",
            )
        )

    assert f"add:{ManagedSecretMaterial.__name__}" not in session.calls


@pytest.mark.asyncio
async def test_deployment_resolver_does_not_flush_or_store_material() -> None:
    """ENVIRONMENT/FILE は密文行を持たず、余分な flush も発行しない。"""

    session = _RecordingSession()
    repository = IntegrationRepository(session)  # type: ignore[arg-type]

    await repository.create_secret_reference(
        CreateSecretReferenceCommand(
            project_id=uuid4(),
            name="Deployment token",
            provider="redmine",
            resolver=SecretResolver.ENVIRONMENT,
            locator="REDMINE_API_KEY",
            key_version="v1",
            created_by=uuid4(),
        )
    )

    assert session.calls == [f"add:{SecretReference.__name__}"]
