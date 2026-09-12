"""段階認可の現在行を差し替える DB double。実 lock/transaction の証明には使わない。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from skillmind.db.models import (
    AgentSession,
    ChangeApproval,
    ChangeProposal,
    EffectExecution,
    Integration,
    Organization,
    Project,
    ProjectMember,
    ResourceBinding,
    Run,
    RunSegment,
    User,
)
from skillmind.effects.database_write import DATABASE_WRITE_PROVIDER_VERSION
from skillmind.effects.release import ExecutionFeatures
from skillmind.runs.domain import lease_token_hash
from skillmind.runs.repository import RunRepository
from tests.effects.database_fixtures import database_execution


class AuthorizationHarness:
    """実 repository の段階判定へ可変 current rows と操作順の記録を注入する。"""

    def __init__(self) -> None:
        """同一発起人が精確批准した、まだ有効な lease の一組を構築する。"""

        self.claimed = c = database_execution()
        actor_id, organization_id, internal_session_id = uuid4(), uuid4(), uuid4()
        self.actor = SimpleNamespace(
            id=actor_id, organization_id=organization_id, status="ACTIVE", system_role="USER"
        )
        self.project = SimpleNamespace(
            id=c.project_id, organization_id=organization_id, status="ACTIVE"
        )
        self.member = SimpleNamespace(project_id=c.project_id, user_id=actor_id, status="ACTIVE")
        self.run = SimpleNamespace(
            id=c.run_id,
            project_id=c.project_id,
            status="WAITING_FOR_APPROVAL",
            permission_snapshot_json={
                "actor_id": str(actor_id),
                "allowed_capabilities": [c.capability_version],
            },
        )
        self.segment = SimpleNamespace(id=c.run_segment_id, run_id=c.run_id, status="WAITING")
        self.proposal = SimpleNamespace(
            id=c.proposal_id,
            run_id=c.run_id,
            project_id=c.project_id,
            run_segment_id=c.run_segment_id,
            status="APPLYING",
            version=1,
            checksum="a" * 71,
            expires_at=datetime.now(UTC) + timedelta(minutes=2),
            proposal_ref=c.proposal_ref,
            run_attempt_id=c.run_attempt_id,
            agent_session_id=internal_session_id,
            target_binding_id=c.binding_id,
            integration_id=c.integration_id,
            capability_version=c.capability_version,
            operation=c.operation,
            target_json=deepcopy(c.target),
            preview_json={"changes": deepcopy(list(c.changes))},
            precondition_json=deepcopy(c.precondition),
            verification_json=deepcopy(c.verification),
            idempotency_key=c.idempotency_key,
            request_fingerprint=c.request_fingerprint,
        )
        self.approval = SimpleNamespace(
            id=c.approval_id,
            run_id=c.run_id,
            proposal_id=c.proposal_id,
            actor_id=actor_id,
            source="USER",
            decision="APPROVED",
            proposal_version=1,
            proposal_checksum="a" * 71,
        )
        self.execution = SimpleNamespace(
            id=c.effect_execution_id,
            run_id=c.run_id,
            proposal_id=c.proposal_id,
            approval_id=c.approval_id,
            status="APPLYING",
            provider=c.provider,
            provider_version=DATABASE_WRITE_PROVIDER_VERSION,
            attempt_no=1,
            request_fingerprint=c.request_fingerprint,
            idempotency_key=c.idempotency_key,
            lease_token_hash=lease_token_hash(c.lease_token),
            lease_expires_at=c.lease_expires_at,
        )
        self.binding = SimpleNamespace(id=c.binding_id, scope_json=deepcopy(c.integration_scope))
        self.integration = SimpleNamespace(
            id=c.integration_id,
            provider=c.provider,
            revision=c.integration_revision,
            config_json=deepcopy(c.integration_config),
            secret_reference_id=c.secret_reference_id,
        )
        self.rows = {
            Run: self.run,
            RunSegment: self.segment,
            ChangeProposal: self.proposal,
            ChangeApproval: self.approval,
            EffectExecution: self.execution,
            User: self.actor,
            Organization: SimpleNamespace(id=organization_id),
            Project: self.project,
            ProjectMember: self.member,
            ResourceBinding: self.binding,
            Integration: self.integration,
            AgentSession: SimpleNamespace(
                id=internal_session_id, sdk_session_id=c.agent_session_id
            ),
        }
        self.locks = []
        self.session = MagicMock()
        self.session.get = AsyncMock(side_effect=self.get)
        self.session.scalar = AsyncMock(side_effect=self.scalar)
        self.repository = RunRepository(
            self.session, execution_features=ExecutionFeatures(database_writes=True)
        )
        self.repository._lock_run_row = AsyncMock(side_effect=self.lock_run)
        self.repository._lock_segment_row = AsyncMock(side_effect=self.lock_segment)
        self.repository.is_cancellation_requested = AsyncMock(return_value=False)
        # 既存の checksum/Blueprint validator は別の回帰対象。本検証は追加した段階判定に限定。
        self.repository._validate_proposal_row = AsyncMock()

    async def get(self, entity, identity, **kwargs):
        """原 identity を変更せず現在の model double を取得する。"""
        del identity, kwargs
        return self.rows.get(entity)

    async def scalar(self, statement):
        """SELECT の entity と lock 順を観測する。SQL lock は実装しない。"""
        entity = statement.column_descriptions[0]["entity"]
        self.locks.append(entity.__name__)
        return self.rows.get(entity)

    async def lock_run(self, *args, **kwargs):
        """Run lock の呼出順だけを記録する。"""
        self.locks.append("Run")
        return self.run

    async def lock_segment(self, *args, **kwargs):
        """Segment lock の呼出順だけを記録する。"""
        self.locks.append("RunSegment")
        return self.segment

    async def authorize(self):
        """実 repository の段階認可規則を呼ぶ。"""
        await self.repository.authorize_effect_step(
            self.claimed, provider_version=DATABASE_WRITE_PROVIDER_VERSION
        )
