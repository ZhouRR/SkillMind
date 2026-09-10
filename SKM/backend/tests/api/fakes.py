"""Business API contract test で共有する in-memory fake service を提供する。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from skillmind.auth.service import (
    AuthenticatedActor,
    CsrfRejectedError,
    InvalidCredentialsError,
    LoginResult,
    SessionResult,
    UnauthorizedSessionError,
)
from skillmind.compositions import (
    ModuleNotFoundError,
    ModuleSkillBinding,
    ModuleSkillInvalidError,
    StoredModule,
)
from skillmind.core.secret_crypto import SecretCryptoError
from skillmind.documents import (
    DocumentConflictError,
    DocumentNotFoundError,
    StoredDocument,
)
from skillmind.documents.domain import (
    DocumentUploadAlreadyPublishedError,
    DocumentUploadClosedError,
    DocumentUploadClosureNotFoundError,
    DocumentUploadKeyConflictError,
    DocumentUploadNotFoundError,
    DocumentUploadPendingError,
    StoredDocumentUpload,
    StoredDocumentUploadClosure,
)
from skillmind.effects import (
    ApprovalDecision,
    ApprovalSource,
    ChangeProposalStatus,
    CreatePreauthorizationCommand,
    DecideProposalCommand,
    EffectRiskLevel,
    PreauthorizationStatus,
    ProposalDecisionResult,
    StoredChangeApproval,
    StoredChangeProposal,
    StoredEffectPreauthorization,
)
from skillmind.evaluations import (
    CreateEvaluationCommand,
    InvalidEvaluationRevisionError,
    StoredEvaluation,
    StoredEvaluationRevision,
)
from skillmind.evaluations.domain import (
    EvaluationResultMismatchError,
    EvaluationSubmissionConflictError,
    EvaluationSubmissionNotFoundError,
    InvalidEvaluationCursorError,
    StoredEvaluationPage,
    StoredEvaluationSubmission,
    evaluation_request_hash,
)
from skillmind.integrations import (
    CreateIntegrationCommand,
    CreateSecretReferenceCommand,
    IntegrationStatus,
    PutResourceBindingCommand,
    StoredIntegration,
    StoredResourceBinding,
    StoredSecretReference,
)
from skillmind.projects import (
    ProjectDeleteBlockedError,
    ProjectMemberStatus,
    ProjectNotFoundError,
    ProjectPermissionDeniedError,
    ProjectStatus,
    StoredProject,
    StoredProjectMember,
)
from skillmind.projects.domain import require_project_version
from skillmind.runs.creation_participation import RunCreationParticipant
from skillmind.runs.domain import (
    CancelledRun,
    CreatedRun,
    IdempotencyConflictError,
    RespondedInteraction,
    RunCancellationState,
    RunDetail,
    RunHistoryItem,
    RunHistoryPage,
    RunNotCancellableError,
    RunNotFoundError,
    RunSegmentStatus,
    RunSegmentTrigger,
    RunStatus,
    SessionContinuationMode,
    StoredEvidence,
    StoredRunResult,
    StoredRunSegment,
    StoredToolCall,
    TaskLastRun,
    TaskSourceSelectionError,
    derive_task_id,
)
from skillmind.schedules import (
    InvalidScheduleTransitionError,
    ScheduleConflictError,
    ScheduleDefinition,
    ScheduleInvalidError,
    ScheduleKind,
    ScheduleNotFoundError,
    SchedulePage,
    ScheduleRecord,
    ScheduleStatus,
)
from skillmind.skills import (
    InlineSkillFile,
    InterpretationLaunch,
    ManifestGateFinding,
    PublishedTaskDescriptor,
    PublishedTaskNotFoundError,
    ResolvedTaskRun,
    SkillInterpretationNotFoundError,
    SkillInterpretationNotReadyError,
    SkillInterpretationStatus,
    SkillInterpreterUnavailableError,
    SkillPreview,
    SkillPublishGateError,
    SkillSourceIntegrityError,
    SkillSourceNotFoundError,
    SkillStorageUnavailableError,
    SkillVersionDeleteBlockedError,
    SkillVersionStatus,
    StoredInterpretationExecution,
    StoredProjectSkillVersion,
    StoredSkillPreview,
    StoredSkillVersion,
    TaskInputInvalidError,
    TaskToolRequirement,
    UploadSkillFile,
)
from skillmind.skills.resource_binding import (
    RequirementBinding,
    RequirementStatus,
    TaskReadiness,
    TaskReadinessLevel,
)
from skillmind.storage import UploadRejectedError
from skillmind.users.domain import (
    StoredUser,
    StoredUserSecurityEvent,
    UserAccess,
    UserMutationResult,
    UserRole,
    UserSecurityAction,
    UserStatus,
)


class FakeAuthService:
    """認証 route contract を DB/Redis なしで検証する fake。"""

    def __init__(self, *, invalid: bool = False, unauthorized: bool = False) -> None:
        """Login failure または session failure scenario を選択する。"""

        self.invalid = invalid
        self.unauthorized = unauthorized
        self.login_csrf = "login-csrf-token"
        self.session_token = "session-token"
        self.csrf_token = "session-csrf-token"
        self.logged_out = False
        self.login_sources: list[str] = []
        self.password_change_actors: list[UUID] = []
        self.actor = AuthenticatedActor(
            user_id=uuid4(),
            organization_id=uuid4(),
            email="admin@example.com",
            display_name="Admin",
            system_role="ADMIN",
        )
        self.expires_at = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)

    async def begin_login(self, client_address: str) -> object:
        """Body 検証前の来源確認を記録する。Redis の実 rate 判断は模倣しない。"""

        self.login_sources.append(client_address)
        return object()

    async def issue_login_csrf(self, admission: object) -> str:
        """固定 login challenge を返す。"""

        return self.login_csrf

    async def admit_password_change(self, *, actor: AuthenticatedActor, admission: object) -> None:
        """改密が現在 actor で配額へ接続することを記録し、実 Redis と区別する。"""

        self.password_change_actors.append(actor.user_id)

    async def login(self, **kwargs: object) -> LoginResult:
        """受信 CSRF と credential を検証し、固定 session を返す。"""

        assert kwargs["login_csrf_header"] == self.login_csrf
        assert kwargs["login_csrf_cookie"] == self.login_csrf
        if self.invalid:
            raise InvalidCredentialsError("hidden")
        return LoginResult(
            actor=self.actor,
            session_token=self.session_token,
            csrf_token=self.csrf_token,
            absolute_expires_at=self.expires_at,
        )

    async def get_session(self, session_token: str) -> SessionResult:
        """Cookie token が一致する場合だけ current session を返す。"""

        if self.unauthorized or session_token != self.session_token:
            raise UnauthorizedSessionError("hidden")
        return SessionResult(
            actor=self.actor,
            csrf_token=self.csrf_token,
            absolute_expires_at=self.expires_at,
        )

    async def logout(self, *, session_token: str, csrf_token: str) -> None:
        """Session/CSRF token を確認して logout 呼び出しを記録する。"""

        assert session_token == self.session_token
        assert csrf_token == self.csrf_token
        self.logged_out = True

    async def authenticate_session(self, session_token: str) -> AuthenticatedActor:
        """Project read route 用に固定 actor を返す。"""

        del session_token
        if self.unauthorized:
            raise UnauthorizedSessionError("hidden")
        return self.actor

    async def authenticate_unsafe_session(
        self,
        *,
        session_token: str,
        csrf_token: str,
    ) -> AuthenticatedActor:
        """Project mutation route 用に CSRF 検証済み actor を返す。"""

        del session_token
        if self.unauthorized:
            raise UnauthorizedSessionError("hidden")
        if csrf_token != self.csrf_token:
            raise CsrfRejectedError("hidden")
        return self.actor


class FakeUserService:
    """API の授権・明示投影だけを検査する。DB transaction を模倣しない。"""

    def __init__(self, actor: AuthenticatedActor) -> None:
        """本番ユーザーを使わず、固定の公開可能 DTO を用意する。"""

        now = datetime(2026, 9, 9, 12, tzinfo=UTC)
        self.account = StoredUser(
            actor.user_id,
            actor.email,
            actor.display_name,
            UserRole(actor.system_role),
            UserStatus.ACTIVE,
            1,
            now,
            now,
        )
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failure: Exception | None = None
        self.revoked = False

    def _record(self, operation: str, kwargs: dict[str, object]) -> UserAccess:
        """原 credential と server 相関 ID を use case へ渡したことを確認する。"""

        self.calls.append((operation, kwargs))
        if self.failure:
            raise self.failure
        access = kwargs["access"]
        assert isinstance(access, UserAccess)
        return access

    async def get_account(self, **kwargs: object) -> StoredUser:
        """本人 account の固定投影を返す。"""

        self._record("account", kwargs)
        return self.account

    async def get_user(self, **kwargs: object) -> StoredUser:
        """精確 ID の読取が共通の管理 service へ到達した事実を残す。"""

        self._record("get_user", kwargs)
        return replace(self.account, user_id=cast(UUID, kwargs["user_id"]))

    async def list_users(self, **kwargs: object) -> tuple[tuple[StoredUser, ...], int]:
        """先頭 page だけではない総件数を返す。"""

        self._record("list", kwargs)
        return (self.account,), 151

    async def list_security_events(
        self, **kwargs: object
    ) -> tuple[tuple[StoredUserSecurityEvent, ...], int]:
        """架空の操作事実を、request ID とともに公開する。"""

        access = self._record("events", kwargs)
        return (
            StoredUserSecurityEvent(
                uuid4(),
                cast(UUID, kwargs["user_id"]),
                access.actor.user_id,
                UserSecurityAction.SESSIONS_REVOKED,
                2,
                UserRole.USER,
                UserStatus.ACTIVE,
                UserRole.USER,
                UserStatus.ACTIVE,
                1,
                access.request_id,
                self.account.created_at,
            ),
        ), 1

    def _result(self, operation: str, kwargs: dict[str, object]) -> UserMutationResult:
        """HTTP は原入力を転送するだけであることを記録する。"""

        self._record(operation, kwargs)
        return UserMutationResult(
            replace(self.account, row_version=2), 1 if self.revoked else 0, self.revoked
        )

    async def create_user(self, **kwargs: object) -> UserMutationResult:
        """新規作成の use case 呼出しを記録する。"""

        return self._result("create", kwargs)

    async def update_user(self, **kwargs: object) -> UserMutationResult:
        """版付き更新の use case 呼出しを記録する。"""

        return self._result("update", kwargs)

    async def revoke_sessions(self, **kwargs: object) -> UserMutationResult:
        """失効要求の対象 ID と版を記録する。"""

        return self._result("revoke", kwargs)

    async def change_password(self, **kwargs: object) -> UserMutationResult:
        """改密の use case 呼出しを記録する。"""

        return self._result("password", kwargs)


class FakeProjectService:
    """Project API contract を database なしで検証する in-memory service。"""

    def __init__(self) -> None:
        """空の Project と membership 集合を初期化する。"""

        self.projects: dict[UUID, StoredProject] = {}
        self.members: dict[UUID, list[StoredProjectMember]] = {}

    async def list_projects(
        self,
        *,
        actor: AuthenticatedActor,
        include_archived: bool,
    ) -> tuple[StoredProject, ...]:
        """ADMIN test actor に保存済み Project を返す。"""

        del actor
        return tuple(
            project
            for project in self.projects.values()
            if include_archived or project.status is ProjectStatus.ACTIVE
        )

    async def create_project(
        self,
        *,
        access: UserAccess,
        key: str,
        name: str,
        description: str,
        settings: dict[str, object],
        retention_days: int,
    ) -> StoredProject:
        """ADMIN の入力から固定 Project read model を作成する。"""

        if access.actor.system_role != "ADMIN":
            raise ProjectPermissionDeniedError("denied")
        now = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
        project = StoredProject(
            project_id=uuid4(),
            key=key,
            name=name,
            description=description,
            status=ProjectStatus.ACTIVE,
            settings=settings,
            retention_days=retention_days,
            row_version=1,
            created_at=now,
            updated_at=now,
        )
        self.projects[project.project_id] = project
        return project

    async def archive_project(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
        expected_row_version: int,
    ) -> StoredProject:
        """保存済み Project を ARCHIVED read model へ置換する。"""

        if access.actor.system_role != "ADMIN":
            raise ProjectPermissionDeniedError("denied")
        project = self.projects[project_id]
        require_project_version(project.row_version, expected_row_version)
        archived = StoredProject(
            project_id=project.project_id,
            key=project.key,
            name=project.name,
            description=project.description,
            status=ProjectStatus.ARCHIVED,
            row_version=project.row_version + (project.status != ProjectStatus.ARCHIVED),
            settings=project.settings,
            retention_days=project.retention_days,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )
        self.projects[project_id] = archived
        return archived

    async def unarchive_project(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
        expected_row_version: int,
    ) -> StoredProject:
        """保存済み Project を ACTIVE read model へ戻す。"""

        if access.actor.system_role != "ADMIN":
            raise ProjectPermissionDeniedError("denied")
        project = self.projects[project_id]
        require_project_version(project.row_version, expected_row_version)
        restored = StoredProject(
            project_id=project.project_id,
            key=project.key,
            name=project.name,
            description=project.description,
            status=ProjectStatus.ACTIVE,
            row_version=project.row_version + (project.status != ProjectStatus.ACTIVE),
            settings=project.settings,
            retention_days=project.retention_days,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )
        self.projects[project_id] = restored
        return restored

    async def delete_project(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
        expected_row_version: int,
    ) -> None:
        """ARCHIVED Project だけを削除し、それ以外は route の 409 経路を再現する。"""

        if access.actor.system_role != "ADMIN":
            raise ProjectPermissionDeniedError("denied")
        project = self.projects[project_id]
        require_project_version(project.row_version, expected_row_version)
        if project.status is not ProjectStatus.ARCHIVED:
            raise ProjectDeleteBlockedError(
                "Project must be archived before deletion",
                blockers=("project_not_archived",),
            )
        del self.projects[project_id]

    async def add_member(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
        user_id: UUID,
    ) -> StoredProjectMember:
        """Project に固定 ACTIVE membership を追加する。"""

        if access.actor.system_role != "ADMIN":
            raise ProjectPermissionDeniedError("denied")
        member = StoredProjectMember(
            user_id=user_id,
            email="user@example.com",
            display_name="Project User",
            status=ProjectMemberStatus.ACTIVE,
            joined_at=datetime(2026, 7, 4, 12, 30, tzinfo=UTC),
        )
        self.members.setdefault(project_id, []).append(member)
        return member


class FakeProjectAuthorizationService:
    """Business route test で任意 Project を同一 Organization として許可する fake。"""

    async def get_project(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
    ) -> StoredProject:
        """Path の Project ID を ACTIVE read model として返す。"""

        now = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
        return StoredProject(
            project_id=project_id,
            key=f"test-{str(project_id)[:8]}",
            name="Authorized Project",
            description="",
            status=ProjectStatus.ACTIVE,
            settings={"organization_id": str(actor.organization_id)},
            retention_days=90,
            row_version=1,
            created_at=now,
            updated_at=now,
        )


class DeniedProjectAuthorizationService:
    """Resource existence を公開せず Project access を拒否する fake。"""

    async def get_project(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
    ) -> StoredProject:
        """Actor と Project の組合せを一律 not found にする。"""

        del actor
        raise ProjectNotFoundError(f"Project not found: {project_id}")


class FakeIntegrationService:
    """Integration 管理 API の公開 allowlist を検証する in-memory service。"""

    def __init__(self) -> None:
        """空の Secret、Integration と Binding 集合を初期化する。"""

        self.secrets: list[StoredSecretReference] = []
        self.integrations: list[StoredIntegration] = []
        self.bindings: list[StoredResourceBinding] = []
        self.received_locator: str | None = None
        self.received_secret_value: str | None = None
        self.received_config: dict[str, object] | None = None
        # True にすると KEK 未配線の配備を模し、MANAGED 作成が fail closed する。
        self.managed_secret_unavailable = False

    async def create_secret_reference(
        self, command: CreateSecretReferenceCommand
    ) -> StoredSecretReference:
        """Locator/明文の受領を記録し、公開 model には含めず保存する。"""

        if self.managed_secret_unavailable:
            raise SecretCryptoError("Managed secret storage is not configured")
        now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
        self.received_locator = command.locator
        self.received_secret_value = command.secret_value
        stored = StoredSecretReference(
            secret_reference_id=uuid4(),
            project_id=command.project_id,
            name=command.name,
            provider=command.provider,
            resolver=command.resolver,
            key_version=command.key_version,
            status=IntegrationStatus.ACTIVE,
            created_by=command.created_by,
            created_at=now,
            updated_at=now,
            disabled_at=None,
        )
        self.secrets.append(stored)
        return stored

    async def list_secret_references(
        self, *, project_id: UUID
    ) -> tuple[StoredSecretReference, ...]:
        """Project 内の SecretReference metadata を返す。"""

        return tuple(item for item in self.secrets if item.project_id == project_id)

    async def disable_secret_reference(
        self, *, project_id: UUID, secret_reference_id: UUID
    ) -> StoredSecretReference:
        """対象 SecretReference を削除せず DISABLED へ置換する。"""

        index = next(
            index
            for index, item in enumerate(self.secrets)
            if item.project_id == project_id and item.secret_reference_id == secret_reference_id
        )
        now = datetime(2026, 7, 18, 12, 5, tzinfo=UTC)
        stored = replace(
            self.secrets[index],
            status=IntegrationStatus.DISABLED,
            disabled_at=now,
            updated_at=now,
        )
        self.secrets[index] = stored
        return stored

    async def create_integration(self, command: CreateIntegrationCommand) -> StoredIntegration:
        """Connection config key だけを公開 model へ投影する。"""

        now = datetime(2026, 7, 18, 12, 10, tzinfo=UTC)
        self.received_config = dict(command.config)
        stored = StoredIntegration(
            integration_id=uuid4(),
            project_id=command.project_id,
            name=command.name,
            kind=command.kind,
            provider=command.provider,
            status=IntegrationStatus.ACTIVE,
            revision=1,
            capabilities=command.capabilities,
            scope=dict(command.scope),
            config_keys=tuple(sorted(command.config)),
            secret_reference_id=command.secret_reference_id,
            created_by=command.created_by,
            created_at=now,
            updated_at=now,
            disabled_at=None,
        )
        self.integrations.append(stored)
        return stored

    async def list_integrations(self, *, project_id: UUID) -> tuple[StoredIntegration, ...]:
        """Project 内の接続本文を除いた Integration を返す。"""

        return tuple(item for item in self.integrations if item.project_id == project_id)

    async def disable_integration(
        self, *, project_id: UUID, integration_id: UUID, expected_revision: int
    ) -> StoredIntegration:
        """Expected revision と一致する Integration を無効化する。"""

        index = next(
            index
            for index, item in enumerate(self.integrations)
            if item.project_id == project_id and item.integration_id == integration_id
        )
        current = self.integrations[index]
        assert current.revision == expected_revision
        now = datetime(2026, 7, 18, 12, 15, tzinfo=UTC)
        stored = replace(
            current,
            status=IntegrationStatus.DISABLED,
            revision=current.revision + 1,
            disabled_at=now,
            updated_at=now,
        )
        self.integrations[index] = stored
        return stored

    async def put_resource_binding(
        self, command: PutResourceBindingCommand
    ) -> StoredResourceBinding:
        """Exact scope と capability を checksum 付き Binding として返す。"""

        integration = next(
            item
            for item in self.integrations
            if item.integration_id == command.integration_id
            and item.project_id == command.project_id
        )
        now = datetime(2026, 7, 18, 12, 20, tzinfo=UTC)
        stored = StoredResourceBinding(
            binding_id=uuid4(),
            project_id=command.project_id,
            scope_level=command.scope_level,
            scope_key=command.scope_key,
            requirement_key=command.requirement_key,
            resource_kind=command.resource_kind,
            integration_id=command.integration_id,
            run_id=None,
            source_binding_id=None,
            provider=integration.provider,
            capability_version=command.capability_version,
            revision=str(integration.revision),
            scope=dict(command.requested_scope),
            checksum="sha256:" + ("b" * 64),
            created_by=command.created_by,
            created_at=now,
            updated_at=now,
            disabled_at=None,
        )
        self.bindings.append(stored)
        return stored

    async def list_resource_bindings(
        self, *, project_id: UUID
    ) -> tuple[StoredResourceBinding, ...]:
        """Project の持続/Run Binding を返す。"""

        return tuple(item for item in self.bindings if item.project_id == project_id)


class FakeEffectService:
    """LOW-only preauthorization API を DB なしで検証する fake。"""

    def __init__(self) -> None:
        """空の policy 集合を初期化する。"""

        self.policies: list[StoredEffectPreauthorization] = []

    async def create_preauthorization(
        self, command: CreatePreauthorizationCommand
    ) -> StoredEffectPreauthorization:
        """Exact scope の ACTIVE policy を作成する。"""

        now = datetime(2026, 7, 18, 13, 0, tzinfo=UTC)
        stored = StoredEffectPreauthorization(
            preauthorization_id=uuid4(),
            project_id=command.project_id,
            integration_id=command.integration_id,
            capability_version=command.capability_version,
            operation=command.operation,
            max_risk_level=EffectRiskLevel.LOW,
            scope=dict(command.scope),
            status=PreauthorizationStatus.ACTIVE,
            policy_version=1,
            created_by=command.created_by,
            expires_at=command.expires_at,
            disabled_at=None,
            created_at=now,
            updated_at=now,
        )
        self.policies.append(stored)
        return stored

    async def list_preauthorizations(
        self, *, project_id: UUID
    ) -> tuple[StoredEffectPreauthorization, ...]:
        """Project の active/disabled policy を返す。"""

        return tuple(item for item in self.policies if item.project_id == project_id)

    async def disable_preauthorization(
        self,
        *,
        project_id: UUID,
        preauthorization_id: UUID,
        expected_policy_version: int,
    ) -> StoredEffectPreauthorization:
        """Optimistic version を確認して policy を無効化する。"""

        index = next(
            index
            for index, item in enumerate(self.policies)
            if item.project_id == project_id and item.preauthorization_id == preauthorization_id
        )
        current = self.policies[index]
        assert current.policy_version == expected_policy_version
        now = datetime(2026, 7, 18, 13, 5, tzinfo=UTC)
        stored = replace(
            current,
            status=PreauthorizationStatus.DISABLED,
            policy_version=current.policy_version + 1,
            disabled_at=now,
            updated_at=now,
        )
        self.policies[index] = stored
        return stored


class FakeProposalDecisionService:
    """Proposal decision route の command と公開 response を検証する fake。"""

    def __init__(self) -> None:
        """未受信状態で初期化する。"""

        self.received: DecideProposalCommand | None = None

    async def decide_change_proposal(
        self, command: DecideProposalCommand
    ) -> ProposalDecisionResult:
        """受信した exact version/checksum を固定 response として返す。"""

        self.received = command
        now = datetime(2026, 7, 18, 14, 0, tzinfo=UTC)
        proposal = StoredChangeProposal(
            proposal_id=command.proposal_id,
            proposal_ref="cp_api_001",
            project_id=command.project_id,
            run_id=command.run_id,
            run_segment_id=uuid4(),
            agent_session_id=uuid4(),
            target_binding_id=uuid4(),
            integration_id=uuid4(),
            effect_intent_key="update_issue",
            capability_version="issue.update/v1",
            operation="update_fields",
            target={"issue_id": "1001"},
            summary="Update reviewed issue fields.",
            changes=({"op": "SET", "field": "status_id", "value": 3},),
            precondition={"revision": "17"},
            evidence_refs=("ev_issue_before_001",),
            risk_level=EffectRiskLevel.LOW,
            reversible=True,
            rollback={"strategy": "restore_previous_fields"},
            verification={"mode": "read_back"},
            continuation_mode="FORK",
            checkpoint={"summary": "Issue observed."},
            checkpoint_checksum="sha256:" + ("c" * 64),
            idempotency_key="proposal-api-001",
            status=(
                ChangeProposalStatus.APPROVED
                if command.decision is ApprovalDecision.APPROVED
                else ChangeProposalStatus.REJECTED
            ),
            version=command.proposal_version,
            checksum=command.proposal_checksum,
            expires_at=datetime(2026, 7, 19, 14, 0, tzinfo=UTC),
            created_at=now,
            updated_at=now,
        )
        approval = StoredChangeApproval(
            approval_id=uuid4(),
            proposal_id=command.proposal_id,
            run_id=command.run_id,
            source=ApprovalSource.USER,
            decision=command.decision,
            actor_id=command.actor_id,
            preauthorization_id=None,
            proposal_version=command.proposal_version,
            proposal_checksum=command.proposal_checksum,
            reason=command.reason,
            created_at=now,
        )
        return ProposalDecisionResult(
            proposal=proposal,
            approval=approval,
            effect_execution=None,
            run_status=RunStatus.WAITING_FOR_APPROVAL.value,
            idempotent_replay=False,
        )


class FakeRunService:
    """API contract test で database を使わず Run use case を置き換える。"""

    def __init__(
        self, *, replay: bool = False, conflict: bool = False, source_invalid: bool = False
    ) -> None:
        """Replay または conflict の test scenario を設定する。"""

        self.replay = replay
        self.conflict = conflict
        self.source_invalid = source_invalid
        self.received_input: dict[str, object] | None = None
        self.received_actor_id: UUID | None = None
        self.received_actor_role: str | None = None
        self.received_membership: str | None = None
        self.received_sources: dict[str, str] | None = None
        self.created_run: CreatedRun | None = None
        self.received_interaction_response: dict[str, object] | None = None
        self.history_statuses: tuple[RunStatus, ...] = ()
        self.replay_lookups = 0

    async def find_task_run_replay(
        self,
        *,
        project_id: UUID,
        skill_version_id: UUID,
        task_key: str,
        input_json: dict[str, object],
        sources: dict[str, str],
        actor_id: UUID,
        idempotency_key: str,
        authorization: UserAccess | RunCreationParticipant,
    ) -> CreatedRun | None:
        """初回要求を先に確認し、再送では現在の task 解決を必要としない。"""

        self.replay_lookups += 1
        assert isinstance(authorization, UserAccess)
        assert authorization.actor.user_id == actor_id
        assert skill_version_id and task_key and idempotency_key
        if self.conflict:
            raise IdempotencyConflictError("different request")
        if not self.replay:
            return None
        self.received_input = input_json
        self.received_sources = sources
        self.received_actor_id = actor_id
        self.created_run = CreatedRun(
            run_id=uuid4(),
            project_id=project_id,
            task_id=uuid4(),
            status=RunStatus.QUEUED,
            row_version=1,
            created_at=datetime(2026, 7, 9, 12, 0, tzinfo=UTC),
            idempotent_replay=True,
        )
        return self.created_run

    async def create_task_run(
        self,
        *,
        project_id: UUID,
        resolved: ResolvedTaskRun,
        input_json: dict[str, object],
        sources: dict[str, str],
        idempotency_key: str,
        trace_id: str | None,
        actor_id: UUID,
        authorization: UserAccess | RunCreationParticipant,
    ) -> CreatedRun:
        """通用 Run を作成し、source 不正と conflict の scenario を再現する。"""

        if self.conflict:
            raise IdempotencyConflictError("different request")
        if self.source_invalid:
            raise TaskSourceSelectionError("provider not accepted")
        assert idempotency_key
        assert trace_id
        assert resolved.task_key
        assert isinstance(authorization, UserAccess)
        assert authorization.actor.user_id == actor_id
        self.received_input = input_json
        self.received_sources = sources
        self.received_actor_id = actor_id
        self.received_actor_role = authorization.actor.system_role
        self.received_membership = (
            "ADMIN_BYPASS" if authorization.actor.system_role == "ADMIN" else "ACTIVE"
        )
        self.created_run = CreatedRun(
            run_id=uuid4(),
            project_id=project_id,
            task_id=uuid4(),
            status=RunStatus.QUEUED,
            row_version=1,
            created_at=datetime(2026, 7, 9, 12, 0, tzinfo=UTC),
            idempotent_replay=self.replay,
        )
        return self.created_run

    async def get_run(self, run_id: UUID) -> CreatedRun:
        """作成済み Run を返し、異なる ID は not found とする。"""

        if self.created_run is None or self.created_run.run_id != run_id:
            raise RunNotFoundError(f"Run not found: {run_id}")
        return self.created_run

    async def cancel_run(self, run_id: UUID, *, trace_id: str | None) -> CancelledRun:
        """作成済み Run を取消し、終態 Run は production と同じ conflict にする。"""

        run = await self.get_run(run_id)
        assert trace_id
        if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
            raise RunNotCancellableError(f"Terminal Run cannot be cancelled: {run.status.value}")
        if run.status is not RunStatus.CANCELLED:
            run = CreatedRun(
                run_id=run.run_id,
                project_id=run.project_id,
                task_id=run.task_id,
                status=RunStatus.CANCELLED,
                row_version=run.row_version + 1,
                created_at=run.created_at,
                idempotent_replay=False,
            )
            self.created_run = run
        return CancelledRun(run=run, cancellation=RunCancellationState.CANCELLED)

    async def respond_to_interaction(
        self,
        *,
        project_id: UUID,
        run_id: UUID,
        interaction_id: UUID,
        access: UserAccess,
        interaction_version: int,
        response_json: dict[str, object],
        idempotency_key: str,
        trace_id: str | None,
    ) -> RespondedInteraction:
        """有効な Interaction 回答から次 Segment を作る API scenario を再現する。"""

        run = await self.get_run(run_id)
        if run.project_id != project_id:
            raise RunNotFoundError(f"Run not found in project: {run_id}")
        assert access.actor.user_id
        assert interaction_version == 1
        assert idempotency_key
        assert trace_id
        self.received_interaction_response = response_json
        next_run = CreatedRun(
            run_id=run.run_id,
            project_id=run.project_id,
            task_id=run.task_id,
            status=RunStatus.QUEUED,
            row_version=run.row_version + 1,
            created_at=run.created_at,
            idempotent_replay=False,
        )
        self.created_run = next_run
        return RespondedInteraction(
            run=next_run,
            interaction_id=interaction_id,
            response_id=uuid4(),
            run_segment_id=uuid4(),
            segment_no=2,
            continuation_mode=SessionContinuationMode.REPLACE,
            idempotent_replay=self.replay,
        )

    async def get_run_detail(self, *, project_id: UUID, run_id: UUID) -> RunDetail:
        """Project と Run が一致する固定 Result/Evidence read model を返す。"""

        run = await self.get_run(run_id)
        if run.project_id != project_id:
            raise RunNotFoundError(f"Run not found in project: {run_id}")
        now = datetime(2026, 7, 2, 13, 0, tzinfo=UTC)
        tool_call_id = uuid4()
        return RunDetail(
            run=run,
            input=self.received_input or {"target": "main"},
            selected_sources=self.received_sources
            or {"repository-source": {"capability": "repository.read/v1", "provider": "git"}},
            segments=(
                StoredRunSegment(
                    run_segment_id=uuid4(),
                    segment_no=1,
                    trigger_type=RunSegmentTrigger.INITIAL,
                    trigger_ref=None,
                    status=RunSegmentStatus.COMPLETED,
                    objective={"text": "Review the repository"},
                    checkpoint={},
                    continuation_mode=SessionContinuationMode.INITIAL,
                    parent_agent_session_id=None,
                    task_brief_checksum="sha256:" + ("2" * 64),
                    started_at=now,
                    finished_at=now,
                    created_at=now,
                ),
            ),
            result=StoredRunResult(
                result_id=uuid4(),
                output_schema="sha256:" + ("c" * 64),
                result_kind="OUTCOME_ENVELOPE",
                data={"summary": "Repository review completed", "evidence_refs": ["ev_repo_001"]},
                evidence_refs=("ev_repo_001",),
                artifact_refs=(),
                change_proposal_refs=(),
                optional_schema_identity={},
                summary="completed",
                confidence=0.8,
                needs_review=False,
                usage={"input_tokens": 10},
                cost={"total_cost_usd": 0.01},
                validation={"schema_valid": True},
                created_at=now,
            ),
            tool_calls=(
                StoredToolCall(
                    tool_call_id=tool_call_id,
                    run_attempt_id=uuid4(),
                    agent_session_id=uuid4(),
                    tool_name="repository_read_v1",
                    capability="repository.read/v1",
                    provider="git",
                    arguments_summary={"target": "main"},
                    status="SUCCEEDED",
                    duration_ms=12,
                    created_at=now,
                ),
            ),
            evidence=(
                StoredEvidence(
                    evidence_ref="ev_repo_001",
                    tool_call_id=tool_call_id,
                    evidence_type="repository-file",
                    source_uri="git://fixture/repository/main",
                    source_locator={"path": "main"},
                    content_hash="sha256:" + ("1" * 64),
                    snapshot_uri=None,
                    excerpt="sanitized excerpt",
                    metadata={},
                    created_at=now,
                ),
            ),
        )

    async def latest_run_by_task(self, *, project_id: UUID) -> dict[UUID, TaskLastRun]:
        """作成済み Run があればその task の最新 Run として返す。"""

        if self.created_run is None or self.created_run.project_id != project_id:
            return {}
        return {
            self.created_run.task_id: TaskLastRun(
                task_id=self.created_run.task_id,
                run_id=self.created_run.run_id,
                status=self.created_run.status,
                created_at=self.created_run.created_at,
                finished_at=None,
                result_summary=None,
            )
        }

    async def list_run_history(
        self,
        *,
        project_id: UUID,
        limit: int,
        offset: int,
        statuses: tuple[RunStatus, ...] = (),
    ) -> RunHistoryPage:
        """作成済み Run を一件だけ含む固定 history page を返す。絞り込み条件は記録する。"""

        self.history_statuses = statuses
        items: tuple[RunHistoryItem, ...] = ()
        if self.created_run is not None and self.created_run.project_id == project_id:
            items = (
                RunHistoryItem(
                    run=self.created_run,
                    input=self.received_input or {"target": "main"},
                    selected_sources=self.received_sources
                    or {
                        "repository-source": {
                            "capability": "repository.read/v1",
                            "provider": "git",
                        }
                    },
                    started_at=self.created_run.created_at,
                    finished_at=self.created_run.created_at,
                    result_summary="completed",
                    result_confidence=0.8,
                    result_needs_review=False,
                ),
            )
        return RunHistoryPage(items=items, limit=limit, offset=offset, has_more=False)


class FakeSkillService:
    """Skill persistence API を database なしで置き換える。"""

    def __init__(
        self,
        *,
        missing: bool = False,
        source_missing: bool = False,
        interpreter_unavailable: bool = False,
        not_ready: bool = False,
        published_task_missing: bool = False,
        task_input_invalid: bool = False,
        storage_unavailable: bool = False,
        source_integrity_failed: bool = False,
        launch_stored: bool = False,
        terminal: tuple[UUID, str, str | None] | None = None,
    ) -> None:
        """正常取得または各種 not found/unavailable scenario を設定する。"""

        self.missing = missing
        self.source_missing = source_missing
        self.interpreter_unavailable = interpreter_unavailable
        self.not_ready = not_ready
        self.published_task_missing = published_task_missing
        self.task_input_invalid = task_input_invalid
        self.storage_unavailable = storage_unavailable
        self.source_integrity_failed = source_integrity_failed
        # launch_stored=True は再利用/unsafe 相当の同期確定 launch を再現する。
        self.launch_stored = launch_stored
        self.terminal = terminal
        self.adjustment_instruction: str | None = None
        # catalog が返す task は instance 内で不変にする。Run 履歴との突き合わせ検証で使う。
        self._catalog_skill_version_id = uuid4()
        # 削除 API 用: 受理した version と、監査参照ありを模して 409 を返す version。
        self.deleted_version_ids: list[UUID] = []
        self.blocked_delete_version_id: UUID | None = None
        self.received_files: tuple[InlineSkillFile, ...] = ()
        self.received_uploads: tuple[UploadSkillFile, ...] = ()
        self.stored = StoredSkillPreview(
            skill_source_id=uuid4(),
            interpretation_id=uuid4(),
            organization_id=uuid4(),
            name="API Skill",
            source_hash="sha256:" + ("1" * 64),
            source_type="directory",
            interpretation_status=SkillInterpretationStatus.PREVIEW_READY,
            compatibility_level="assisted",
            confidence=0.25,
            interpreter_version="deterministic-parser/1.0.0",
            created_at=datetime(2026, 7, 2, 1, 30, tzinfo=UTC),
            preview=SkillPreview(
                normalized_package={
                    "package_format": "skillmind.normalized/v1",
                    "metadata": {"name": "API Skill"},
                },
                runtime_manifest_draft={
                    "manifest_version": "skillmind/v1alpha1",
                    "compatibility": {"level": "assisted"},
                },
            ),
        )

    async def save_inline(
        self,
        *,
        access: UserAccess,
        files: tuple[InlineSkillFile, ...],
    ) -> StoredSkillPreview:
        """受信 file を記録し、Organization を反映した固定 preview を返す。"""

        assert access.actor.user_id
        self.received_files = files
        return StoredSkillPreview(
            skill_source_id=self.stored.skill_source_id,
            interpretation_id=self.stored.interpretation_id,
            organization_id=access.actor.organization_id,
            name=self.stored.name,
            source_hash=self.stored.source_hash,
            source_type=self.stored.source_type,
            interpretation_status=self.stored.interpretation_status,
            compatibility_level=self.stored.compatibility_level,
            confidence=self.stored.confidence,
            interpreter_version=self.stored.interpreter_version,
            created_at=self.stored.created_at,
            preview=self.stored.preview,
        )

    async def save_upload(
        self,
        *,
        access: UserAccess,
        files: tuple[UploadSkillFile, ...],
    ) -> StoredSkillPreview:
        """受信 upload を記録し、storage 未配線 scenario では domain error を返す。"""

        assert access.actor.user_id
        if self.storage_unavailable:
            raise SkillStorageUnavailableError("Object storage is not configured for uploads")
        self.received_uploads = files
        return StoredSkillPreview(
            skill_source_id=self.stored.skill_source_id,
            interpretation_id=self.stored.interpretation_id,
            organization_id=access.actor.organization_id,
            name=self.stored.name,
            source_hash=self.stored.source_hash,
            source_type=self.stored.source_type,
            interpretation_status=self.stored.interpretation_status,
            compatibility_level=self.stored.compatibility_level,
            confidence=self.stored.confidence,
            interpreter_version=self.stored.interpreter_version,
            created_at=self.stored.created_at,
            preview=self.stored.preview,
        )

    async def get_interpretation(
        self, *, organization_id: UUID, interpretation_id: UUID
    ) -> StoredSkillPreview:
        """固定 preview を返し、missing scenario では domain error を返す。"""

        assert organization_id
        if self.missing:
            raise SkillInterpretationNotFoundError(
                f"SkillInterpretation not found: {interpretation_id}"
            )
        return self.stored

    async def create_version_draft(
        self, *, access: UserAccess, interpretation_id: UUID
    ) -> StoredSkillVersion:
        """Assisted の明示 acceptance warning を持つ固定 DRAFT を返す。"""

        return self._skill_version(
            organization_id=access.actor.organization_id,
            interpretation_id=interpretation_id,
            status=SkillVersionStatus.DRAFT,
        )

    def _skill_version(
        self,
        *,
        organization_id: UUID,
        interpretation_id: UUID | None = None,
        skill_version_id: UUID | None = None,
        status: SkillVersionStatus = SkillVersionStatus.PUBLISHED,
    ) -> StoredSkillVersion:
        """SkillVersion lifecycle API 用の固定 frozen version を組み立てる。"""

        return StoredSkillVersion(
            skill_id=uuid4(),
            skill_version_id=skill_version_id or uuid4(),
            skill_source_id=self.stored.skill_source_id,
            interpretation_id=interpretation_id or self.stored.interpretation_id,
            organization_id=organization_id,
            skill_key="api-skill",
            name="API Skill",
            description="API 契約テスト用の説明文。",
            version="0.1.0",
            status=status,
            manifest_checksum="sha256:" + ("3" * 64),
            manifest={"manifest_version": "skillmind/v1alpha1"},
            gate_passed=True,
            gate_findings=(
                ManifestGateFinding(
                    code="assisted_review_required",
                    severity="warning",
                    message="Assisted compatibility requires explicit administrator acceptance",
                ),
            ),
            interpretation_diff={},
            created_at=datetime(2026, 7, 3, 13, 0, tzinfo=UTC),
            published_by=(uuid4() if status is not SkillVersionStatus.DRAFT else None),
            published_at=(
                datetime(2026, 7, 3, 14, 0, tzinfo=UTC)
                if status is not SkillVersionStatus.DRAFT
                else None
            ),
        )

    async def list_skill_versions(self, *, organization_id: UUID) -> tuple[StoredSkillVersion, ...]:
        """Organization library の固定一件を返す。"""

        return (self._skill_version(organization_id=organization_id),)

    async def get_skill_version(
        self, *, organization_id: UUID, skill_version_id: UUID
    ) -> StoredSkillVersion:
        """Organization 内の指定 version を返す。"""

        return self._skill_version(
            organization_id=organization_id,
            skill_version_id=skill_version_id,
        )

    async def publish_skill_version(
        self, *, access: UserAccess, skill_version_id: UUID, accepted_warnings: frozenset[str]
    ) -> StoredSkillVersion:
        """未受理 warning を API が 409 へ変換できるよう拒否する。"""

        del access, skill_version_id, accepted_warnings
        raise SkillPublishGateError("unresolved findings")

    async def deprecate_skill_version(
        self, *, access: UserAccess, skill_version_id: UUID
    ) -> StoredSkillVersion:
        """指定 version を DEPRECATED とした固定結果を返す。"""

        return self._skill_version(
            organization_id=access.actor.organization_id,
            skill_version_id=skill_version_id,
            status=SkillVersionStatus.DEPRECATED,
        )

    async def delete_skill_version(self, *, access: UserAccess, skill_version_id: UUID) -> None:
        """削除要求を記録し、参照ありを模す version だけ 409 経路へ落とす。"""

        del access
        if skill_version_id == self.blocked_delete_version_id:
            raise SkillVersionDeleteBlockedError(
                "SkillVersion is still referenced by run or composition records"
            )
        self.deleted_version_ids.append(skill_version_id)

    async def enable_project_skill_version(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
        skill_version_id: UUID,
    ) -> StoredProjectSkillVersion:
        """Project へ精確版を有効化した固定監査結果を返す。"""

        return self._project_skill_version(
            organization_id=access.actor.organization_id,
            project_id=project_id,
            skill_version_id=skill_version_id,
            enabled_by=access.actor.user_id,
        )

    async def disable_project_skill_version(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
        skill_version_id: UUID,
    ) -> StoredProjectSkillVersion:
        """Project の有効化を停用した固定監査結果を返す。"""

        return self._project_skill_version(
            organization_id=access.actor.organization_id,
            project_id=project_id,
            skill_version_id=skill_version_id,
            enabled_by=uuid4(),
            disabled_at=datetime(2026, 7, 4, 9, 0, tzinfo=UTC),
        )

    async def list_project_skill_versions(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        include_disabled: bool = False,
    ) -> tuple[StoredProjectSkillVersion, ...]:
        """Project の active enablement 一件を返す。"""

        del include_disabled
        return (
            self._project_skill_version(
                organization_id=organization_id,
                project_id=project_id,
                skill_version_id=uuid4(),
                enabled_by=uuid4(),
            ),
        )

    def _project_skill_version(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        skill_version_id: UUID,
        enabled_by: UUID,
        disabled_at: datetime | None = None,
    ) -> StoredProjectSkillVersion:
        """Project enablement の公開 read model を組み立てる。"""

        return StoredProjectSkillVersion(
            project_id=project_id,
            organization_id=organization_id,
            skill_version=self._skill_version(
                organization_id=organization_id,
                skill_version_id=skill_version_id,
            ),
            enabled_by=enabled_by,
            enabled_at=datetime(2026, 7, 3, 15, 0, tzinfo=UTC),
            disabled_at=disabled_at,
        )

    async def list_published_tasks(
        self, *, project_id: UUID
    ) -> tuple[PublishedTaskDescriptor, ...]:
        """Project を反映した固定の汎用 task catalog を返す。"""

        assert project_id
        # 同じ instance からは同じ task を返す。呼ぶたび ID が変わると、catalog と Run 履歴の
        # 突き合わせを検証する test が「毎回別の task」を見ることになり成立しない。
        skill_version_id = self._catalog_skill_version_id
        return (
            PublishedTaskDescriptor(
                skill_id=uuid4(),
                skill_version_id=skill_version_id,
                skill_key="repository-review",
                skill_name="Repository Review",
                version="1.0.0",
                task_key="review-change",
                task_id=derive_task_id(skill_version_id=skill_version_id, task_key="review-change"),
                capability="repository.review",
                title="Repository Review",
                task_type="immediate",
                input_schema={"type": "object", "additionalProperties": False},
                output_schema={"type": "object", "additionalProperties": False},
                input_schema_checksum="sha256:" + ("b" * 64),
                output_schema_checksum="sha256:" + ("c" * 64),
                task_output_schema=None,
                task_output_schema_checksum=None,
                workflow="review-change-v1",
                view="repository-review-report",
                default_view="repository-review-report",
                compatibility_level="adapted",
                tool_requirements=(
                    TaskToolRequirement(capability="repository.read/v1", required=True),
                ),
                published_at=datetime(2026, 7, 9, tzinfo=UTC),
                capability_blueprint={
                    "blueprint_version": "skillmind.capability-blueprint/v1",
                    "capabilities": [{"key": "repository.review", "title": "Repository Review"}],
                    "tasks": [
                        {
                            "key": "review-change",
                            "capability": "repository.review",
                            "objective": "Review the selected repository change.",
                        }
                    ],
                    "resource_requirements": [
                        {
                            "key": "repository-source",
                            "kind": "repository",
                            "required": True,
                            "access": "read",
                            "capabilities": ["repository.read/v1"],
                        }
                    ],
                },
                readiness=TaskReadiness(
                    level=TaskReadinessLevel.CONFIGURATION_REQUIRED,
                    requirements=(
                        RequirementBinding(
                            key="repository-source",
                            kind="repository",
                            required=True,
                            access="read",
                            status=RequirementStatus.UNAVAILABLE,
                            reason="The project has no resource bound for this requirement yet",
                            candidates=(),
                            capabilities=("repository.read/v1",),
                            selection_guidance=None,
                        ),
                    ),
                ),
            ),
        )

    async def resolve_task_run(
        self,
        *,
        project_id: UUID,
        skill_version_id: UUID,
        task_key: str,
        input_json: dict[str, object],
    ) -> ResolvedTaskRun:
        """PUBLISHED task を解決し、not found/入力不正 scenario を再現する。"""

        if self.published_task_missing:
            raise PublishedTaskNotFoundError(
                f"Published task not found: {skill_version_id}/{task_key}"
            )
        if self.task_input_invalid:
            raise TaskInputInvalidError("Run input does not match task input schema")
        assert project_id
        assert input_json is not None
        return ResolvedTaskRun(
            skill_id=uuid4(),
            skill_version_id=skill_version_id,
            skill_key="repository-review",
            version="1.0.0",
            task_key=task_key,
            capability="repository.review",
            task_type="immediate",
            manifest_checksum="sha256:" + ("a" * 64),
            input_schema={
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
            input_schema_checksum="sha256:" + ("b" * 64),
            output_schema_checksum="sha256:" + ("c" * 64),
            task_output_schema=None,
            task_output_schema_checksum=None,
            allowed_capabilities=("repository.read/v1",),
            skill_snapshot={"skill_version_id": str(skill_version_id), "sort_order": 0},
        )

    async def begin_interpret(
        self, *, organization_id: UUID, skill_source_id: UUID, force_regenerate: bool = False
    ) -> InterpretationLaunch:
        """Model を呼ばずに interpret 受理結果を返す。source/interpreter scenario を再現する。"""

        if self.source_missing:
            raise SkillSourceNotFoundError(f"SkillSource not found: {skill_source_id}")
        if self.interpreter_unavailable:
            raise SkillInterpreterUnavailableError("Skill model interpreter is not configured")
        if self.source_integrity_failed:
            raise SkillSourceIntegrityError("Stored SkillSource content hash drifted")
        key = "sha256:" + (("f" if force_regenerate else "e") * 64)
        if self.launch_stored and not force_regenerate:
            return InterpretationLaunch(
                status="stored",
                execution_key=key,
                stored=self._execution(organization_id=organization_id),
                job_name="",
                job_kwargs={},
            )
        return InterpretationLaunch(
            status="queued",
            execution_key=key,
            stored=None,
            job_name="interpret_skill_source_job",
            job_kwargs={
                "organization_id": str(organization_id),
                "skill_source_id": str(skill_source_id),
                "model": "claude-opus-4-8",
                "parameters": {},
                "execution_key": key,
                "force_regenerate": force_regenerate,
                "regeneration_nonce": "forced" if force_regenerate else None,
            },
        )

    async def begin_adjust(
        self,
        *,
        organization_id: UUID,
        interpretation_id: UUID,
        instruction: str,
        actor_id: UUID,
    ) -> InterpretationLaunch:
        """Model を呼ばずに adjust 受理結果を返す。404/409 は同期のまま返す。"""

        if self.missing:
            raise SkillInterpretationNotFoundError(
                f"SkillInterpretation not found: {interpretation_id}"
            )
        if self.not_ready:
            raise SkillInterpretationNotReadyError(
                f"SkillInterpretation is not preview-ready: {interpretation_id}"
            )
        if self.interpreter_unavailable:
            raise SkillInterpreterUnavailableError("Skill model interpreter is not configured")
        self.adjustment_instruction = instruction
        key = "sha256:" + ("f" * 64)
        return InterpretationLaunch(
            status="queued",
            execution_key=key,
            stored=None,
            job_name="adjust_skill_interpretation_job",
            job_kwargs={
                "organization_id": str(organization_id),
                "interpretation_id": str(interpretation_id),
                "instruction": instruction,
                "actor_id": str(actor_id),
                "model": "claude-opus-4-8",
                "parameters": {},
                "execution_key": key,
            },
        )

    async def find_terminal_execution(
        self, *, organization_id: UUID, execution_key: str
    ) -> tuple[UUID, str, str | None] | None:
        """SSE 接続時の即時回放用に、確定済み execution の軽量 tuple を返す。"""

        del organization_id, execution_key
        return self.terminal

    async def get_interpretation_execution(
        self, *, organization_id: UUID, interpretation_id: UUID
    ) -> StoredInterpretationExecution:
        """Model interpretation の実行 detail を diff 付きで返す。"""

        if self.missing:
            raise SkillInterpretationNotFoundError(
                f"SkillInterpretation not found: {interpretation_id}"
            )
        return self._execution(organization_id=organization_id, diff={"has_changes": False})

    def _execution(
        self,
        *,
        organization_id: UUID,
        parent_interpretation_id: UUID | None = None,
        adjustment: dict[str, object] | None = None,
        diff: dict[str, object] | None = None,
    ) -> StoredInterpretationExecution:
        """API contract 検証用の固定 model interpretation 実行結果を作る。"""

        return StoredInterpretationExecution(
            interpretation_id=self.stored.interpretation_id,
            skill_source_id=self.stored.skill_source_id,
            organization_id=organization_id,
            status=SkillInterpretationStatus.PREVIEW_READY,
            origin="model",
            model="claude-opus-4-8",
            interpreter_version="skillmind-skill-interpreter/1.0.0",
            execution_key="sha256:" + ("e" * 64),
            error_code=None,
            compatibility_level="adapted",
            confidence=0.85,
            summary="Adapted repository review task.",
            created_at=datetime(2026, 7, 8, tzinfo=UTC),
            preview=self.stored.preview,
            report={
                "report_version": "skillmind.skill-interpretation-report/v1",
                "summary": "Adapted repository review task.",
            },
            reused=False,
            parent_interpretation_id=parent_interpretation_id,
            adjustment=adjustment,
            diff=diff or {},
        )


class FakeEvaluationService:
    """Evaluation API contract test 用の追加式 in-memory service。"""

    def __init__(self, *, invalid_revision: bool = False) -> None:
        """正常追加または pointer 拒否 scenario を設定する。"""

        self.invalid_revision = invalid_revision
        self.items: list[StoredEvaluation] = []
        self.received: CreateEvaluationCommand | None = None
        self.result_id = uuid4()
        self.scope: tuple[UUID, UUID] | None = None
        self.accesses: list[UserAccess] = []
        self.failure: Exception | None = None
        self.submissions: dict[tuple[UUID, UUID], tuple[str, StoredEvaluation]] = {}

    async def create(
        self, command: CreateEvaluationCommand, *, access: UserAccess,
    ) -> StoredEvaluation:
        """Request command を記録し、原値補完済みの固定評価を追加する。"""

        self._access(access, project_id=command.project_id, run_id=command.run_id)
        assert command.user_id == access.actor.user_id
        return self._append(command)

    def _append(self, command: CreateEvaluationCommand) -> StoredEvaluation:
        """従来追加と新原要求の初回で同じ保存応答を使用する。"""

        if self.invalid_revision:
            raise InvalidEvaluationRevisionError("Revision pointer does not exist")
        self.received = command
        now = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
        evaluation = StoredEvaluation(
            evaluation_id=uuid4(),
            result_id=self.result_id,
            run_id=command.run_id,
            user_id=command.user_id,
            rating=command.rating,
            verdict=command.verdict,
            comment=command.comment,
            revisions=tuple(
                StoredEvaluationRevision(
                    pointer=revision.pointer,
                    original_value="before",
                    suggested_value=revision.suggested_value,
                    reason=revision.reason,
                )
                for revision in command.revisions
            ),
            created_at=now,
        )
        self.items.append(evaluation)
        return evaluation

    async def list_for_run(
        self, *, project_id: UUID, run_id: UUID, access: UserAccess,
    ) -> tuple[StoredEvaluation, ...]:
        """指定 Run に追加済みの評価だけを作成順で返す。"""

        self._access(access, project_id=project_id, run_id=run_id)
        return tuple(item for item in self.items if item.run_id == run_id)

    async def submit(
        self, command: CreateEvaluationCommand, *, submission_key: UUID, result_id: UUID,
        access: UserAccess,
    ) -> StoredEvaluationSubmission:
        """共有原要求 hash で fixture の再送を区別し、DB の競争証明とは分ける。"""

        self._access(access, project_id=command.project_id, run_id=command.run_id)
        assert command.user_id == access.actor.user_id
        self._result(result_id)
        checksum = evaluation_request_hash(
            command, result_id=result_id, submission_key=submission_key,
        )
        original = self.submissions.get((access.actor.user_id, submission_key))
        if original is not None:
            if checksum != original[0]:
                raise EvaluationSubmissionConflictError("Original request differs")
            return StoredEvaluationSubmission(
                project_id=command.project_id, run_id=command.run_id,
                submission_key=submission_key, evaluation=original[1], idempotent_replay=True,
            )
        evaluation = self._append(command)
        self.submissions[(access.actor.user_id, submission_key)] = (checksum, evaluation)
        return StoredEvaluationSubmission(
            project_id=command.project_id, run_id=command.run_id,
            submission_key=submission_key, evaluation=evaluation,
        )

    async def get_submission(
        self, *, project_id: UUID, run_id: UUID, submission_key: UUID, result_id: UUID,
        access: UserAccess,
    ) -> StoredEvaluationSubmission:
        """同じ actor の原要求だけを確認し、類似する履歴から成功を補わない。"""

        self._access(access, project_id=project_id, run_id=run_id)
        self._result(result_id)
        original = self.submissions.get((access.actor.user_id, submission_key))
        if original is None:
            raise EvaluationSubmissionNotFoundError("Original request was not found")
        return StoredEvaluationSubmission(
            project_id=project_id, run_id=run_id, submission_key=submission_key,
            evaluation=original[1], idempotent_replay=True,
        )

    async def list_page(
        self, *, project_id: UUID, run_id: UUID, access: UserAccess, limit: int = 20,
        after: UUID | None = None,
    ) -> StoredEvaluationPage:
        """同時刻も ID で安定順化し、未知/別 Result の cursor を固定拒否する。"""

        self._access(access, project_id=project_id, run_id=run_id)
        ordered = sorted(self.items, key=lambda item: (item.created_at, item.evaluation_id))
        start = 0
        if after is not None:
            positions = [index for index, item in enumerate(ordered) if item.evaluation_id == after]
            if not positions:
                raise InvalidEvaluationCursorError("Invalid Evaluation cursor")
            start = positions[0] + 1
        items = tuple(ordered[start:start + limit])
        return StoredEvaluationPage(
            project_id=project_id, run_id=run_id, result_id=self.result_id, items=items,
            next_cursor=items[-1].evaluation_id if start + limit < len(ordered) else None,
        )

    def _access(self, access: UserAccess, *, project_id: UUID, run_id: UUID) -> None:
        """元 credential の伝達と URL の scope を観測し、資格失効を注入可能にする。"""

        self.accesses.append(access)
        if self.failure is not None:
            raise self.failure
        if self.scope is None:
            self.scope = (project_id, run_id)
        if self.scope != (project_id, run_id):
            raise RunNotFoundError("synthetic-private-other-run")

    def _result(self, result_id: UUID) -> None:
        """表示対象を勝手に現在 Result へ置き換えない。"""

        if result_id != self.result_id:
            raise EvaluationResultMismatchError("synthetic-private-current-result")


class FakeDocumentService:
    """文書 API contract を storage/DB なしで検証する fake。"""

    def __init__(
        self,
        *,
        not_found: bool = False,
        quota_exceeded: bool = False,
        conflict: bool = False,
    ) -> None:
        """各種正常/拒否 scenario を設定し、受信操作を記録する。"""

        self.not_found = not_found
        self.quota_exceeded = quota_exceeded
        self.conflict = conflict
        self.uploaded: list[tuple[str, str, bytes]] = []
        self.deleted: list[UUID] = []
        self.content = b"document-body"
        self.max_upload_bytes = 25 * 1024 * 1024
        self.uploads: dict[tuple[UUID, UUID, UUID], StoredDocumentUpload] = {}
        self.upload_fingerprints: dict[tuple[UUID, UUID, UUID], tuple[str, str, bytes, str]] = {}
        self.upload_queries: list[tuple[UUID, UUID, UserAccess]] = []
        self.upload_targets: dict[tuple[UUID, UUID, UUID], UUID] = {}
        self.upload_closures: dict[tuple[UUID, UUID, UUID], StoredDocumentUploadClosure] = {}
        self.closure_requests: list[tuple[UUID, UUID, UserAccess]] = []
        self.closure_queries: list[tuple[UUID, UUID, UserAccess]] = []

    async def upload_document(
        self,
        *,
        project_id: UUID,
        upload_key: UUID,
        access: UserAccess,
        folder: str,
        name: str,
        data: bytes,
        content_type: str,
    ) -> StoredDocument:
        """受信 upload を記録し、拒否 scenario では domain error を返す。"""

        identity = (project_id, access.actor.user_id, upload_key)
        fingerprint = (folder, name, data, content_type)
        previous = self.uploads.get(identity)
        if previous is not None:
            if self.upload_fingerprints.get(identity) != fingerprint:
                raise DocumentUploadKeyConflictError("private original fingerprint")
            if identity in self.upload_closures:
                raise DocumentUploadClosedError("private closed state")
            if previous.document is None:
                raise DocumentUploadPendingError("private pending state")
            return previous.document
        if self.quota_exceeded:
            raise UploadRejectedError("project_quota_exceeded", "Project storage quota is exceeded")
        if self.conflict:
            raise DocumentConflictError(f"Document already exists: {folder}/{name}")
        self.uploaded.append((folder, name, data))
        document = _fake_document(
            project_id, access.actor.user_id, folder, name, len(data), content_type
        )
        self.uploads[identity] = StoredDocumentUpload(
            upload_key=upload_key, project_id=project_id, state="PUBLISHED",
            created_at=document.created_at, document=document,
        )
        self.upload_fingerprints[identity] = fingerprint
        self.upload_targets[identity] = document.document_id
        return document

    async def get_upload(
        self, *, project_id: UUID, upload_key: UUID, access: UserAccess,
    ) -> StoredDocumentUpload:
        """同じ actor/Project の原記録だけを読み、削除後も元の公開 metadata を保持する。"""

        self.upload_queries.append((project_id, upload_key, access))
        upload = self.uploads.get((project_id, access.actor.user_id, upload_key))
        if upload is None:
            raise DocumentUploadNotFoundError("private upload key")
        return upload

    async def close_upload(
        self, *, project_id: UUID, upload_key: UUID, access: UserAccess,
    ) -> tuple[StoredDocumentUploadClosure, bool]:
        """fixture の原対象だけに独立回执を作る。競争・transaction は実 service 側で検証する。"""

        self.closure_requests.append((project_id, upload_key, access))
        identity = (project_id, access.actor.user_id, upload_key)
        original = self.uploads.get(identity)
        if original is None:
            raise DocumentUploadNotFoundError("private upload key")
        if original.state == "PUBLISHED":
            raise DocumentUploadAlreadyPublishedError("private published receipt")
        if identity in self.upload_closures:
            return self.upload_closures[identity], False
        closure = StoredDocumentUploadClosure(
            upload_key=upload_key, project_id=project_id,
            document_id=self.upload_targets[identity], closed_at=datetime.now(UTC),
        )
        self.upload_closures[identity] = closure
        return closure, True

    async def get_upload_closure(
        self, *, project_id: UUID, upload_key: UUID, access: UserAccess,
    ) -> StoredDocumentUploadClosure:
        """同じ原作者の閉鎖回执だけを取得し、書込 fake は呼ばない。"""

        self.closure_queries.append((project_id, upload_key, access))
        closure = self.upload_closures.get((project_id, access.actor.user_id, upload_key))
        if closure is None:
            raise DocumentUploadClosureNotFoundError("private closure key")
        return closure

    async def list_documents(self, *, project_id: UUID) -> list[StoredDocument]:
        """固定の一件を Project 反映で返す。"""

        return [_fake_document(project_id, uuid4(), "specs", "overview.md", 12, "text/markdown")]

    async def download_document(
        self, *, project_id: UUID, document_id: UUID
    ) -> tuple[StoredDocument, bytes]:
        """metadata と本文を返し、missing scenario では domain error を返す。"""

        if self.not_found:
            raise DocumentNotFoundError(f"Document not found: {document_id}")
        document = _fake_document(project_id, uuid4(), "specs", "overview.md", 12, "text/markdown")
        return document, self.content

    async def get_document(self, *, project_id: UUID, document_id: UUID) -> StoredDocument:
        """元 ID のみを反映し、本文の読取や削除を行わない。"""

        if self.not_found:
            raise DocumentNotFoundError("Document unavailable")
        document = _fake_document(project_id, uuid4(), "specs", "overview.md", 12, "text/markdown")
        return replace(document, document_id=document_id)

    async def delete_document(
        self, *, project_id: UUID, document_id: UUID, access: UserAccess
    ) -> None:
        """削除を記録し、missing scenario では domain error を返す。"""

        assert project_id
        assert access.actor.user_id
        if self.not_found:
            raise DocumentNotFoundError(f"Document not found: {document_id}")
        self.deleted.append(document_id)


def _fake_document(
    project_id: UUID, uploaded_by: UUID, folder: str, name: str, size: int, mime: str
) -> StoredDocument:
    """API test 用の安定した StoredDocument を組み立てる。"""

    return StoredDocument(
        document_id=uuid4(),
        project_id=project_id,
        folder=folder,
        name=name,
        size=size,
        mime=mime,
        checksum="sha256:" + ("a" * 64),
        uploaded_by=uploaded_by,
        created_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
    )


class FakeCompositionService:
    """Module API contract を DB なしで検証する fake。"""

    def __init__(self, *, not_found: bool = False, invalid_skill: bool = False) -> None:
        """404/422 scenario を設定し、受信 command を記録する。"""

        self.not_found = not_found
        self.invalid_skill = invalid_skill
        self.created: list[tuple[str, list[UUID]]] = []
        self.updated: list[tuple[UUID, str, list[UUID]]] = []
        self.deleted: list[UUID] = []
        self.accesses: list[UserAccess] = []

    async def list_modules(self, *, project_id: UUID) -> list[StoredModule]:
        """固定の 1 module を返す。"""

        return [make_stored_module(project_id=project_id)]

    async def create_module(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
        name: str,
        description: str,
        skill_version_ids: list[UUID],
    ) -> StoredModule:
        """作成 command を記録し、echo した read model を返す。"""

        self.accesses.append(access)
        if self.invalid_skill:
            raise ModuleSkillInvalidError("Skill versions are not published in this project")
        self.created.append((name, list(skill_version_ids)))
        return make_stored_module(
            project_id=project_id,
            name=name,
            description=description,
            skill_version_ids=skill_version_ids,
        )

    async def update_module(
        self,
        *,
        access: UserAccess,
        project_id: UUID,
        module_id: UUID,
        name: str,
        description: str,
        skill_version_ids: list[UUID],
    ) -> StoredModule:
        """更新 command を記録し、echo した read model を返す。"""

        self.accesses.append(access)
        if self.not_found:
            raise ModuleNotFoundError(f"Module not found: {module_id}")
        if self.invalid_skill:
            raise ModuleSkillInvalidError("Skill versions are not published in this project")
        self.updated.append((module_id, name, list(skill_version_ids)))
        return make_stored_module(
            project_id=project_id,
            module_id=module_id,
            name=name,
            description=description,
            skill_version_ids=skill_version_ids,
        )

    async def delete_module(
        self, *, access: UserAccess, project_id: UUID, module_id: UUID
    ) -> None:
        """削除対象を記録する。"""

        self.accesses.append(access)
        if self.not_found:
            raise ModuleNotFoundError(f"Module not found: {module_id}")
        self.deleted.append(module_id)


def make_stored_module(
    *,
    project_id: UUID,
    module_id: UUID | None = None,
    name: str = "品质分析",
    description: str = "单票据品质分析模块",
    skill_version_ids: list[UUID] | None = None,
) -> StoredModule:
    """テスト用の module read model を生成する。"""

    version_ids = skill_version_ids if skill_version_ids is not None else [uuid4()]
    return StoredModule(
        module_id=module_id or uuid4(),
        project_id=project_id,
        name=name,
        description=description,
        skills=tuple(
            ModuleSkillBinding(
                skill_version_id=version_id,
                skill_id=uuid4(),
                skill_key=f"skill-{index}",
                skill_name=f"Skill {index}",
                version="1.0.0",
                sort_order=index,
            )
            for index, version_id in enumerate(version_ids)
        ),
        created_at=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
    )


class FakeArqPool:
    """ARQ queue client を置き換え、投入された job を記録する fake。"""

    def __init__(self) -> None:
        """(job 名, kwargs) の受信順記録と job_id を保持する。"""

        self.jobs: list[tuple[str, dict[str, object]]] = []
        self.job_ids: list[str | None] = []

    async def enqueue_job(
        self,
        function: str,
        *args: object,
        _job_id: str | None = None,
        _queue_name: str | None = None,
    ) -> None:
        """投入 job を記録する。API は単一 kwargs dict を位置引数で渡す。"""

        del _queue_name
        kwargs = cast("dict[str, object]", args[0]) if args else {}
        self.jobs.append((function, kwargs))
        self.job_ids.append(_job_id)

    async def aclose(self) -> None:
        """Lifespan teardown の close 契約を満たす no-op。"""

        return None


class FakeScheduleService:
    """TaskSchedule API contract を DB なしで検証する fake (計画 §22)。"""

    def __init__(
        self,
        *,
        not_found: bool = False,
        invalid: bool = False,
        conflict: bool = False,
        transition_rejected: bool = False,
    ) -> None:
        """各種拒否 scenario を設定し、受信操作を記録する。"""

        self.not_found = not_found
        self.invalid = invalid
        self.conflict = conflict
        self.transition_rejected = transition_rejected
        self.created: list[tuple[str, ScheduleDefinition]] = []
        self.updated: list[tuple[UUID, int]] = []
        self.status_changes: list[tuple[UUID, ScheduleStatus, int]] = []
        self.listed: list[tuple[UUID, int, int, str | None, ScheduleStatus | None]] = []
        self.previewed: list[ScheduleDefinition] = []
        self.preview_result: list[datetime] = [
            datetime(2026, 7, 27, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 28, 0, 0, tzinfo=UTC),
            datetime(2026, 7, 29, 0, 0, tzinfo=UTC),
        ]

    async def list_schedules(
        self,
        *,
        project_id: UUID,
        limit: int,
        offset: int,
        q: str | None = None,
        status: ScheduleStatus | None = None,
    ) -> SchedulePage:
        """固定の一件を Project 反映で返す。"""

        self.listed.append((project_id, limit, offset, q, status))
        return SchedulePage(
            items=(_fake_schedule(project_id),), total=1, limit=limit, offset=offset
        )

    async def get_schedule(self, *, project_id: UUID, schedule_id: UUID) -> ScheduleRecord:
        """指定 schedule を返す。not_found scenario では domain error を返す。"""

        if self.not_found:
            raise ScheduleNotFoundError("Schedule was not found")
        return _fake_schedule(project_id, schedule_id=schedule_id)

    def preview(self, definition: ScheduleDefinition) -> list[datetime]:
        """受信した定義を記録し、固定の発火予告を返す。"""

        self.previewed.append(definition)
        return list(self.preview_result)

    async def create_schedule(
        self,
        *,
        project_id: UUID,
        access: UserAccess,
        name: str,
        definition: ScheduleDefinition,
        skill_version_id: UUID,
        task_key: str,
        input_json: dict[str, object],
        sources: dict[str, str],
    ) -> ScheduleRecord:
        """受信した作成要求を記録する。"""

        del skill_version_id, task_key, input_json, sources
        if self.invalid:
            raise ScheduleInvalidError("Schedule has no future occurrence")
        self.created.append((name, definition))
        return _fake_schedule(project_id, name=name, created_by=access.actor.user_id)

    async def update_schedule(
        self,
        *,
        project_id: UUID,
        access: UserAccess,
        schedule_id: UUID,
        name: str,
        definition: ScheduleDefinition,
        input_json: dict[str, object],
        sources: dict[str, str],
        expected_row_version: int,
    ) -> ScheduleRecord:
        """受信した更新要求を記録する。"""

        del access, definition, input_json, sources
        if self.not_found:
            raise ScheduleNotFoundError("Schedule was not found")
        if self.conflict:
            raise ScheduleConflictError("Schedule was modified by another request")
        self.updated.append((schedule_id, expected_row_version))
        return _fake_schedule(project_id, schedule_id=schedule_id, name=name)

    async def change_status(
        self,
        *,
        project_id: UUID,
        access: UserAccess,
        schedule_id: UUID,
        target: ScheduleStatus,
        expected_row_version: int,
    ) -> ScheduleRecord:
        """受信した状態遷移を記録する。"""

        del access
        if self.not_found:
            raise ScheduleNotFoundError("Schedule was not found")
        if self.conflict:
            raise ScheduleConflictError("Schedule was modified by another request")
        if self.transition_rejected:
            raise InvalidScheduleTransitionError("Schedule transition is not allowed")
        self.status_changes.append((schedule_id, target, expected_row_version))
        return replace(
            _fake_schedule(project_id, schedule_id=schedule_id, status=target),
            row_version=expected_row_version + 1,
        )


def _fake_schedule(
    project_id: UUID,
    *,
    schedule_id: UUID | None = None,
    name: str = "nightly-analysis",
    status: ScheduleStatus = ScheduleStatus.ACTIVE,
    created_by: UUID | None = None,
) -> ScheduleRecord:
    """固定 field の schedule read model を組み立てる。"""

    moment = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    return ScheduleRecord(
        schedule_id=schedule_id or uuid4(),
        project_id=project_id,
        name=name,
        kind=ScheduleKind.CRON,
        status=status,
        timezone="Asia/Tokyo",
        cron_expression="0 3 * * *",
        run_at=None,
        end_at=None,
        max_runs=None,
        skill_version_id=uuid4(),
        task_key="analyze",
        input_json={"ticket": "T-1"},
        sources={"issues": "integration:00000000-0000-0000-0000-000000000001"},
        next_run_at=datetime(2026, 7, 27, 18, 0, tzinfo=UTC),
        last_run_at=None,
        last_run_id=None,
        last_outcome=None,
        last_error=None,
        run_count=0,
        missed_count=0,
        created_by=created_by or uuid4(),
        row_version=1,
        created_at=moment,
        updated_at=moment,
    )
