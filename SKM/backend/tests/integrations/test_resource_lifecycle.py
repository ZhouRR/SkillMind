"""資源編集・削除の競合、参照保護と Secret の非開示を検証する。"""

from __future__ import annotations

import base64
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.core.secret_crypto import EncryptedSecret, load_secret_cipher, managed_secret_aad
from skillmind.db.models import Integration, ManagedSecretMaterial, SecretReference
from skillmind.integrations.domain import (
    MANAGED_SECRET_LOCATOR,
    IntegrationConflictError,
    IntegrationNotFoundError,
    IntegrationValidationError,
    SecretReferenceNotFoundError,
    UpdateSecretReferenceCommand,
)
from skillmind.integrations.repository import IntegrationRepository
from tests.integrations.test_readonly_resources import command


def integration_fixture() -> tuple[IntegrationRepository, MagicMock, Integration]:
    """外部接続しない MCP 行と transaction session double を返す。"""
    value = command("mcp")
    now = datetime.now(UTC)
    row = Integration(
        id=uuid4(),
        project_id=value.project_id,
        name=value.name,
        kind=value.kind,
        provider=value.provider,
        status="ACTIVE",
        revision=2,
        capabilities_json=list(value.capabilities),
        scope_json=value.scope,
        config_json=value.config,
        secret_reference_id=None,
        created_by=value.created_by,
        created_at=now,
        updated_at=now,
        disabled_at=None,
    )
    session = MagicMock(spec=AsyncSession)
    session.scalars = AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: row))
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    return IntegrationRepository(session), session, row


@pytest.mark.asyncio
@pytest.mark.parametrize("uris", [["resource://reports/new"], []])
async def test_edit_updates_connection_and_scope_under_original_revision(uris) -> None:
    """metadata と scope を更新して revision を進め、既存 snapshot は触らない。"""
    repo, session, row = integration_fixture()
    value = replace(
        command("mcp"),
        project_id=row.project_id,
        name="Updated reports",
        scope={"resource_uris": uris},
    )
    result = await repo.update_integration(value, integration_id=row.id, expected_revision=2)
    assert result.name == "Updated reports"
    assert result.revision == 3
    assert result.scope == value.scope
    assert row.config_json == value.config
    session.delete.assert_not_awaited()
    session.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["edit", "delete"])
async def test_stale_revision_never_mutates_resource(operation: str) -> None:
    """古い画面からの上書き・削除を同じ revision 検査で拒否する。"""
    repo, session, row = integration_fixture()
    with pytest.raises(IntegrationConflictError, match="stale"):
        if operation == "edit":
            await repo.update_integration(
                command("mcp"), integration_id=row.id, expected_revision=1
            )
        else:
            await repo.delete_integration(
                project_id=row.project_id, integration_id=row.id, expected_revision=1
            )
    assert row.revision == 2
    session.delete.assert_not_awaited()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_rejects_invalid_scope_without_touching_original() -> None:
    """作成と同じ Provider validator が編集での全許可追加も拒否する。"""
    repo, session, row = integration_fixture()
    original = dict(row.scope_json)
    with pytest.raises(IntegrationValidationError):
        await repo.update_integration(
            replace(command("mcp"), scope={"resource_uris": ["*"]}),
            integration_id=row.id,
            expected_revision=2,
        )
    assert row.scope_json == original
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_index", [0, 1, 2])
async def test_deletion_preserves_each_kind_of_existing_reference(reference_index: int) -> None:
    """binding・事前批准・変更提案のいずれが残っていても削除しない。"""
    repo, session, row = integration_fixture()
    session.scalar.side_effect = [None] * reference_index + [uuid4()]
    with pytest.raises(IntegrationConflictError, match="referenced"):
        await repo.delete_integration(
            project_id=row.project_id, integration_id=row.id, expected_revision=2
        )
    session.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_deletion_removes_only_unreferenced_integration() -> None:
    """全参照検査が空の場合だけ対象行を削除する。"""
    repo, session, row = integration_fixture()
    await repo.delete_integration(
        project_id=row.project_id, integration_id=row.id, expected_revision=2
    )
    session.delete.assert_awaited_once_with(row)
    session.flush.assert_awaited_once()


def secret_fixture() -> tuple[IntegrationRepository, MagicMock, SecretReference]:
    """暗号材料を必要としない metadata 編集用の合成認証情報を返す。"""
    now = datetime.now(UTC)
    row = SecretReference(
        id=uuid4(),
        project_id=uuid4(),
        name="Password",
        provider="postgres",
        resolver="MANAGED",
        locator=MANAGED_SECRET_LOCATOR,
        key_version="v1",
        status="ACTIVE",
        created_by=uuid4(),
        created_at=now,
        updated_at=now,
    )
    session = MagicMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=row)
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    session.execute = AsyncMock()
    return IntegrationRepository(session), session, row


def edit_command(row: SecretReference) -> UpdateSecretReferenceCommand:
    """画面で取得した更新日時を保つ編集 command を返す。"""
    return UpdateSecretReferenceCommand(
        project_id=row.project_id,
        secret_reference_id=row.id,
        expected_updated_at=row.updated_at,
        name="Renamed",
        key_version="v1",
    )


@pytest.mark.asyncio
async def test_empty_secret_edit_preserves_material_without_decrypting() -> None:
    """本文未指定時は KEK も不要で、公開応答は名前と metadata のみとなる。"""
    repo, session, row = secret_fixture()
    result = await repo.update_secret_reference(edit_command(row))
    assert result.name == "Renamed"
    assert not hasattr(result, "locator")
    assert not hasattr(result, "secret_value")
    session.scalars.assert_not_called()
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_secret_replacement_encrypts_exact_value_under_same_aad() -> None:
    """入力の空白も暗号化・復号で保ち、原値を response へ返さない。"""
    _, session, row = secret_fixture()
    cipher = load_secret_cipher("v1:" + base64.b64encode(os.urandom(32)).decode())
    assert cipher is not None
    material = ManagedSecretMaterial(
        id=uuid4(),
        secret_reference_id=row.id,
        project_id=row.project_id,
        kek_version="old",
        nonce=b"old",
        ciphertext=b"old",
    )
    session.scalars = AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: material))
    repo = IntegrationRepository(session, secret_cipher=cipher)
    plaintext = "  synthetic-password-密碼  "
    result = await repo.update_secret_reference(replace(edit_command(row), secret_value=plaintext))
    assert material.ciphertext != plaintext.encode()
    assert (
        cipher.decrypt(
            EncryptedSecret(
                kek_version=material.kek_version,
                nonce=material.nonce,
                ciphertext=material.ciphertext,
            ),
            aad=managed_secret_aad(project_id=row.project_id, secret_reference_id=row.id),
        )
        == plaintext
    )
    assert plaintext not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_project", [True, False])
async def test_secret_edit_rejects_cross_project_and_stale_view(wrong_project: bool) -> None:
    """別 Project は 404 型、古い更新日時は競合型となり書込しない。"""
    repo, session, row = secret_fixture()
    value = edit_command(row)
    if wrong_project:
        value = replace(value, project_id=uuid4())
    else:
        value = replace(value, expected_updated_at=row.updated_at - timedelta(seconds=1))
    with pytest.raises(SecretReferenceNotFoundError if wrong_project else IntegrationConflictError):
        await repo.update_secret_reference(value)
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_secret_deletion_keeps_material_when_used() -> None:
    """認証情報が接続に使われる間は密文も参照も削除しない。"""
    repo, session, row = secret_fixture()
    session.scalar.return_value = uuid4()
    with pytest.raises(IntegrationConflictError, match="used"):
        await repo.delete_secret_reference(
            project_id=row.project_id,
            secret_reference_id=row.id,
            expected_updated_at=row.updated_at,
        )
    session.execute.assert_not_awaited()
    session.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_integration_details_and_delete_share_not_found() -> None:
    """所有 Project で見つからない場合は詳細取得も削除も同じ不存在となる。"""
    repo, session, row = integration_fixture()
    session.scalars.return_value = SimpleNamespace(one_or_none=lambda: None)
    with pytest.raises(IntegrationNotFoundError):
        await repo.get_integration_details(project_id=uuid4(), integration_id=row.id)
    with pytest.raises(IntegrationNotFoundError):
        await repo.delete_integration(
            project_id=uuid4(), integration_id=row.id, expected_revision=2
        )
    session.delete.assert_not_awaited()
