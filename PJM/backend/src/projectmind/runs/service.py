"""M0 Run use case と server-side snapshot 生成を実装する。"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.agent.domain import AgentEvent
from projectmind.agent.subagent import SUBAGENT_DISPATCH_CAPABILITY
from projectmind.agent.task_brief import resolve_execution_profile
from projectmind.agent.tool_policy import DENIED_BUILTIN_TOOLS
from projectmind.auth.sessions import UnauthorizedSessionError
from projectmind.documents.binding import resolve_document_binding
from projectmind.documents.repository import DocumentRepository
from projectmind.documents.snapshot import DOCUMENT_READ_CAPABILITY, DocumentSnapshotError
from projectmind.effects.domain import (
    ChangeProposalDraft,
    ChangeProposalExpiredError,
    DecideProposalCommand,
    ProposalDecisionResult,
)
from projectmind.effects.proposal import CHANGE_PROPOSE_CAPABILITY
from projectmind.integrations.domain import (
    IntegrationStatus,
    ResolvedRunBinding,
)
from projectmind.integrations.repository import IntegrationRepository
from projectmind.projects.domain import ProjectNotFoundError
from projectmind.projects.repository import ProjectRepository
from projectmind.runs.creation_participation import RunCreationAuthority, RunCreationParticipant
from projectmind.runs.creation_request import CREATION_REQUEST_FIELD, TaskRunIntent
from projectmind.runs.domain import (
    AgentSessionMetadata,
    CancelledRun,
    ClaimedRun,
    CreatedRun,
    CreateRunCommand,
    IdempotencyConflictError,
    InteractionExpiredError,
    PreparedExecution,
    RespondedInteraction,
    RunAttemptStatus,
    RunDetail,
    RunHistoryPage,
    RunResultRecord,
    RunStatus,
    StoredRunEvent,
    TaskLastRun,
    TaskSourceSelectionError,
    derive_task_id,
    lease_token_hash,
)
from projectmind.runs.interaction import (
    INTERACTION_REQUEST_CAPABILITY,
    InteractionRequestDraft,
)
from projectmind.runs.repository import RunRepository
from projectmind.skills.capability_blueprint import resolve_capability_blueprint
from projectmind.skills.resource_binding import is_write_capability
from projectmind.skills.task_catalog import ResolvedTaskRun
from projectmind.users.access import authorize_user_access, validate_user_access
from projectmind.users.domain import UserAccess
from projectmind.users.repository import UserRepository

# M0 では Agent に組み込み file/shell/web tool を一切公開しない。これはセキュリティ境界。
M0_DENIED_BUILTIN_TOOLS = tuple(sorted(DENIED_BUILTIN_TOOLS))

# M0 の実行 limit は Manifest ではなく platform policy として固定する。
M0_LIMITS_SNAPSHOT = {
    "wall_timeout_seconds": 900,
    "max_turns": 20,
    "max_output_bytes": 1_048_576,
}


class RunService:
    """Transaction 境界を所有して Run use case を実行する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Database session factory を保持する。"""

        self._session_factory = session_factory

    async def create_task_run(
        self,
        *,
        project_id: UUID,
        resolved: ResolvedTaskRun,
        input_json: dict[str, Any],
        sources: dict[str, str],
        idempotency_key: str,
        trace_id: str | None,
        actor_id: UUID,
        authorization: UserAccess | RunCreationParticipant,
    ) -> CreatedRun:
        """解決済み PUBLISHED task と現在の資格から通用 Run を作成する。

        入力は SkillService が task の input schema で検証済み。ここでは資源選択を
        blueprint の resource_requirements と突き合わせ、精確 version 束縛と platform 権限・limit
        を不変 snapshot に固定する。
        """

        async with self._creation_transaction(
            authorization,
            project_id=project_id,
            skill_version_id=resolved.skill_version_id,
            task_key=resolved.task_key,
            input_json=input_json,
            sources=sources,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        ) as (session, intent, authority):
            # 精確 task の同一性は現在の権限や資源の変化から独立させる。
            task_id = derive_task_id(
                skill_version_id=intent.skill_version_id, task_key=intent.task_key
            )
            repository = RunRepository(session)
            replay = await repository.find_task_run_replay(
                intent=intent, idempotency_key=idempotency_key
            )
            if replay is not None:
                await _complete_creation(authorization, session, replay)
                return replay
            _require_creation_namespace(idempotency_key, authorization)
            if not isinstance(authorization, UserAccess):
                await authorization.before_create(session)
            input_schema_json = deepcopy(resolved.input_schema)
            output_schema_json = deepcopy(resolved.output_schema)
            manifest = resolved.skill_snapshot.get("manifest")
            if not isinstance(manifest, dict):
                raise ValueError("Resolved task is missing its frozen RuntimeManifest")
            blueprint = resolve_capability_blueprint(manifest)
            if blueprint is None:
                raise ValueError("Resolved task is missing its CapabilityBlueprint")
            execution_profile = resolve_execution_profile(blueprint).profile.value
            has_apply_intent = any(
                isinstance(effect, dict) and effect.get("mode") == "apply"
                for effect in blueprint.get("effect_intents", [])
            )
            integration_repository = IntegrationRepository(session)
            try:
                selected_sources, run_bindings = await _resolve_selected_sources(
                    resolved,
                    intent.sources,
                    blueprint=blueprint,
                    project_id=project_id,
                    integration_repository=integration_repository,
                    document_repository=DocumentRepository(session),
                )
            except TaskSourceSelectionError:
                # 解析中に同一要求の勝者が commit した場合、その凍結事実を返す。不存在なら
                # 元の前提失効を保持し、現在の資源を代用して新しい Run を作らない。
                replay = await repository.find_task_run_replay(
                    intent=intent, idempotency_key=idempotency_key
                )
                if replay is not None:
                    await _complete_creation(authorization, session, replay)
                    return replay
                raise
            command = CreateRunCommand(
                project_id=project_id,
                task_id=task_id,
                idempotency_key=idempotency_key,
                input_json=intent.input_json,
                task_snapshot_json={
                    CREATION_REQUEST_FIELD: intent.to_json(),
                    "task_id": str(task_id),
                    "task_key": resolved.task_key,
                    "capability": resolved.capability,
                    "skill_version_id": str(resolved.skill_version_id),
                    "manifest_checksum": resolved.manifest_checksum,
                    "input_schema": resolved.input_schema_checksum,
                    "output_schema": resolved.output_schema_checksum,
                    "input_schema_checksum": resolved.input_schema_checksum,
                    "output_schema_checksum": resolved.output_schema_checksum,
                    "input_schema_json": input_schema_json,
                    "output_schema_json": output_schema_json,
                    "result_kind": "OUTCOME_ENVELOPE",
                    "task_output_schema": resolved.task_output_schema_checksum,
                    "task_output_schema_checksum": resolved.task_output_schema_checksum,
                    "task_output_schema_json": deepcopy(resolved.task_output_schema),
                    "skill_snapshots": [resolved.skill_snapshot],
                },
                permission_snapshot_json={
                    "mode": "auto_read_only",
                    "actor_id": str(actor_id),
                    "actor_system_role": authority.actor_system_role,
                    "project_membership": authority.project_membership,
                    "execution_profile": execution_profile,
                    # 構造化質問と扇出は外部資源への権限ではなく platform control capability。
                    # 新規 Run にだけ固定し、歴史 snapshot へ後付けしない。扇出を無条件に付ける
                    # のは、並行させるかどうかが実行時の判断であり blueprint の語義ではないため
                    # (計画 §23 D2)。子は Run 予算を分け合うだけで上限を増やさない (D4)。
                    "allowed_capabilities": sorted(
                        {
                            *resolved.allowed_capabilities,
                            INTERACTION_REQUEST_CAPABILITY,
                            SUBAGENT_DISPATCH_CAPABILITY,
                            *({CHANGE_PROPOSE_CAPABILITY} if has_apply_intent else set()),
                        }
                    ),
                    "denied_builtin_tools": list(M0_DENIED_BUILTIN_TOOLS),
                },
                selected_sources_json=selected_sources,
                limits_snapshot_json=dict(M0_LIMITS_SNAPSHOT),
                trace_id=trace_id,
                skill_snapshots_json=(resolved.skill_snapshot,),
            )
            created = await repository.create_idempotent(command)
            if created.idempotent_replay:
                await _complete_creation(authorization, session, created)
                return created
            frozen_sources = deepcopy(selected_sources)
            for binding in run_bindings:
                frozen = await integration_repository.freeze_run_binding(
                    run_id=created.run_id,
                    project_id=project_id,
                    actor_id=actor_id,
                    binding=binding,
                )
                source = frozen_sources.get(binding.requirement_key)
                if not isinstance(source, dict):
                    raise ValueError("Resolved Integration source snapshot is missing")
                source.update(
                    {
                        "binding_id": str(frozen.binding_id),
                        "binding_checksum": frozen.checksum,
                        "binding_capability": frozen.capability_version,
                    }
                )
            if run_bindings:
                await repository.replace_initial_selected_sources(
                    run_id=created.run_id,
                    selected_sources=frozen_sources,
                )
            await _complete_creation(authorization, session, created)
            return created

    async def find_task_run_replay(
        self,
        *,
        project_id: UUID,
        skill_version_id: UUID,
        task_key: str,
        input_json: dict[str, Any],
        sources: dict[str, str],
        actor_id: UUID,
        idempotency_key: str,
        authorization: UserAccess | RunCreationParticipant,
    ) -> CreatedRun | None:
        """API と調度が現在の Project 授権後に、元の要求を先に確認する共通入口。"""

        async with self._creation_transaction(
            authorization,
            project_id=project_id,
            skill_version_id=skill_version_id,
            task_key=task_key,
            input_json=input_json,
            sources=sources,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        ) as (session, intent, _):
            replay = await RunRepository(session).find_task_run_replay(
                intent=intent, idempotency_key=idempotency_key
            )
            if replay is None:
                _require_creation_namespace(idempotency_key, authorization)
            else:
                await _complete_creation(authorization, session, replay)
            return replay

    @asynccontextmanager
    async def _creation_transaction(
        self,
        authorization: UserAccess | RunCreationParticipant,
        *,
        project_id: UUID,
        skill_version_id: UUID,
        task_key: str,
        input_json: dict[str, Any],
        sources: dict[str, str],
        actor_id: UUID,
        idempotency_key: str,
    ) -> AsyncIterator[tuple[AsyncSession, TaskRunIntent, RunCreationAuthority]]:
        """普通要求の原会話と内部認領を区別し、すべての確認/作成出口を保護する。"""

        if isinstance(authorization, UserAccess):
            validate_user_access(authorization)
            if actor_id != authorization.actor.user_id:
                raise UnauthorizedSessionError("Authentication is required")
        elif not isinstance(authorization, RunCreationParticipant):
            raise TypeError("Run creation requires user access or an internal claim participant")
        make_intent = partial(
            _task_run_intent,
            project_id=project_id,
            skill_version_id=skill_version_id,
            task_key=task_key,
            input_json=deepcopy(input_json),
            sources=deepcopy(sources),
            actor_id=actor_id,
        )
        async with self._session_factory() as session, session.begin():
            if not isinstance(authorization, UserAccess):
                # Worker は人間の login に依存させず、元の持久 claim と結算の門禁を維持する。
                intent = make_intent()
                authority = await authorization.authorize(
                    session, intent=intent, idempotency_key=idempotency_key
                )
                yield session, intent, authority
                return
            users = await UserRepository(session).lock_users(
                access=authorization,
                target_id=None,
                include_target_sessions=False,
                read_only_actor=True,
            )
            authorize_user_access(
                authorization, users, now=datetime.now(UTC), admin=False, write=True
            )
            projects = ProjectRepository(session)
            try:
                project = await projects.lock_write_access(user=users.actor, project_id=project_id)
            except ProjectNotFoundError:
                # 不存在で lock helper が早期拒否しても、待機中の会話失効を見落とさない。
                authorize_user_access(
                    authorization, users, now=datetime.now(UTC), admin=False, write=True
                )
                raise

            def require_current_access() -> None:
                """新時刻で原会話を先に検証し、失効時に Project 状態を漏らさない。"""

                authorize_user_access(
                    authorization, users, now=datetime.now(UTC), admin=False, write=True
                )
                projects.require_active_write_access(project)

            require_current_access()
            authority = RunCreationAuthority(
                actor_system_role=users.actor.system_role,
                project_membership=(
                    "ADMIN_BYPASS" if users.actor.system_role == "ADMIN" else "ACTIVE"
                ),
            )
            try:
                # 選択構文の拒否も現在の資格の後に行い、旧認証から状態を推測させない。
                yield session, make_intent(), authority
            except (TaskSourceSelectionError, IdempotencyConflictError):
                require_current_access()
                raise
            # 重放/不存在も同じ出口を通る。FK 等の待機後に失効すれば全初期書込を rollback。
            # DB 障害・取消・commit 応答未知は変換せず、別 key の自動再試行も行わない。
            await session.flush()
            require_current_access()

    async def validate_task_sources(
        self,
        *,
        project_id: UUID,
        resolved: ResolvedTaskRun,
        sources: dict[str, str],
    ) -> None:
        """Run を作らずに資源選択だけを検証する (計画 §22 の schedule 保存時検証)。

        `create_task_run` と同じ `_resolve_selected_sources` を通す。調度は保存時点の設定を凍結
        するので、「保存できたのに最初の発火で必ず落ちる」状態を作らないための事前確認が要る。
        検証専用の別実装を書くと、本番経路と判定がずれた瞬間に無意味になる。
        """

        manifest = resolved.skill_snapshot.get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError("Resolved task is missing its frozen RuntimeManifest")
        blueprint = resolve_capability_blueprint(manifest)
        if blueprint is None:
            raise ValueError("Resolved task is missing its CapabilityBlueprint")
        async with self._session_factory() as session:
            await _resolve_selected_sources(
                resolved,
                sources,
                blueprint=blueprint,
                project_id=project_id,
                integration_repository=IntegrationRepository(session),
                document_repository=DocumentRepository(session),
            )

    async def get_run(self, run_id: UUID) -> CreatedRun:
        """SSE 開始前の存在確認に使用する Run snapshot を取得する。"""

        async with self._session_factory() as session:
            return await RunRepository(session).get(run_id)

    async def cancel_run(self, run_id: UUID, *, trace_id: str | None) -> CancelledRun:
        """取消 intent または即時 CANCELLED 終態を一 transaction で記録する。"""

        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).request_cancellation(run_id, trace_id=trace_id)

    async def is_cancellation_requested(self, run_id: UUID) -> bool:
        """Worker の取消監視用に durable intent の有無を返す。"""

        async with self._session_factory() as session:
            return await RunRepository(session).is_cancellation_requested(run_id)

    async def get_run_detail(self, *, project_id: UUID, run_id: UUID) -> RunDetail:
        """Project ownership を強制して Result、ToolCall、Evidence を取得する。"""

        async with self._session_factory() as session:
            return await RunRepository(session).get_detail(
                project_id=project_id,
                run_id=run_id,
            )

    async def list_run_history(
        self,
        *,
        project_id: UUID,
        limit: int,
        offset: int,
        statuses: tuple[RunStatus, ...] = (),
    ) -> RunHistoryPage:
        """Project 内の Run history page を read-only transaction で取得する。"""

        async with self._session_factory() as session:
            return await RunRepository(session).list_history(
                project_id=project_id,
                limit=limit,
                offset=offset,
                statuses=statuses,
            )

    async def latest_run_by_task(self, *, project_id: UUID) -> dict[UUID, TaskLastRun]:
        """Project 内の各 task の最新 Run を read-only transaction で取得する。"""

        async with self._session_factory() as session:
            return await RunRepository(session).latest_run_by_task(project_id=project_id)

    async def list_events(self, run_id: UUID, *, after: int) -> list[StoredRunEvent]:
        """指定 sequence より後の RunEvent を取得する。"""

        async with self._session_factory() as session:
            return await RunRepository(session).list_events(run_id, after=after)

    async def transition_run(
        self,
        run_id: UUID,
        *,
        target: RunStatus,
        expected_row_version: int,
        error_json: dict[str, Any] | None = None,
        run_attempt_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> CreatedRun:
        """Worker から利用する optimistic state transition を実行する。"""

        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).transition(
                run_id,
                target=target,
                expected_row_version=expected_row_version,
                error_json=error_json,
                run_attempt_id=run_attempt_id,
                trace_id=trace_id,
            )

    async def claim_run(
        self,
        run_id: UUID,
        *,
        worker_id: str,
        lease_seconds: int,
        max_attempts: int,
        trace_id: str | None = None,
    ) -> ClaimedRun | None:
        """Worker 用 lease token を発行し、RunAttempt と PREPARING 状態を原子的に作成する。"""

        lease_token = secrets.token_urlsafe(32)
        lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).claim_for_execution(
                run_id,
                worker_id=worker_id,
                lease_token=lease_token,
                lease_token_hash=lease_token_hash(lease_token),
                lease_expires_at=lease_expires_at,
                max_attempts=max_attempts,
                trace_id=trace_id,
            )

    async def heartbeat_run_attempt(
        self,
        attempt_id: UUID,
        *,
        lease_token: str,
        lease_seconds: int,
    ) -> None:
        """Lease token を検証し、RunAttempt heartbeat と期限を延長する。"""

        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            await RunRepository(session).heartbeat_attempt(
                attempt_id,
                lease_token_hash=lease_token_hash(lease_token),
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                now=now,
            )

    async def prepare_execution(self, claimed: ClaimedRun) -> PreparedExecution:
        """Claim 済み Attempt を RUNNING へ進め、Agent event の開始 sequence を返す。"""

        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).prepare_execution(claimed)

    async def append_agent_event(
        self,
        claimed: ClaimedRun,
        event: AgentEvent,
        *,
        session_metadata: AgentSessionMetadata,
    ) -> None:
        """一つの永続対象 AgentEvent と通知 Outbox を commit する。"""

        async with self._session_factory() as session, session.begin():
            await RunRepository(session).append_agent_event(
                claimed,
                event,
                session_metadata=session_metadata,
            )

    async def verify_execution_start(self, claimed: ClaimedRun) -> bool:
        """外部 I/O を含めず、現在の実行権と durable cancel の開始 gate を commit する。"""

        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).verify_execution_start(claimed)

    async def freeze_agent_task_brief(
        self,
        claimed: ClaimedRun,
        *,
        brief: dict[str, Any],
        checksum: str,
    ) -> None:
        """現在 Segment の Brief snapshot を active lease 下で一度だけ保存する。"""

        async with self._session_factory() as session, session.begin():
            await RunRepository(session).freeze_agent_task_brief(
                claimed,
                brief=brief,
                checksum=checksum,
            )

    async def suspend_for_interaction(
        self,
        claimed: ClaimedRun,
        *,
        event: AgentEvent,
        session_metadata: AgentSessionMetadata,
        interaction: InteractionRequestDraft,
    ) -> UUID:
        """Agent の公開質問を保存し、Run/Attempt lease を待機状態へ確定する。"""

        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).suspend_for_interaction(
                claimed,
                event=event,
                session_metadata=session_metadata,
                request=interaction,
            )

    async def suspend_for_proposal(
        self,
        claimed: ClaimedRun,
        *,
        event: AgentEvent,
        session_metadata: AgentSessionMetadata,
        proposal: ChangeProposalDraft,
    ) -> UUID:
        """Agent の Proposal candidate を検証し approval/effect 待機へ確定する。"""

        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).suspend_for_proposal(
                claimed,
                event=event,
                session_metadata=session_metadata,
                draft=proposal,
            )

    async def respond_to_interaction(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        interaction_id: UUID,
        access: UserAccess,
        interaction_version: int,
        response_json: dict[str, Any],
        idempotency_key: str,
        trace_id: str | None,
    ) -> RespondedInteraction:
        """原会話・現在所属を同 transaction で固定し、認可済み回答だけを dispatch する。"""

        validate_user_access(access)
        expired: InteractionExpiredError | None = None
        result: RespondedInteraction | None = None
        async with self._session_factory() as session, session.begin():
            users = await UserRepository(session).lock_users(
                access=access,
                target_id=None,
                include_target_sessions=False,
            )
            authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=True)
            projects = ProjectRepository(session)
            project = await projects.lock_write_access(user=users.actor, project_id=project_id)
            authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=True)
            projects.require_active_write_access(project)
            repository = RunRepository(session)
            locked = await repository.lock_interaction_response(
                project_id=project_id,
                run_id=run_id,
                interaction_id=interaction_id,
            )
            # Run 等の待機中に原会話が期限を迎えても、回答・expiry の書き込み前に止める。
            authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=True)
            projects.require_active_write_access(project)
            try:
                result = await repository.respond_to_locked_interaction(
                    locked=locked,
                    actor_id=users.actor.id,
                    interaction_version=interaction_version,
                    response_json=response_json,
                    idempotency_key=idempotency_key,
                    trace_id=trace_id,
                )
            except InteractionExpiredError as error:
                # Expiry event と timeout continuation を rollback しないため、Proposal と
                # 同様に transaction 内で捕捉し、commit 後に API 用例外を返す。
                expired = error
            # Replay と捕捉済み 410 も同じ gate を通す。失効時は expiry/outbox ごと rollback する。
            await session.flush()
            authorize_user_access(access, users, now=datetime.now(UTC), admin=False, write=True)
            projects.require_active_write_access(project)
        if expired is not None:
            raise expired
        if result is None:
            raise RuntimeError("Interaction response produced no result")
        return result

    async def decide_change_proposal(
        self, command: DecideProposalCommand
    ) -> ProposalDecisionResult:
        """Proposal の user decision を apply dispatch または Run continuation へ反映する。"""

        expired: ChangeProposalExpiredError | None = None
        result: ProposalDecisionResult | None = None
        async with self._session_factory() as session, session.begin():
            try:
                result = await RunRepository(session).decide_change_proposal(command)
            except ChangeProposalExpiredError as error:
                # Repository は expiry の監査 event と次 Segment を同 transaction に保存する。
                # 例外を transaction 内で捕捉して commit 後に再送出し、409 応答のために
                # 状態変更まで rollback されることを防ぐ。
                expired = error
        if expired is not None:
            raise expired
        if result is None:
            raise RuntimeError("ChangeProposal decision produced no result")
        return result

    async def finalize_execution(
        self,
        claimed: ClaimedRun,
        *,
        target: RunStatus,
        attempt_status: RunAttemptStatus,
        event: AgentEvent | None,
        session_metadata: AgentSessionMetadata | None,
        result: RunResultRecord | None,
        error_json: dict[str, Any] | None,
    ) -> RunStatus:
        """Agent terminal outcome と Result を Run aggregate へ原子的に反映する。"""

        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).finalize_execution(
                claimed,
                target=target,
                attempt_status=attempt_status,
                event=event,
                session_metadata=session_metadata,
                result=result,
                error_json=error_json,
            )

    async def recover_expired_attempts(self, *, limit: int = 20) -> int:
        """期限切れ lease を RETRY_PENDING と再 dispatch Outbox へ変換する。"""

        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).recover_expired_attempts(
                now=datetime.now(UTC), limit=limit
            )

    async def recover_expired_interactions(self, *, limit: int = 20) -> int:
        """通常 interaction の独立期限を閉じ、安全な continuation を保存する。"""

        async with self._session_factory() as session, session.begin():
            return await RunRepository(session).recover_expired_interactions(
                now=datetime.now(UTC), limit=limit
            )


def _require_creation_namespace(
    idempotency_key: str, authorization: UserAccess | RunCreationParticipant
) -> None:
    """調度 key の新規作成は持久認領を必須とし、既存要求の確認は妨げない。"""

    if idempotency_key.startswith("schedule:") and isinstance(authorization, UserAccess):
        raise IdempotencyConflictError("Schedule keys are reserved for scheduled Run creation")


async def _complete_creation(
    authorization: UserAccess | RunCreationParticipant, session: AsyncSession, created: CreatedRun
) -> None:
    """すべての作成/再送 return に同じ transaction 内の結算を適用する。"""

    if not isinstance(authorization, UserAccess):
        await authorization.complete(session, created)


def _task_run_intent(
    *,
    project_id: UUID,
    skill_version_id: UUID,
    task_key: str,
    input_json: dict[str, Any],
    sources: dict[str, str],
    actor_id: UUID,
) -> TaskRunIntent:
    """選択構文の失敗を公開 Problem へ変換できる domain error に統一する。"""

    try:
        return TaskRunIntent(
            project_id=project_id,
            skill_version_id=skill_version_id,
            task_key=task_key,
            input_json=input_json,
            sources=sources,
            actor_id=actor_id,
        )
    except ValueError as error:
        raise TaskSourceSelectionError(str(error)) from error


async def _resolve_selected_sources(
    resolved: ResolvedTaskRun,
    sources: dict[str, str],
    *,
    blueprint: dict[str, Any],
    project_id: UUID,
    integration_repository: IntegrationRepository,
    document_repository: DocumentRepository | None = None,
) -> tuple[dict[str, Any], tuple[ResolvedRunBinding, ...]]:
    """三層 binding と Run override を解決し、provider/revision/scope を凍結準備する。

    資源要求の唯一の宣言元は blueprint の `resource_requirements` である。以前は manifest の
    `data_sources` にも同じ要求が並び、key がずれると画面に出ない要求で Run が失敗していた。
    `sources` の `integration:<uuid>` は Run preflight override、未指定時は Task → Project default
    の順に持続 binding を採用する。既存 fixture provider 名は歴史的 M0 受入経路に限り残す。
    `accepted_providers` は Integration では排序 hint とし、kind/capability/scope を硬境界にする。
    """

    provided = dict(sources)
    selected: dict[str, Any] = {}
    bindings: list[ResolvedRunBinding] = []
    blueprint_requirements = {
        str(item["key"]): item
        for item in _object_list(blueprint.get("resource_requirements"))
        if isinstance(item.get("key"), str)
    }
    task_scope_key = f"{resolved.skill_version_id}:{resolved.task_key}"
    for key, blueprint_requirement in blueprint_requirements.items():
        required = bool(blueprint_requirement.get("required", False))
        selected_token = provided.pop(key, None)
        configured = None
        if selected_token is None:
            configured = await integration_repository.find_configured_binding(
                project_id=project_id,
                task_scope_key=task_scope_key,
                requirement_key=key,
            )
            if configured is not None and configured.integration_id is not None:
                selected_token = f"integration:{configured.integration_id}"
        if selected_token is None:
            if required:
                raise TaskSourceSelectionError(f"Required data source has no binding: {key}")
            continue
        declared = tuple(
            item for item in blueprint_requirement.get("capabilities", []) if isinstance(item, str)
        )
        access = str(blueprint_requirement.get("access") or "read")
        if blueprint_requirement.get("kind") == "document":
            if (
                document_repository is None
                or access != "read"
                or DOCUMENT_READ_CAPABILITY not in declared
            ):
                raise TaskSourceSelectionError(f"Document read binding is not supported: {key}")
            try:
                selected[key] = await resolve_document_binding(
                    document_repository,
                    project_id=project_id,
                    requirement_key=key,
                    token=selected_token,
                )
            except DocumentSnapshotError as error:
                raise TaskSourceSelectionError(str(error)) from error
            continue
        # 要求が宣言した capability のうち access に対応するものが、この要求の観測 capability。
        observe_capability = _declared_capability(declared, access=access)
        if not selected_token.startswith("integration:"):
            accepted = tuple(
                item
                for item in blueprint_requirement.get("accepted_providers", [])
                if isinstance(item, str)
            )
            if selected_token not in accepted:
                raise TaskSourceSelectionError(
                    f"Legacy Provider is not accepted for {key}: {selected_token}"
                )
            if observe_capability is None:
                raise TaskSourceSelectionError(
                    f"Resource requirement declares no capability for {access}: {key}"
                )
            selected[key] = {
                "capability": observe_capability,
                "provider": selected_token,
                "binding_level": "LEGACY_FIXTURE",
            }
            continue
        try:
            integration_id = UUID(selected_token.removeprefix("integration:"))
        except ValueError as error:
            raise TaskSourceSelectionError(
                f"Integration candidate key is invalid: {key}"
            ) from error
        try:
            integration = await integration_repository.get_integration(
                project_id=project_id,
                integration_id=integration_id,
            )
        except LookupError as error:
            raise TaskSourceSelectionError(f"Integration is not available for {key}") from error
        if integration.status is not IntegrationStatus.ACTIVE:
            raise TaskSourceSelectionError(f"Integration is disabled for {key}")
        kind = str(blueprint_requirement.get("kind") or "")
        if integration.kind != kind:
            raise TaskSourceSelectionError(
                f"Integration kind does not satisfy resource requirement: {key}"
            )
        binding_capability = _select_binding_capability(
            integration.capabilities,
            hints=declared,
            access=access,
            observe_capability=observe_capability,
        )
        if observe_capability is not None and observe_capability not in integration.capabilities:
            raise TaskSourceSelectionError(f"Integration lacks the observe capability for {key}")
        scope = dict(configured.scope) if configured is not None else dict(integration.scope)
        if configured is not None and (
            configured.integration_id != integration.integration_id
            or configured.revision != str(integration.revision)
            or configured.capability_version != binding_capability
        ):
            raise TaskSourceSelectionError(f"Configured ResourceBinding is stale for {key}")
        bindings.append(
            ResolvedRunBinding(
                requirement_key=key,
                resource_kind=kind,
                integration=integration,
                capability_version=binding_capability,
                scope=scope,
                source_binding_id=configured.binding_id if configured is not None else None,
            )
        )
        selected[key] = {
            "capability": observe_capability or binding_capability,
            "provider": integration.provider,
            "candidate_key": selected_token,
            "integration_id": str(integration.integration_id),
            "revision": str(integration.revision),
            "scope": scope,
            "resource_kind": kind,
            "access": access,
            "source_binding_id": (str(configured.binding_id) if configured is not None else None),
        }
    if provided:
        raise TaskSourceSelectionError(f"Unknown data source keys: {sorted(provided)}")
    return selected, tuple(bindings)


def _select_binding_capability(
    capabilities: tuple[str, ...],
    *,
    hints: tuple[str, ...],
    access: str,
    observe_capability: str | None,
) -> str:
    """Resource access と hint を満たす Integration capability を決定的に選ぶ。"""

    candidates = [item for item in hints if item in capabilities]
    if access == "write":
        candidates = [item for item in candidates if is_write_capability(item)]
    else:
        candidates = [item for item in candidates if not is_write_capability(item)]
    if candidates:
        return sorted(candidates)[0]
    if observe_capability is not None and observe_capability in capabilities and access != "write":
        return observe_capability
    fallback = [item for item in capabilities if is_write_capability(item) == (access == "write")]
    if not fallback:
        raise TaskSourceSelectionError("Integration has no capability for required access")
    return sorted(fallback)[0]


def _declared_capability(capabilities: tuple[str, ...], *, access: str) -> str | None:
    """要求が宣言した capability から access に対応する一つを決定的に選ぶ。

    以前は manifest の `data_sources[].capability` が単一値を持っていたが、要求宣言を
    blueprint へ一本化したため、宣言集合と access からここで導出する。同じ access に複数
    宣言があるときは辞書順先頭に固定し、Run ごとに揺れないようにする。
    """

    wants_write = access == "write"
    matching = sorted(
        capability for capability in capabilities if is_write_capability(capability) == wants_write
    )
    return matching[0] if matching else None


def _object_list(value: Any) -> list[dict[str, Any]]:
    """Blueprint array から object item だけを返す。"""

    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]
