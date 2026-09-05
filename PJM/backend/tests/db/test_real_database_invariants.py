"""実 PostgreSQL 上で migration 済み schema の不変条件を検証する統合テスト。

P8 収尾の「本機で先行できる真実 DB 検証」を担う:
0012/0013 の実機制約、interpret/adjust の並行 idempotency、
Project isolation join、および seed migration の歴史 snapshot 回帰。
既定 test database を汚さないよう、専用 scratch database を作成して
`alembic upgrade head` を subprocess で全鎖実行し、終了時に破棄する。
"""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from projectmind.agent.run_binding import RunBindingError, load_bound_run_resource
from projectmind.agent.subagent_sessions import (
    PostgresSubagentSessionRecorder,
    SubagentSessionDraft,
    SubagentSessionRecordingError,
)
from projectmind.compositions.domain import (
    CreateModuleCommand,
    ModuleNotFoundError,
    ModuleSkillInvalidError,
    UpdateModuleCommand,
)
from projectmind.compositions.repository import CompositionRepository
from projectmind.core.secret_crypto import load_secret_cipher
from projectmind.db.models import (
    AgentSession,
    Integration,
    InteractionResponse,
    ManagedSecretMaterial,
    Organization,
    Project,
    ProjectMember,
    ProjectSkillVersion,
    ResourceBinding,
    Run,
    RunAttempt,
    RunSegment,
    RuntimeManifest,
    Skill,
    SkillComposition,
    SkillCompositionItem,
    SkillInterpretation,
    SkillSource,
    SkillVersion,
    TaskSchedule,
    User,
    UserInteraction,
)
from projectmind.integrations.domain import (
    MANAGED_SECRET_LOCATOR,
    CreateSecretReferenceCommand,
    ResourceBindingLevel,
    SecretResolver,
    binding_checksum,
)
from projectmind.integrations.repository import IntegrationRepository
from projectmind.integrations.secrets import DeploymentSecretResolver
from projectmind.schedules.domain import ScheduleNotFoundError
from projectmind.schedules.repository import ScheduleRepository
from projectmind.skills.domain import (
    CreateSkillVersionDraftCommand,
    SaveModelInterpretationCommand,
    SkillInterpretationNotFoundError,
    SkillInterpretationStatus,
    SkillSourceNotFoundError,
    SkillVersionNotFoundError,
)
from projectmind.skills.repository import SkillRepository
from projectmind.skills.service import save_execution_idempotently

BACKEND_DIR = Path(__file__).resolve().parents[2]
SCRATCH_DATABASE = f"projectmind_dbverify_{os.getpid()}"
LEGACY_JAF_SKILL_VERSION_ID = UUID("00000000-0000-4000-8000-000000000203")
LEGACY_JAF_RUNTIME_MANIFEST_ID = UUID("00000000-0000-4000-8000-000000000204")
LEGACY_JAF_MANIFEST_CHECKSUM = (
    "sha256:92c7726b88f205059de7f62bebf2ff1560d69bdfb28112fe02856569fc3e6ddc"
)


def _replace_database(url: str, database: str) -> str:
    """接続 URL の database 名だけを差し替える。"""

    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{database}"))


async def _run_admin_sql(statement: str) -> None:
    """AUTOCOMMIT の管理接続で CREATE/DROP DATABASE を実行する。"""

    admin_url = _replace_database(os.environ["PROJECTMIND_DATABASE_URL"], "postgres")
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql(statement)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def migrated_database_url() -> Iterator[str]:
    """全 migration 鎖を実機適用した使い捨て database の URL を提供する。"""

    scratch_url = _replace_database(os.environ["PROJECTMIND_DATABASE_URL"], SCRATCH_DATABASE)
    try:
        asyncio.run(_run_admin_sql(f'CREATE DATABASE "{SCRATCH_DATABASE}"'))
    except OSError as error:
        # PostgreSQL が起動していない環境では setup ERROR ではなく理由付き skip として
        # 集計へ現れるようにする。接続到達後の失敗(認証・権限・migration)は実障害の
        # 可能性があるため、従来どおり fail させる。
        pytest.skip(f"real PostgreSQL is unreachable: {error}")
    try:
        # settings の env_file/lru_cache を汚さないため、subprocess へ URL を注入する。
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND_DIR,
            env={**os.environ, "PROJECTMIND_DATABASE_URL": scratch_url},
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, f"alembic upgrade failed:\n{completed.stderr[-2000:]}"
        yield scratch_url
    finally:
        asyncio.run(_run_admin_sql(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)'))


def _alembic_upgrade(url: str, revision: str) -> None:
    """指定 revision まで migration を適用する。段階適用の検証で使う。"""

    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=BACKEND_DIR,
        env={**os.environ, "PROJECTMIND_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"alembic upgrade to {revision} failed:\n{completed.stderr[-2000:]}"
    )


async def _insert_in_order(session: AsyncSession, *rows: object) -> None:
    """引数の順で一行ずつ flush する。

    `run_segments.parent_agent_session_id` と `agent_sessions.run_segment_id` は互いを指すため、
    Run 骨格の table 群は FK 上**循環**する。循環があると SQLAlchemy は table を位相整列できず、
    `add_all` 一括の INSERT 順は不定になる。実 PostgreSQL は FK を即時検査するので、順が
    ずれた瞬間に FK 違反で落ちる (SQLite や in-memory 検証では露見しない)。
    本番経路は claim 済み Attempt を参照するため一括 INSERT は起きず、これは fixture 側の都合。
    """

    for row in rows:
        session.add(row)
        await session.flush()


@asynccontextmanager
async def _session_factory(
    url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """テストの event loop に束ねた engine と session factory を提供する。"""

    engine = create_async_engine(url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _organization() -> Organization:
    """Skill library 実機検証用の Organization を生成する。"""

    now = datetime.now(UTC)
    return Organization(
        id=uuid4(),
        name=f"DB Verify Org {uuid4().hex[:8]}",
        status="ACTIVE",
        settings_json={},
        default_retention_days=90,
        created_at=now,
        updated_at=now,
    )


def _skill_source(organization_id: UUID) -> SkillSource:
    """任意 Organization に属する不変 source snapshot を生成する。"""

    return SkillSource(
        id=uuid4(),
        organization_id=organization_id,
        name="DB Verify Skill",
        source_type="directory",
        storage_uri="database://skill-sources/db-verify",
        content_hash=f"sha256:{uuid4().hex * 2}"[:71],
        source_version=None,
        imported_by=uuid4(),
        source_snapshot_json=[{"path": "SKILL.md", "content": "# DB Verify\n"}],
        created_at=datetime.now(UTC),
    )


def _interpretation(
    source: SkillSource,
    *,
    checksum: str,
    parent_id: UUID | None = None,
) -> SkillInterpretation:
    """uq/FK 制約検証用の最小 interpretation row を生成する。"""

    return SkillInterpretation(
        id=uuid4(),
        skill_source_id=source.id,
        origin="model",
        interpreter_version="db-verify/1.0.0",
        model="fixture",
        compatibility_level="assisted",
        status="PREVIEW_READY",
        summary="db verify",
        confidence=0.5,
        assumptions_json=[],
        questions_json=[],
        diagnostics_json=[],
        normalized_package_json={"package_format": "projectmind.normalized/v1"},
        manifest_draft_json={},
        report_json=None,
        execution_json=None,
        parent_interpretation_id=parent_id,
        adjustment_json=None,
        checksum=checksum,
        created_at=datetime.now(UTC),
    )


def _command(source: SkillSource, execution_key: str) -> SaveModelInterpretationCommand:
    """service 経由の idempotent 保存に使う command を生成する。"""

    return SaveModelInterpretationCommand(
        organization_id=source.organization_id,
        skill_source_id=source.id,
        execution_key=execution_key,
        interpreter_version="db-verify/1.0.0",
        model="fixture",
        status=SkillInterpretationStatus.PREVIEW_READY,
        compatibility_level="assisted",
        confidence=0.5,
        summary="db verify concurrent",
        assumptions=(),
        questions=(),
        diagnostics=(),
        normalized_package={"package_format": "projectmind.normalized/v1"},
        manifest_draft={},
        report=None,
        execution={"model": "fixture", "parameters": {}},
    )


def _run_spine(now: datetime) -> tuple[Run, RunSegment, RunAttempt, AgentSession]:
    """Run / Segment / Attempt / PRIMARY Session の最小骨格を作る。"""

    run = Run(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        trigger_type="immediate",
        idempotency_key=f"db-verify-{uuid4().hex}",
        request_hash=uuid4().hex * 2,
        status="RUNNING",
        input_json={},
        task_snapshot_json={},
        permission_snapshot_json={},
        selected_sources_json={},
        limits_snapshot_json={},
        row_version=1,
        started_at=now,
        finished_at=None,
        error_json=None,
        created_at=now,
        updated_at=now,
    )
    segment = RunSegment(
        id=uuid4(),
        run_id=run.id,
        segment_no=1,
        trigger_type="INITIAL",
        trigger_ref=None,
        status="RUNNING",
        objective_json={"text": "Analyze"},
        checkpoint_json={},
        continuation_mode="INITIAL",
        parent_agent_session_id=None,
        instruction_snapshot_id=None,
        started_at=now,
        finished_at=None,
        created_at=now,
        updated_at=now,
    )
    attempt = RunAttempt(
        id=uuid4(),
        run_id=run.id,
        run_segment_id=segment.id,
        attempt_no=1,
        reason="INITIAL",
        status="RUNNING",
        worker_id="db-verify",
        lease_token_hash=None,
        lease_expires_at=None,
        heartbeat_at=now,
        started_at=now,
        finished_at=None,
        error_json=None,
        created_at=now,
        updated_at=now,
    )
    primary = AgentSession(
        id=uuid4(),
        run_id=run.id,
        run_attempt_id=attempt.id,
        run_segment_id=segment.id,
        sdk_session_id=uuid4(),
        parent_session_id=None,
        continuation_mode="INITIAL",
        checkpoint_checksum=None,
        engine_options_checksum=f"sha256:{'1' * 64}",
        engine="db-verify",
        session_kind="PRIMARY",
        cwd="/workspace/db-verify",
        sdk_version="1.0.0",
        cli_version="1.0.0",
        model="fixture",
        status="ACTIVE",
        usage_json={},
        cost_json={},
        created_at=now,
        updated_at=now,
    )
    return run, segment, attempt, primary


def _subagent_session(
    run: Run,
    segment: RunSegment,
    attempt: RunAttempt,
    primary: AgentSession,
    now: datetime,
    *,
    key: str,
    status: str = "CLOSED",
) -> AgentSession:
    """扇出の一路にあたる子 Session を作る。"""

    return AgentSession(
        id=uuid4(),
        run_id=run.id,
        run_attempt_id=attempt.id,
        run_segment_id=segment.id,
        sdk_session_id=uuid4(),
        parent_session_id=primary.id,
        continuation_mode="BRANCH",
        checkpoint_checksum=None,
        engine_options_checksum=primary.engine_options_checksum,
        engine=primary.engine,
        session_kind="SUBAGENT",
        cwd=primary.cwd,
        sdk_version=primary.sdk_version,
        cli_version=primary.cli_version,
        model=primary.model,
        status=status,
        usage_json={"branch_key": key},
        cost_json={},
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_0012_unique_execution_identity_is_enforced(migrated_database_url: str) -> None:
    """(source, interpreter_version, checksum) の重複 INSERT を実機 uq 制約が拒否する。"""

    async with _session_factory(migrated_database_url) as factory:
        organization = _organization()
        source = _skill_source(organization.id)
        checksum = f"sha256:{'a' * 64}"
        async with factory() as session, session.begin():
            session.add_all([organization, source])
            session.add(_interpretation(source, checksum=checksum))

        with pytest.raises(IntegrityError) as caught:
            async with factory() as session, session.begin():
                session.add(_interpretation(source, checksum=checksum))
        assert "uq_skill_interpretations_source_version_checksum" in str(caught.value)


@pytest.mark.asyncio
async def test_0013_parent_lineage_foreign_key_is_enforced(migrated_database_url: str) -> None:
    """存在しない親 interpretation への lineage を実機 FK が拒否する。"""

    async with _session_factory(migrated_database_url) as factory:
        organization = _organization()
        source = _skill_source(organization.id)
        async with factory() as session, session.begin():
            session.add_all([organization, source])

        with pytest.raises(IntegrityError) as caught:
            async with factory() as session, session.begin():
                session.add(
                    _interpretation(source, checksum=f"sha256:{'b' * 64}", parent_id=uuid4())
                )
        assert "parent_interpretation_id" in str(caught.value)


@pytest.mark.asyncio
async def test_0020_interactive_run_constraints_are_enforced(
    migrated_database_url: str,
) -> None:
    """Segment/Attempt/Session/Interaction の順次実行制約を実 PostgreSQL で確認する。"""

    now = datetime.now(UTC)
    run = Run(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        trigger_type="immediate",
        idempotency_key=f"db-verify-{uuid4().hex}",
        request_hash=uuid4().hex * 2,
        status="RUNNING",
        input_json={},
        task_snapshot_json={},
        permission_snapshot_json={},
        selected_sources_json={},
        limits_snapshot_json={},
        row_version=4,
        started_at=now,
        finished_at=None,
        error_json=None,
        created_at=now,
        updated_at=now,
    )
    segment_a = RunSegment(
        id=uuid4(),
        run_id=run.id,
        segment_no=1,
        trigger_type="INITIAL",
        trigger_ref=None,
        status="COMPLETED",
        objective_json={"text": "Analyze"},
        checkpoint_json={},
        continuation_mode="INITIAL",
        parent_agent_session_id=None,
        instruction_snapshot_id=None,
        started_at=now,
        finished_at=now,
        created_at=now,
        updated_at=now,
    )
    attempt_a = RunAttempt(
        id=uuid4(),
        run_id=run.id,
        run_segment_id=segment_a.id,
        attempt_no=1,
        reason="INITIAL",
        status="DEFERRED",
        worker_id="db-verify",
        lease_token_hash=None,
        lease_expires_at=None,
        heartbeat_at=now,
        started_at=now,
        finished_at=now,
        error_json=None,
        created_at=now,
        updated_at=now,
    )
    session_a = AgentSession(
        id=uuid4(),
        run_id=run.id,
        run_attempt_id=attempt_a.id,
        run_segment_id=segment_a.id,
        sdk_session_id=uuid4(),
        parent_session_id=None,
        continuation_mode="INITIAL",
        checkpoint_checksum=None,
        engine_options_checksum=f"sha256:{'1' * 64}",
        engine="db-verify",
        session_kind="PRIMARY",
        cwd="/workspace/db-verify",
        sdk_version="1.0.0",
        cli_version="1.0.0",
        model="fixture",
        status="IDLE",
        usage_json={},
        cost_json={},
        created_at=now,
        updated_at=now,
    )
    interaction = UserInteraction(
        id=uuid4(),
        run_id=run.id,
        run_segment_id=segment_a.id,
        agent_session_id=session_a.id,
        interaction_type="REVIEW",
        prompt_json={"prompt": "Review", "allow_multiple": False},
        options_json=[],
        required=True,
        expires_at=now + timedelta(hours=1),
        status="RESPONDED",
        version=2,
        continuation_mode="FORK",
        checkpoint_json={"summary": "Ready"},
        checkpoint_checksum=f"sha256:{'2' * 64}",
        created_at=now,
        updated_at=now,
    )
    response = InteractionResponse(
        id=uuid4(),
        interaction_id=interaction.id,
        run_id=run.id,
        actor_id=uuid4(),
        interaction_version=1,
        idempotency_key="response-1",
        request_hash="3" * 64,
        response_json={"text": "Continue"},
        created_at=now,
    )

    async with _session_factory(migrated_database_url) as factory:
        async with factory() as session, session.begin():
            await _insert_in_order(
                session, run, segment_a, attempt_a, session_a, interaction, response
            )

        segment_b = RunSegment(
            id=uuid4(),
            run_id=run.id,
            segment_no=2,
            trigger_type="INTERACTION_RESPONSE",
            trigger_ref=response.id,
            status="CREATED",
            objective_json={"text": "Continue"},
            checkpoint_json={"user_responses": [{"text": "Continue"}]},
            continuation_mode="FORK",
            parent_agent_session_id=session_a.id,
            instruction_snapshot_id=None,
            started_at=None,
            finished_at=None,
            created_at=now,
            updated_at=now,
        )
        attempt_b = RunAttempt(
            id=uuid4(),
            run_id=run.id,
            run_segment_id=segment_b.id,
            attempt_no=1,
            reason="CONTINUATION",
            status="RUNNING",
            worker_id=None,
            lease_token_hash=None,
            lease_expires_at=None,
            heartbeat_at=None,
            started_at=None,
            finished_at=None,
            error_json=None,
            created_at=now,
            updated_at=now,
        )
        session_b = AgentSession(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=attempt_b.id,
            run_segment_id=segment_b.id,
            sdk_session_id=uuid4(),
            parent_session_id=session_a.id,
            continuation_mode="FORK",
            checkpoint_checksum=f"sha256:{'2' * 64}",
            engine_options_checksum=f"sha256:{'4' * 64}",
            engine="db-verify",
            session_kind="PRIMARY",
            cwd="/workspace/db-verify-b",
            sdk_version="1.0.0",
            cli_version="1.0.0",
            model="fixture",
            status="ACTIVE",
            usage_json={},
            cost_json={},
            created_at=now,
            updated_at=now,
        )
        async with factory() as session, session.begin():
            await _insert_in_order(session, segment_b, attempt_b, session_b)

        # Attempt 番号は Run 全体ではなく Segment ごとに 1 から始められる。
        async with factory() as session:
            attempt_count = await session.scalar(
                select(func.count(RunAttempt.id)).where(
                    RunAttempt.run_id == run.id,
                    RunAttempt.attempt_no == 1,
                )
            )
        assert attempt_count == 2

        duplicate_segment = RunSegment(
            id=uuid4(),
            run_id=run.id,
            segment_no=2,
            trigger_type="INTERACTION_RESPONSE",
            trigger_ref=uuid4(),
            status="CREATED",
            objective_json={},
            checkpoint_json={},
            continuation_mode="REPLACE",
            parent_agent_session_id=session_a.id,
            instruction_snapshot_id=None,
            started_at=None,
            finished_at=None,
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(IntegrityError) as segment_error:
            async with factory() as session, session.begin():
                session.add(duplicate_segment)
        assert "uq_run_segments_run_no" in str(segment_error.value)

        retry_attempt = RunAttempt(
            id=uuid4(),
            run_id=run.id,
            run_segment_id=segment_b.id,
            attempt_no=2,
            reason="RETRY",
            status="RUNNING",
            worker_id="db-verify-retry",
            lease_token_hash="6" * 64,
            lease_expires_at=now + timedelta(minutes=1),
            heartbeat_at=now,
            started_at=now,
            finished_at=None,
            error_json=None,
            created_at=now,
            updated_at=now,
        )
        async with factory() as session, session.begin():
            session.add(retry_attempt)

        duplicate_active_session = AgentSession(
            id=uuid4(),
            run_id=run.id,
            run_attempt_id=retry_attempt.id,
            run_segment_id=segment_b.id,
            sdk_session_id=uuid4(),
            parent_session_id=session_a.id,
            continuation_mode="FORK",
            checkpoint_checksum=f"sha256:{'2' * 64}",
            engine_options_checksum=f"sha256:{'4' * 64}",
            engine="db-verify",
            session_kind="PRIMARY",
            cwd="/workspace/db-verify-b",
            sdk_version="1.0.0",
            cli_version="1.0.0",
            model="fixture",
            status="ACTIVE",
            usage_json={},
            cost_json={},
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(IntegrityError) as session_error:
            async with factory() as session, session.begin():
                session.add(duplicate_active_session)
        assert "uq_agent_sessions_active_run" in str(session_error.value)

        duplicate_response = InteractionResponse(
            id=uuid4(),
            interaction_id=interaction.id,
            run_id=run.id,
            actor_id=uuid4(),
            interaction_version=1,
            idempotency_key="response-2",
            request_hash="5" * 64,
            response_json={"text": "Different"},
            created_at=now,
        )
        with pytest.raises(IntegrityError) as response_error:
            async with factory() as session, session.begin():
                session.add(duplicate_response)
        assert "uq_interaction_responses_interaction" in str(response_error.value)


@pytest.mark.asyncio
async def test_0027_subagent_sessions_coexist_under_one_attempt(
    migrated_database_url: str,
) -> None:
    """扇出の子 Session が主 Session と同居できることを実 PostgreSQL で確認する (計画 §23 P3b)。

    0027 が守るのは二つの一意条件を**外す**ことではなく **PRIMARY 限定へ掛け直す**こと。
    条件を外してしまうと「Attempt に本体 session が二つ」も通り、守っていた不変条件が消える。
    索引の述語は SQLite や metadata 検査では効かないため、ここでしか検証できない。
    """

    now = datetime.now(UTC)
    run, segment, attempt, primary = _run_spine(now)

    async with _session_factory(migrated_database_url) as factory:
        async with factory() as session, session.begin():
            await _insert_in_order(session, run, segment, attempt, primary)

        # 同じ Attempt・同じ Run の下に、ACTIVE な子が複数本並ぶ——これが扇出の形。
        branches = [
            _subagent_session(run, segment, attempt, primary, now, key=key, status="ACTIVE")
            for key in ("dependencies", "migrations", "tests")
        ]
        async with factory() as session, session.begin():
            session.add_all(branches)

        async with factory() as session:
            stored = await session.scalar(
                select(func.count(AgentSession.id)).where(
                    AgentSession.run_attempt_id == attempt.id,
                    AgentSession.session_kind == "SUBAGENT",
                )
            )
        assert stored == 3

        # PRIMARY 側の不変条件は残っている。
        second_primary = _subagent_session(
            run, segment, attempt, primary, now, key="x", status="ACTIVE"
        )
        second_primary.session_kind = "PRIMARY"
        second_primary.continuation_mode = "INITIAL"
        second_primary.parent_session_id = None
        with pytest.raises(IntegrityError) as duplicate_primary:
            async with factory() as session, session.begin():
                session.add(second_primary)
        assert "uq_agent_sessions_primary_run_attempt" in str(duplicate_primary.value)


@pytest.mark.asyncio
async def test_0027_branch_mode_and_subagent_kind_cannot_disagree(
    migrated_database_url: str,
) -> None:
    """BRANCH と SUBAGENT の対応が DB 側で固定されていることを確認する。

    片方だけを名乗れると、監査で「なぜ同じ Attempt に session が複数あるのか」が読めなくなる。
    """

    now = datetime.now(UTC)
    run, segment, attempt, primary = _run_spine(now)

    async with _session_factory(migrated_database_url) as factory:
        async with factory() as session, session.begin():
            await _insert_in_order(session, run, segment, attempt, primary)

        # PRIMARY なのに BRANCH を名乗る。
        mislabelled = _subagent_session(run, segment, attempt, primary, now, key="a")
        mislabelled.session_kind = "PRIMARY"
        with pytest.raises(IntegrityError) as kind_error:
            async with factory() as session, session.begin():
                session.add(mislabelled)
        assert "ck_agent_sessions_branch_is_subagent" in str(kind_error.value)

        # SUBAGENT なのに継続方法が BRANCH でない。
        mismatched = _subagent_session(run, segment, attempt, primary, now, key="b")
        mismatched.continuation_mode = "FORK"
        with pytest.raises(IntegrityError) as mode_error:
            async with factory() as session, session.begin():
                session.add(mismatched)
        assert "ck_agent_sessions_branch_is_subagent" in str(mode_error.value)


@pytest.mark.asyncio
async def test_0027_only_a_subagent_may_omit_its_sdk_session(
    migrated_database_url: str,
) -> None:
    """SDK 未起動の branch は NULL を許すが、PRIMARY は許さない。

    捏造した ID を入れると「引けるように見えて引けない参照」になる——P3b が潰した不具合そのもの。
    """

    now = datetime.now(UTC)
    run, segment, attempt, primary = _run_spine(now)

    async with _session_factory(migrated_database_url) as factory:
        async with factory() as session, session.begin():
            await _insert_in_order(session, run, segment, attempt, primary)

        never_started = _subagent_session(run, segment, attempt, primary, now, key="a")
        never_started.sdk_session_id = None
        async with factory() as session, session.begin():
            session.add(never_started)

        run_b, segment_b, attempt_b, primary_b = _run_spine(now)
        primary_b.sdk_session_id = None
        with pytest.raises(IntegrityError) as primary_error:
            async with factory() as session, session.begin():
                await _insert_in_order(session, run_b, segment_b, attempt_b, primary_b)
        assert "ck_agent_sessions_primary_has_sdk_session" in str(primary_error.value)


@pytest.mark.asyncio
async def test_subagent_recorder_attaches_every_branch_to_the_primary_session(
    migrated_database_url: str,
) -> None:
    """Recorder が親を引き当て、一組の branch をまとめて保存することを確認する。

    返す ID の順は request の branch 順と一致していなければならない。ずれると Provider が
    別の路の session を指す ID を返し、監査で結論と経路の対応が入れ替わる。
    """

    now = datetime.now(UTC)
    run, segment, attempt, primary = _run_spine(now)

    async with _session_factory(migrated_database_url) as factory:
        async with factory() as session, session.begin():
            await _insert_in_order(session, run, segment, attempt, primary)

        recorder = PostgresSubagentSessionRecorder(factory)
        opened = uuid4()
        session_ids = await recorder.record_branches(
            run_id=run.id,
            run_attempt_id=attempt.id,
            drafts=[
                SubagentSessionDraft(branch_key="a", outcome="COMPLETED", sdk_session_id=opened),
                SubagentSessionDraft(branch_key="b", outcome="TIMED_OUT", sdk_session_id=None),
            ],
        )

        assert len(session_ids) == 2
        async with factory() as session:
            stored = {
                row.id: row
                for row in (
                    await session.scalars(
                        select(AgentSession).where(AgentSession.id.in_(session_ids))
                    )
                ).all()
            }
        # 返り値の順で引き当てる。ここが崩れると Provider は別の路の session を指す ID を返す。
        rows = [stored[session_id] for session_id in session_ids]
        assert [row.usage_json["branch_key"] for row in rows] == ["a", "b"]
        # engine / cwd / version は親から引き継ぐ。draft 側に持たせると食い違える。
        assert all(row.parent_session_id == primary.id for row in rows)
        assert all(row.session_kind == "SUBAGENT" for row in rows)
        assert all(row.continuation_mode == "BRANCH" for row in rows)
        assert all(row.cwd == primary.cwd and row.engine == primary.engine for row in rows)
        assert [row.status for row in rows] == ["CLOSED", "INTERRUPTED"]
        assert [row.sdk_session_id for row in rows] == [opened, None]


@pytest.mark.asyncio
async def test_subagent_recorder_refuses_to_leave_orphan_sessions(
    migrated_database_url: str,
) -> None:
    """親 PRIMARY が居ない Attempt では保存しない。宙に浮いた session 行を作らない。"""

    now = datetime.now(UTC)
    run, segment, attempt, _primary = _run_spine(now)

    async with _session_factory(migrated_database_url) as factory:
        async with factory() as session, session.begin():
            await _insert_in_order(session, run, segment, attempt)

        recorder = PostgresSubagentSessionRecorder(factory)
        with pytest.raises(SubagentSessionRecordingError):
            await recorder.record_branches(
                run_id=run.id,
                run_attempt_id=attempt.id,
                drafts=[
                    SubagentSessionDraft(branch_key="a", outcome="COMPLETED", sdk_session_id=None)
                ],
            )


@pytest.mark.asyncio
async def test_concurrent_interpret_saves_reuse_a_single_row(
    migrated_database_url: str,
) -> None:
    """同一 execution key の並行保存が単一 row へ収束し、負けた側は reuse で成功する。"""

    async with _session_factory(migrated_database_url) as factory:
        organization = _organization()
        source = _skill_source(organization.id)
        async with factory() as session, session.begin():
            session.add_all([organization, source])

        execution_key = f"sha256:{'c' * 64}"
        command = _command(source, execution_key)
        release_winner = asyncio.Event()

        async def winner_holds_lock_then_commits() -> UUID:
            # flush 済み・未 commit の一意 index entry を保持し、敗者を実際に競合させる。
            """一意 index entry を保持したまま commit を遅らせ、実競合を作る。"""

            async with factory() as session, session.begin():
                stored = await SkillRepository(session).save_model_interpretation(command)
                await session.flush()
                await release_winner.wait()
                return stored.interpretation_id

        async def loser_retries_and_reuses() -> UUID:
            """勝者の commit を待って再試行し、既存 row を再利用する。"""

            await asyncio.sleep(0.2)
            release_task = asyncio.get_running_loop().call_later(0.3, release_winner.set)
            try:
                stored = await save_execution_idempotently(factory, command)
            finally:
                release_task.cancel()
                release_winner.set()
            return stored.interpretation_id

        winner_id, loser_id = await asyncio.gather(
            winner_holds_lock_then_commits(), loser_retries_and_reuses()
        )
        assert winner_id == loser_id

        async with factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(SkillInterpretation)
                .where(SkillInterpretation.skill_source_id == source.id)
            )
        assert count == 1


@pytest.mark.asyncio
async def test_organization_isolation_hides_foreign_interpretations(
    migrated_database_url: str,
) -> None:
    """他 Organization の ID を知っていても、越権は不存在と同じ失敗になる。"""

    async with _session_factory(migrated_database_url) as factory:
        owner = _organization()
        intruder_organization_id = uuid4()
        source = _skill_source(owner.id)
        interpretation = _interpretation(source, checksum=f"sha256:{'d' * 64}")
        async with factory() as session, session.begin():
            session.add_all([owner, source])
            session.add(interpretation)

        async with factory() as session:
            repository = SkillRepository(session)
            with pytest.raises(SkillInterpretationNotFoundError):
                await repository.get_model_interpretation(
                    organization_id=intruder_organization_id,
                    interpretation_id=interpretation.id,
                )
            with pytest.raises(SkillSourceNotFoundError):
                await repository.find_model_interpretation(
                    organization_id=intruder_organization_id,
                    skill_source_id=source.id,
                    interpreter_version="db-verify/1.0.0",
                    execution_key=interpretation.checksum,
                )
            # 正当な Organization からは同じ ID で取得できる(作用域条件そのものの回帰)。
            owned = await repository.get_model_interpretation(
                organization_id=owner.id,
                interpretation_id=interpretation.id,
            )
            assert owned.interpretation_id == interpretation.id


@pytest.mark.asyncio
async def test_create_version_draft_inserts_manifest_after_version(
    migrated_database_url: str,
) -> None:
    """DRAFT 発行が SkillVersion→RuntimeManifest の FK 順序を守り、実機 FK 違反を起こさない。

    version と manifest は relationship を持たないため、明示 flush が無いと UOW の挿入順が
    環境依存になり runtime_manifests の FK 違反を招く。本番で観測された不具合の回帰防止。
    """

    async with _session_factory(migrated_database_url) as factory:
        organization = _organization()
        source = _skill_source(organization.id)
        interpretation = _interpretation(source, checksum=f"sha256:{'f0' * 32}")
        async with factory() as session, session.begin():
            session.add_all([organization, source])
            session.add(interpretation)

        skill_key = f"db-verify-draft-{uuid4().hex[:8]}"
        command = CreateSkillVersionDraftCommand(
            organization_id=source.organization_id,
            interpretation_id=interpretation.id,
            manifest={
                "manifest_version": "projectmind/v1alpha1",
                "identity": {"skill_key": skill_key},
            },
            manifest_checksum=f"sha256:{'ab' * 32}",
            gate_passed=True,
            gate_findings=(),
            interpretation_diff={},
        )
        async with factory() as session, session.begin():
            stored = await SkillRepository(session).create_version_draft(command)

        # RuntimeManifest が実際に永続化され、version を親として読めることを確認する。
        async with factory() as session:
            manifest = (
                await session.scalars(
                    select(RuntimeManifest).where(
                        RuntimeManifest.skill_version_id == stored.skill_version_id
                    )
                )
            ).one()
        assert manifest.checksum == command.manifest_checksum


@pytest.mark.asyncio
async def test_project_skill_version_controls_new_run_visibility(
    migrated_database_url: str,
) -> None:
    """未有効化・停用済みの精確版を Run 解決から同じ 404 相当へ隠す。"""

    async with _session_factory(migrated_database_url) as factory:
        now = datetime.now(UTC)
        organization = _organization()
        project_a = Project(
            id=uuid4(),
            organization_id=organization.id,
            key=f"db-enable-a-{uuid4().hex[:8]}",
            name="Enablement A",
            description="",
            status="ACTIVE",
            settings_json={},
            retention_days=90,
            created_at=now,
            updated_at=now,
        )
        project_b = Project(
            id=uuid4(),
            organization_id=organization.id,
            key=f"db-enable-b-{uuid4().hex[:8]}",
            name="Enablement B",
            description="",
            status="ACTIVE",
            settings_json={},
            retention_days=90,
            created_at=now,
            updated_at=now,
        )
        source = _skill_source(organization.id)
        interpretation = _interpretation(source, checksum=f"sha256:{'12' * 32}")
        skill = Skill(
            id=uuid4(),
            organization_id=organization.id,
            key=f"db-enable-skill-{uuid4().hex[:8]}",
            name="Enablement Skill",
            description="",
            status="PUBLISHED",
            created_at=now,
            updated_at=now,
        )
        version = SkillVersion(
            id=uuid4(),
            skill_id=skill.id,
            version="1.0.0",
            skill_source_id=source.id,
            interpretation_id=interpretation.id,
            status="PUBLISHED",
            gate_report_json={"passed": True, "findings": []},
            published_by=uuid4(),
            published_at=now,
            created_at=now,
            updated_at=now,
        )
        manifest = RuntimeManifest(
            id=uuid4(),
            interpretation_id=interpretation.id,
            skill_version_id=version.id,
            manifest_version="projectmind/v1alpha1",
            manifest_json={"manifest_version": "projectmind/v1alpha1", "tasks": []},
            checksum=f"sha256:{'34' * 32}",
            created_at=now,
        )
        async with factory() as session, session.begin():
            session.add_all([organization, project_a, project_b, source, interpretation, skill])
            await session.flush()
            session.add(version)
            await session.flush()
            session.add(manifest)

        async with factory() as session, session.begin():
            await SkillRepository(session).enable_project_skill_version(
                organization_id=organization.id,
                project_id=project_a.id,
                skill_version_id=version.id,
                enabled_by=uuid4(),
            )

        async with factory() as session:
            repository = SkillRepository(session)
            resolved = await repository.get_published_task_binding(
                project_id=project_a.id,
                skill_version_id=version.id,
            )
            assert resolved[1].id == version.id
            with pytest.raises(SkillVersionNotFoundError):
                await repository.get_published_task_binding(
                    project_id=project_b.id,
                    skill_version_id=version.id,
                )

        async with factory() as session, session.begin():
            await SkillRepository(session).disable_project_skill_version(
                organization_id=organization.id,
                project_id=project_a.id,
                skill_version_id=version.id,
            )

        async with factory() as session:
            with pytest.raises(SkillVersionNotFoundError):
                await SkillRepository(session).get_published_task_binding(
                    project_id=project_a.id,
                    skill_version_id=version.id,
                )


@pytest.mark.asyncio
async def test_module_composition_round_trip_with_project_isolation(
    migrated_database_url: str,
) -> None:
    """0015 実機 schema 上で module の作成/一覧/更新/削除と Project 隔離を往復検証する。"""

    async with _session_factory(migrated_database_url) as factory:
        now = datetime.now(UTC)
        organization = Organization(
            id=uuid4(),
            name="DB Verify Org",
            status="ACTIVE",
            settings_json={},
            default_retention_days=90,
            created_at=now,
            updated_at=now,
        )
        project = Project(
            id=uuid4(),
            organization_id=organization.id,
            key=f"db-verify-{uuid4().hex[:8]}",
            name="DB Verify Project",
            description="module round trip",
            status="ACTIVE",
            settings_json={},
            retention_days=90,
            created_at=now,
            updated_at=now,
        )
        source = _skill_source(organization.id)
        interpretation = _interpretation(source, checksum=f"sha256:{'e' * 64}")
        skill = Skill(
            id=uuid4(),
            organization_id=organization.id,
            key=f"db-verify-skill-{uuid4().hex[:8]}",
            name="DB Verify Skill",
            description="round trip",
            status="PUBLISHED",
            created_at=now,
            updated_at=now,
        )
        published = SkillVersion(
            id=uuid4(),
            skill_id=skill.id,
            version="1.0.0",
            skill_source_id=source.id,
            interpretation_id=interpretation.id,
            status="PUBLISHED",
            gate_report_json={"passed": True},
            published_by=None,
            published_at=now,
            created_at=now,
            updated_at=now,
        )
        enablement = ProjectSkillVersion(
            id=uuid4(),
            project_id=project.id,
            skill_version_id=published.id,
            enabled_by=uuid4(),
            enabled_at=now,
            disabled_at=None,
        )
        async with factory() as session, session.begin():
            session.add_all([organization, project, source, interpretation, skill])
            await session.flush()
            session.add(published)
            await session.flush()
            session.add(enablement)

        creator = uuid4()
        async with factory() as session, session.begin():
            created = await CompositionRepository(session).create(CreateModuleCommand(
                project_id=project.id,
                created_by=creator,
                name="品质分析",
                description="round trip module",
                skill_version_ids=(published.id,),
            ))
        assert created.skills[0].skill_name == "DB Verify Skill"

        # 一覧は自 Project にだけ見え、他 Project は空になる(有効化 join の隔離)。
        async with factory() as session:
            mine = await CompositionRepository(session).list_for_project(project.id)
            other = await CompositionRepository(session).list_for_project(uuid4())
        assert [module.module_id for module in mine] == [created.module_id]
        assert other == []

        # 未発行 SkillVersion への束縛は実機 join 検証で拒否される。
        async with factory() as session, session.begin():
            with pytest.raises(ModuleSkillInvalidError):
                await CompositionRepository(session).update(UpdateModuleCommand(
                    project_id=project.id,
                    module_id=created.module_id,
                    name="更名",
                    description="",
                    skill_version_ids=(uuid4(),),
                ))

        async with factory() as session, session.begin():
            updated = await CompositionRepository(session).update(UpdateModuleCommand(
                project_id=project.id,
                module_id=created.module_id,
                name="更名后的模块",
                description="renamed",
                skill_version_ids=(published.id,),
            ))
        assert updated.name == "更名后的模块"

        # 他 Project からの削除は不存在と同型で失敗し、自 Project の削除で消える。
        async with factory() as session, session.begin():
            with pytest.raises(ModuleNotFoundError):
                await CompositionRepository(session).delete(
                    project_id=uuid4(), module_id=created.module_id
                )
        async with factory() as session, session.begin():
            await CompositionRepository(session).delete(
                project_id=project.id, module_id=created.module_id
            )
        async with factory() as session:
            remaining = await CompositionRepository(session).list_for_project(project.id)
        assert remaining == []


@pytest.mark.asyncio
async def test_unreferenced_jaf_seed_is_removed_by_full_migration_chain(
    migrated_database_url: str,
) -> None:
    """0001→head の全鎖適用後、参照の無い JAF seed と seed Project が残らないことを確認する。

    0024 は「Run 等から参照されていない seed だけを削除する」条件付き削除であり、まっさらな
    配備(この fixture がまさにその状態)では利用者が作っていない Project と DEPRECATED Skill が
    画面へ出てはならない。参照がある場合の保持は
    `test_referenced_bootstrap_seed_survives_removal_migration` が守る。
    """

    async with _session_factory(migrated_database_url) as factory:
        async with factory() as session:
            version = await session.get(SkillVersion, LEGACY_JAF_SKILL_VERSION_ID)
            manifest = await session.get(RuntimeManifest, LEGACY_JAF_RUNTIME_MANIFEST_ID)
            seed_projects = (
                await session.scalars(
                    select(Project.key).where(
                        Project.key.in_(("jaf-m0", "system-skills"))
                    )
                )
            ).all()

        assert version is None
        assert manifest is None
        assert list(seed_projects) == []


@pytest.mark.asyncio
async def test_referenced_bootstrap_seed_survives_removal_migration() -> None:
    """参照されている seed は 0024 でも削除されず、audit asset として残ることを確認する。

    AGENTS の「歴史的な SkillVersion/Run snapshot は read-only audit asset として保持」を守る。
    0023 まで適用した状態で seed 版本を composition から、seed Project を member から参照させ、
    その後 head へ上げても両者が生き残ることを固定する。
    """

    scratch = f"{SCRATCH_DATABASE}_referenced"
    scratch_url = _replace_database(os.environ["PROJECTMIND_DATABASE_URL"], scratch)
    try:
        # 既に event loop 内のため、同期 fixture と違い asyncio.run は使えない。
        await _run_admin_sql(f'CREATE DATABASE "{scratch}"')
    except OSError as error:
        pytest.skip(f"real PostgreSQL is unreachable: {error}")
    try:
        _alembic_upgrade(scratch_url, "0023_managed_secrets")
        async with (
            _session_factory(scratch_url) as factory,
            factory() as session,
            session.begin(),
        ):
            now = datetime.now(UTC)
            organization_id = (
                await session.scalars(select(Organization.id).limit(1))
            ).one()
            # seed 版本を composition item から参照させる(0024 の skill 保持 guard)。
            composition_id = uuid4()
            session.add(
                SkillComposition(
                    id=composition_id,
                    organization_id=organization_id,
                    name="Seed guard composition",
                    description="Keeps the historical JAF seed referenced",
                    presentation="module",
                    behavior_prompt="",
                    config_json={},
                    status="ACTIVE",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                SkillCompositionItem(
                    id=uuid4(),
                    composition_id=composition_id,
                    skill_version_id=LEGACY_JAF_SKILL_VERSION_ID,
                    sort_order=0,
                    enabled=True,
                    config_override_json={},
                    created_at=now,
                )
            )
            # seed Project を member から参照させる(0024 の Project 保持 guard)。
            user_id = uuid4()
            session.add(
                User(
                    id=user_id,
                    organization_id=organization_id,
                    email=f"seed-guard-{uuid4().hex[:8]}@example.test",
                    password_hash="x" * 32,
                    display_name="Seed Guard",
                    system_role="ADMIN",
                    status="ACTIVE",
                    last_login_at=None,
                )
            )
            m0_project_id = (
                await session.scalars(select(Project.id).where(Project.key == "jaf-m0"))
            ).one()
            session.add(
                ProjectMember(
                    id=uuid4(),
                    project_id=m0_project_id,
                    user_id=user_id,
                    status="ACTIVE",
                    joined_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )

        _alembic_upgrade(scratch_url, "head")

        async with (
            _session_factory(scratch_url) as factory,
            factory() as session,
        ):
            version = await session.get(SkillVersion, LEGACY_JAF_SKILL_VERSION_ID)
            project = await session.get(Project, m0_project_id)

        assert version is not None, "referenced seed skill version must be preserved"
        assert project is not None, "referenced seed project must be preserved"
    finally:
        await _run_admin_sql(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')


@pytest.mark.asyncio
async def test_managed_secret_round_trips_ciphertext_without_persisting_plaintext(
    migrated_database_url: str,
) -> None:
    """MANAGED は密文だけを保存し、KEK 復号で明文へ round-trip、rotation で再封入できる。"""

    plaintext = "redmine-api-key-1234567890"
    v1_entry = "v1:" + base64.b64encode(os.urandom(32)).decode()
    cipher = load_secret_cipher(v1_entry)
    assert cipher is not None

    async with _session_factory(migrated_database_url) as factory:
        now = datetime.now(UTC)
        organization = _organization()
        project = Project(
            id=uuid4(),
            organization_id=organization.id,
            key=f"db-managed-{uuid4().hex[:8]}",
            name="Managed Secret",
            description="",
            status="ACTIVE",
            settings_json={},
            retention_days=90,
            created_at=now,
            updated_at=now,
        )
        async with factory() as session, session.begin():
            session.add_all([organization, project])

        # MANAGED SecretReference を作成し、密文行(LargeBinary)を書き込む。
        async with factory() as session, session.begin():
            stored = await IntegrationRepository(
                session, secret_cipher=cipher
            ).create_secret_reference(
                CreateSecretReferenceCommand(
                    project_id=project.id,
                    name="Managed token",
                    provider="redmine",
                    resolver=SecretResolver.MANAGED,
                    locator=MANAGED_SECRET_LOCATOR,
                    key_version="2026-07",
                    created_by=uuid4(),
                    secret_value=plaintext,
                )
            )
        reference_id = stored.secret_reference_id

        # 密文行は存在し、平文は保存されていない。
        async with factory() as session:
            material = (
                await session.scalars(
                    select(ManagedSecretMaterial).where(
                        ManagedSecretMaterial.secret_reference_id == reference_id
                    )
                )
            ).one()
            assert material.kek_version == "v1"
            assert plaintext.encode("utf-8") not in material.ciphertext

        # resolve → decrypt で明文へ round-trip する。
        async with factory() as session:
            resolved = await IntegrationRepository(session).resolve_secret_reference(
                project_id=project.id, secret_reference_id=reference_id
            )
        assert resolved.managed_material is not None
        assert DeploymentSecretResolver(cipher=cipher).resolve(resolved) == plaintext

        # KEK rotation: v2 を active、v1 を旧鍵にして全密文を再封入する。
        v2_entry = "v2:" + base64.b64encode(os.urandom(32)).decode()
        rotated_cipher = load_secret_cipher(f"{v2_entry},{v1_entry}")
        assert rotated_cipher is not None
        async with factory() as session, session.begin():
            rotated, skipped = await IntegrationRepository(
                session, secret_cipher=rotated_cipher
            ).rotate_managed_material(rotated_cipher)
        assert (rotated, skipped) == (1, 0)

        # rotation 後は kek_version が v2 になり、旧鍵無しでも明文へ復号できる。
        async with factory() as session:
            rotated_material = (
                await session.scalars(
                    select(ManagedSecretMaterial).where(
                        ManagedSecretMaterial.secret_reference_id == reference_id
                    )
                )
            ).one()
            assert rotated_material.kek_version == "v2"

        active_only_cipher = load_secret_cipher(v2_entry)
        assert active_only_cipher is not None
        async with factory() as session:
            resolved = await IntegrationRepository(session).resolve_secret_reference(
                project_id=project.id, secret_reference_id=reference_id
            )
        assert DeploymentSecretResolver(cipher=active_only_cipher).resolve(resolved) == plaintext


@pytest.mark.asyncio
async def test_run_binding_revalidation_rejects_post_creation_changes(
    migrated_database_url: str,
) -> None:
    """Run 凍結 binding の再検証が、実 DB 行の改竄・無効化・すり替えを fail closed する。

    この判定は Redmine 読取と repository 読取/物化が共有する唯一の関門であり、緩むと Run 作成
    時点の権限上限を超えた資源へ到達できてしまう (計画 §19 W4)。
    """

    async with _session_factory(migrated_database_url) as factory:
        now = datetime.now(UTC)
        organization = _organization()
        project = Project(
            id=uuid4(),
            organization_id=organization.id,
            key=f"db-binding-{uuid4().hex[:8]}",
            name="Run Binding",
            description="",
            status="ACTIVE",
            settings_json={},
            retention_days=90,
            created_at=now,
            updated_at=now,
        )
        integration = Integration(
            id=uuid4(),
            project_id=project.id,
            name="Repository",
            kind="repository",
            provider="git",
            status="ACTIVE",
            revision=1,
            capabilities_json=["repository.read/v1"],
            scope_json={"paths": ["src"], "revisions": ["main"]},
            config_json={
                "repository_uri": "https://git.example.invalid/project.git",
                "default_revision": "main",
            },
            secret_reference_id=None,
            created_by=uuid4(),
            disabled_at=None,
            created_at=now,
            updated_at=now,
        )
        run = Run(
            id=uuid4(),
            project_id=project.id,
            task_id=uuid4(),
            trigger_type="immediate",
            idempotency_key=f"db-binding-{uuid4().hex}",
            request_hash=uuid4().hex * 2,
            status="RUNNING",
            input_json={},
            task_snapshot_json={},
            permission_snapshot_json={},
            selected_sources_json={},
            limits_snapshot_json={},
            row_version=1,
            started_at=now,
            finished_at=None,
            error_json=None,
            created_at=now,
            updated_at=now,
        )
        scope = {"paths": ["src"], "revisions": ["main"]}
        binding = ResourceBinding(
            id=uuid4(),
            project_id=project.id,
            scope_level=ResourceBindingLevel.RUN.value,
            scope_key=str(run.id),
            requirement_key="source_repository",
            resource_kind="repository",
            integration_id=integration.id,
            run_id=run.id,
            source_binding_id=None,
            provider="git",
            capability_version="repository.read/v1",
            revision="1",
            scope_json=scope,
            checksum=binding_checksum(
                project_id=project.id,
                scope_level=ResourceBindingLevel.RUN,
                scope_key=str(run.id),
                requirement_key="source_repository",
                resource_kind="repository",
                integration_id=integration.id,
                provider="git",
                capability_version="repository.read/v1",
                revision="1",
                scope=scope,
            ),
            created_by=uuid4(),
            disabled_at=None,
            created_at=now,
            updated_at=now,
        )
        async with factory() as session, session.begin():
            # relationship の無い FK 親子は unit of work が順序を保証しないため、親を追加した
            # 直後に flush して INSERT 順を固定する (実 DB でのみ FK 違反として現れる)。
            session.add(organization)
            await session.flush()
            session.add(project)
            await session.flush()
            session.add(integration)
            session.add(run)
            await session.flush()
            session.add(binding)

        async def _load(session: AsyncSession, *, provider: str = "git") -> None:
            """凍結 binding の実行直前再検証を呼び出す。"""

            await load_bound_run_resource(
                session,
                project_id=project.id,
                run_id=run.id,
                binding_id=binding.id,
                integration_id=integration.id,
                provider=provider,
                capability="repository.read/v1",
            )

        # 凍結時と同じ状態なら scope をそのまま返す。
        async with factory() as session:
            bound = await load_bound_run_resource(
                session,
                project_id=project.id,
                run_id=run.id,
                binding_id=binding.id,
                integration_id=integration.id,
                provider="git",
                capability="repository.read/v1",
            )
        assert bound.scope == scope
        assert bound.integration.config["repository_uri"].startswith("https://")

        # provider のすり替えは受け付けない。
        async with factory() as session:
            with pytest.raises(RunBindingError):
                await _load(session, provider="svn")

        # scope だけを書き換えた行は checksum と一致せず拒否される。
        async with factory() as session, session.begin():
            row = await session.get(ResourceBinding, binding.id)
            assert row is not None
            row.scope_json = {"paths": ["src", "secrets"], "revisions": ["main"]}
        async with factory() as session:
            with pytest.raises(RunBindingError):
                await _load(session)

        # scope を戻したうえで Integration を無効化すると、やはり実行前に止まる。
        async with factory() as session, session.begin():
            row = await session.get(ResourceBinding, binding.id)
            assert row is not None
            row.scope_json = scope
            integration_row = await session.get(Integration, integration.id)
            assert integration_row is not None
            integration_row.status = "DISABLED"
        async with factory() as session:
            with pytest.raises(RunBindingError):
                await _load(session)
@pytest.mark.asyncio
async def test_schedule_claim_lets_only_one_worker_win_the_same_occurrence(
    migrated_database_url: str,
) -> None:
    """0025 実機 schema 上で `next_run_at` の CAS 認領が一度しか成功しないことを確認する。

    調度は複数 Worker が同時に tick する前提なので、同じ発火を二人が掴めると Run が二重に
    走る。実 DB の UPDATE 影響行数でしか確認できない性質のため、ここで固定する。
    """

    async with _session_factory(migrated_database_url) as factory:
        now = datetime.now(UTC)
        organization = _organization()
        project = Project(
            id=uuid4(),
            organization_id=organization.id,
            key=f"db-schedule-{uuid4().hex[:8]}",
            name="Schedule Project",
            description="",
            status="ACTIVE",
            settings_json={},
            retention_days=90,
            created_at=now,
            updated_at=now,
        )
        owner = User(
            id=uuid4(),
            organization_id=organization.id,
            email=f"schedule-{uuid4().hex[:8]}@example.com",
            password_hash="$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA",
            display_name="Schedule Owner",
            system_role="USER",
            status="ACTIVE",
            created_at=now,
            updated_at=now,
        )
        source = _skill_source(organization.id)
        interpretation = _interpretation(source, checksum=f"sha256:{'56' * 32}")
        skill = Skill(
            id=uuid4(),
            organization_id=organization.id,
            key=f"db-schedule-skill-{uuid4().hex[:8]}",
            name="Schedule Skill",
            description="",
            status="PUBLISHED",
            created_at=now,
            updated_at=now,
        )
        version = SkillVersion(
            id=uuid4(),
            skill_id=skill.id,
            version="1.0.0",
            skill_source_id=source.id,
            interpretation_id=interpretation.id,
            status="PUBLISHED",
            gate_report_json={"passed": True, "findings": []},
            published_by=owner.id,
            published_at=now,
            created_at=now,
            updated_at=now,
        )
        occurrence = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
        schedule = TaskSchedule(
            id=uuid4(),
            project_id=project.id,
            name="nightly",
            kind="CRON",
            status="ACTIVE",
            timezone="Asia/Tokyo",
            cron_expression="0 * * * *",
            run_at=None,
            end_at=None,
            max_runs=None,
            skill_version_id=version.id,
            task_key="analyze",
            input_json={},
            sources_json={},
            next_run_at=occurrence,
            run_count=0,
            missed_count=0,
            created_by=owner.id,
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        async with factory() as session, session.begin():
            # relationship の無い FK 親子は追加直後に flush して INSERT 順を固定する。
            session.add(organization)
            await session.flush()
            session.add_all([project, owner, source, interpretation, skill])
            await session.flush()
            session.add(version)
            await session.flush()
            session.add(schedule)

        following = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)

        async def _claim() -> bool:
            """期待発火時刻で schedule の認領を試みる。"""

            async with factory() as session, session.begin():
                return await ScheduleRepository(session).claim(
                    schedule_id=schedule.id,
                    expected_next_run_at=occurrence,
                    next_run_at=following,
                    missed=0,
                )

        assert await _claim() is True
        # 二人目は同じ発火時刻を期待して外れる。
        assert await _claim() is False

        async with factory() as session:
            record = await ScheduleRepository(session).get(
                project_id=project.id, schedule_id=schedule.id
            )
        assert record.next_run_at == following

        # 別 Project から同じ schedule は読めない (越境は不存在と同じ扱い)。
        async with factory() as session:
            with pytest.raises(ScheduleNotFoundError):
                await ScheduleRepository(session).get(
                    project_id=uuid4(), schedule_id=schedule.id
                )
