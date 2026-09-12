"""実 ledger SQL/transaction と S3 client を接続する。PG lock/実 MinIO の証明ではない。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from skillmind.db.models import ProjectDocument, ProjectDocumentEffectUpload
from skillmind.documents.library import DocumentLibraryTarget
from skillmind.effects.document_provider import DocumentWriteProvider
from skillmind.effects.document_service import DocumentEffectService
from skillmind.effects.domain import EffectLeaseValidationError, EffectStepAuthority
from skillmind.effects.redmine import EffectProviderTransportError
from skillmind.effects.release import ExecutionFeatures
from skillmind.effects.wiring import create_effect_provider_registry
from skillmind.runs.repository import RunRepository
from skillmind.storage.s3_effect import S3ObjectWriteSource
from skillmind.worker.effects import ApprovedEffectExecutor
from tests.documents.test_document_effect_repository import SqlSession
from tests.documents.test_document_effect_repository import database as database
from tests.effects.database_fixtures import database_execution
from tests.storage.test_object_effect import Stream
from tests.worker.test_effect_executor import MemoryEffectService


class TransactionFactory:
    """実 SQLite transaction の前後で commit 応答喪失と再認可失敗を注入する。"""

    def __init__(self, db):
        """Org/actor 以外の認可内容は共有 authorization 回帰に任せる。"""
        self.db = db
        self.active = 0
        self.authorizations = 0
        self.revoke_at = None
        self.fail_state = None
        self.fail_after_commit = False

    @asynccontextmanager
    async def __call__(self):
        """段階ごとに新 Session を作り、provider の再開が ORM cache に依存しないようにする。"""
        with Session(self.db.engine, expire_on_commit=False) as session:
            yield TransactionSession(session, self)

    async def authorize(self, repository, execution, *, provider_version):
        """共有認可 port の呼出位置だけを検査し、実 lock/actor 判定とは区別する。"""
        assert self.active == 1
        assert repository._session.session.in_transaction()
        assert repository._document_library_target.namespace == self.db.command.namespace
        assert provider_version == "project-library-receipt/v1"
        assert execution.effect_execution_id == self.db.command.effect_id
        self.authorizations += 1
        if self.authorizations == self.revoke_at:
            raise EffectLeaseValidationError("Synthetic revocation")
        return EffectStepAuthority(self.db.organization, self.db.actor)


class TransactionSession(SqlSession):
    """既存 SQL adapter に service の session.begin/flush port を加える。"""

    def __init__(self, session, factory):
        """対象 DB と故障設定を保持する。"""
        super().__init__(session)
        self.factory = factory

    async def flush(self):
        """本物の flush を使い、公開行と ledger の rollback を検査する。"""
        self.session.flush()

    @asynccontextmanager
    async def begin(self):
        """commit 応答が失われても、呼出元には成功値を返さない。"""
        factory = self.factory
        factory.active += 1
        fail = False
        try:
            with self.session.begin():
                yield
                row = self.session.scalar(select(ProjectDocumentEffectUpload))
                if row is not None and row.state == factory.fail_state:
                    fail = True
                    factory.fail_state = None
                    if not factory.fail_after_commit:
                        raise OSError("Synthetic commit failure")
            if fail:
                raise OSError("Synthetic lost commit response")
        finally:
            factory.active -= 1


class ObjectServer:
    """署名済み HTTP request を記録する合成 storage。実サービスの条件原子性は模さない。"""

    def __init__(self, factory):
        """返却値/PUT 応答喪失を保持し、network 中の DB lock 持越しを検出する。"""
        self.factory = factory
        self.calls = []
        self.saved = None
        self.lose_put_response = False

    def handle(self, request):
        """固定 byte と原 metadata を返し、未保存時は明示 NoSuchKey を返す。"""
        assert self.factory.active == 0
        self.calls.append(request.method)
        if request.method == "PUT":
            assert request.headers["if-none-match"] == "*"
            assert "if-none-match" in request.headers["authorization"]
            assert self.saved is None
            self.saved = (
                request.content,
                {
                    key: value
                    for key, value in request.headers.items()
                    if key.startswith("x-amz-meta-") or key == "content-type"
                },
            )
            if self.lose_put_response:
                raise httpx.ReadError("Synthetic lost PUT response")
            return httpx.Response(
                200, headers={"ETag": '"original"', "x-amz-version-id": "v1"}, stream=Stream([])
            )
        assert request.method == "GET"
        if self.saved is None:
            return httpx.Response(404, stream=Stream([b"<Error><Code>NoSuchKey</Code></Error>"]))
        content, metadata = self.saved
        return httpx.Response(
            200,
            stream=Stream([content]),
            headers={
                **metadata,
                "ETag": '"original"',
                "x-amz-version-id": "v1",
            },
        )


def runtime(db, monkeypatch):
    """実 service/provider/SQL/client を組み立て、認可と HTTP server だけを合成する。"""
    factory = TransactionFactory(db)

    async def authorize(repository, execution, *, provider_version):
        """method descriptor の self を保ったまま transaction 認可を観測する。"""
        return await factory.authorize(repository, execution, provider_version=provider_version)

    monkeypatch.setattr(RunRepository, "authorize_effect_step", authorize)
    command = db.command
    target = DocumentLibraryTarget(command.namespace, command.bucket)
    execution = replace(
        database_execution(),
        effect_execution_id=command.effect_id,
        project_id=command.project_id,
        run_id=command.run_id,
        capability_version="document.write/v1",
        operation="CREATE",
        provider="project-library",
        integration_id=None,
        integration_config={},
        integration_scope=target.scope(command.project_id),
        secret_reference_id=None,
        target={"locator": "results/review/source.md", "display": "Review source"},
        changes=(
            {
                "path": "/document",
                "action": "SET",
                "value": {
                    "artifact_ref": command.artifact_ref,
                    "content_hash": command.content_checksum,
                    "size_bytes": len(command.content),
                    "mime_type": command.content_type,
                },
            },
        ),
        precondition={"revision": "absent"},
        verification={"method": "READ_BACK", "paths": ["/document"]},
    )
    service = DocumentEffectService(
        factory, features=ExecutionFeatures(document_writes=True), target=target, limits=db.limits
    )
    server = ObjectServer(factory)
    source = S3ObjectWriteSource(
        endpoint="https://storage.example.test",
        bucket=command.bucket,
        namespace_id=command.namespace.namespace_id,
        access_key="fixture-access",
        secret_key="fixture-secret",
        transport=httpx.MockTransport(server.handle),
    )
    return DocumentWriteProvider(service=service, source=source), execution, factory, server


def state(db):
    """新 Session で ledger と公開目録の実保存を確認する。"""
    with Session(db.engine) as session:
        row = session.scalar(select(ProjectDocumentEffectUpload))
        documents = session.scalars(select(ProjectDocument)).all()
        return row.state if row else None, [item.id for item in documents]


async def test_original_publication_replays_without_put_or_current_object_read(
    database, monkeypatch
):
    """一回 PUT→GET→公開を commit し、原回执があれば遠端消失後も原公開事実を返す。"""
    provider, execution, factory, server = runtime(database, monkeypatch)
    result = await provider.apply(execution, credential=None)
    assert server.calls == ["PUT", "GET"]
    assert result.replayed is False
    original_state = state(database)
    assert original_state[0] == "PUBLISHED" and len(original_state[1]) == 1
    assert result.before.metadata["observed_remote_absence"] is False
    assert result.after.content["document"]["mime_type"] == "text/markdown"
    saved = result.after.content["document"]
    assert saved["storage"]["bucket"] == database.command.bucket
    assert saved["storage"]["object_key"] == database.command.object_key
    assert saved["storage"]["object_key"] != saved["path"]
    library = DocumentLibraryTarget(database.command.namespace, database.command.bucket)
    assert saved["storage"]["document_library_id"] == library.reference(
        database.command.project_id,
    )["document_library_id"]
    assert set(saved["storage"]) == {
        "document_library_id", "bucket", "object_key", "version_id", "etag",
    }
    server.saved = None
    replay = await provider.apply(replace(execution, attempt_no=2), credential=None)
    assert replay.replayed is True
    assert replay.after == result.after
    assert state(database) == original_state
    assert server.calls == ["PUT", "GET"] and factory.active == 0


async def test_lost_put_response_recovers_by_get_and_publishes_original_document(
    database, monkeypatch
):
    """本文保存後の応答喪失は成功にせず、再 claim で GET だけから公開する。"""
    provider, execution, _, server = runtime(database, monkeypatch)
    server.lose_put_response = True
    with pytest.raises(EffectProviderTransportError, match="document_effect_uncertain"):
        await provider.apply(execution, credential=None)
    assert state(database) == ("SENT", [])
    result = await provider.apply(replace(execution, attempt_no=2), credential=None)
    assert result.replayed and server.calls == ["PUT", "GET"]
    assert state(database)[0] == "PUBLISHED"


@pytest.mark.parametrize("after_commit", [False, True])
async def test_start_commit_must_be_confirmed_before_put(database, monkeypatch, after_commit):
    """SENT commit 前失敗は rollback、commit 応答未知は GET のみ。どちらも失敗時 PUT しない。"""
    provider, execution, factory, server = runtime(database, monkeypatch)
    factory.fail_state, factory.fail_after_commit = "SENT", after_commit
    with pytest.raises(EffectProviderTransportError):
        await provider.apply(execution, credential=None)
    assert not server.calls
    assert state(database) == ("SENT" if after_commit else "RESERVED", [])
    if after_commit:
        for attempt in (2, 3):
            with pytest.raises(EffectProviderTransportError):
                await provider.apply(replace(execution, attempt_no=attempt), credential=None)
        assert server.calls == ["GET", "GET"]
        assert state(database) == ("SENT", [])
    else:
        result = await provider.apply(replace(execution, attempt_no=2), credential=None)
        assert not result.replayed and server.calls == ["PUT", "GET"]


@pytest.mark.parametrize("after_commit", [False, True])
async def test_publication_commit_loss_reuses_receipt_or_get_without_resend(
    database, monkeypatch, after_commit
):
    """公開応答未知で本文を再送せず、目録と ledger が常に一緒に commit/rollback する。"""
    provider, execution, factory, server = runtime(database, monkeypatch)
    factory.fail_state, factory.fail_after_commit = "PUBLISHED", after_commit
    with pytest.raises(EffectProviderTransportError):
        await provider.apply(execution, credential=None)
    assert state(database)[0] == ("PUBLISHED" if after_commit else "SENT")
    result = await provider.apply(replace(execution, attempt_no=2), credential=None)
    assert result.replayed and len(state(database)[1]) == 1
    assert server.calls == (["PUT", "GET"] if after_commit else ["PUT", "GET", "GET"])


async def test_revocation_after_stage_work_rolls_back_send_marker(database, monkeypatch):
    """開始段階の最終再認可が失敗した場合、SENT を commit せず一切 PUT しない。"""
    provider, execution, factory, server = runtime(database, monkeypatch)
    factory.revoke_at = 4
    with pytest.raises(EffectProviderTransportError, match="effect_authority_revoked"):
        await provider.apply(execution, credential=None)
    assert state(database) == ("RESERVED", []) and not server.calls


async def test_frozen_content_change_stops_before_send(database, monkeypatch):
    """準備後に claim の公開内容を変更しても元 Artifact から勝手に別要求を送らない。"""
    provider, execution, _, server = runtime(database, monkeypatch)
    prepared = await provider._service.prepare(execution)
    execution.changes[0]["value"]["size_bytes"] += 1
    with pytest.raises(ValueError):
        await provider._service.start_once(execution, prepared.command)
    assert not server.calls and state(database) == ("RESERVED", [])


@pytest.mark.parametrize("lost_response", [False, True])
async def test_approved_executor_uses_library_registry_without_integration_secret(
    database,
    monkeypatch,
    lost_response,
):
    """本番 executor→registry→service→SQL/S3 を通し、finalize port への結果分類を検証する。"""
    provider, execution, _, server = runtime(database, monkeypatch)
    server.lose_put_response = lost_response
    effects = MemoryEffectService(execution)
    resolver = MagicMock()
    registry = create_effect_provider_registry(
        features=ExecutionFeatures(document_writes=True),
        effect_service=effects,
        secret_resolver=resolver,
        git_client=MagicMock(),
        svn_client=MagicMock(),
        document_service=provider._service,
        document_source=provider._source,
    )
    executor = ApprovedEffectExecutor(
        effect_service=effects,
        provider_registry=registry,
        secret_resolver=resolver,
        worker_id="worker-test",
        lease_seconds=30,
        max_attempts=3,
    )
    status = await executor.execute(execution.effect_execution_id)
    assert status == ("FAILED" if lost_response else "APPLIED")
    result, failure = effects.finalized
    if lost_response:
        assert result is None and failure.retryable
        assert failure.code == "document_effect_uncertain"
        assert state(database) == ("SENT", [])
    else:
        assert failure is None and result.after.metadata["original_object_receipt"]
        assert state(database)[0] == "PUBLISHED"
    resolver.resolve.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
def test_factory_requires_explicit_document_ports_and_never_resolves_fake_secret(enabled):
    """内部開放だけでは Provider を不完全装配せず、既存 switch も文書 write を開かない。"""
    arguments = dict(
        features=ExecutionFeatures(document_writes=enabled),
        effect_service=MagicMock(),
        secret_resolver=MagicMock(),
        git_client=MagicMock(),
        svn_client=MagicMock(),
    )
    if enabled:
        with pytest.raises(ValueError, match="configured service"):
            create_effect_provider_registry(**arguments)
    else:
        assert not create_effect_provider_registry(**arguments).write_capabilities
    registry = create_effect_provider_registry(
        **arguments, document_service=AsyncMock(), document_source=AsyncMock()
    )
    assert registry.write_capabilities == (
        frozenset({"document.write/v1"}) if enabled else frozenset()
    )
    if enabled:
        assert not registry.resolve(
            capability_version="document.write/v1", provider="project-library"
        ).requires_secret
