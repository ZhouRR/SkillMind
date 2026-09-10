"""M0 の Run、実行試行、監査 event の永続 model を定義する。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from projectmind.db.base import Base, IdentityMixin, TimestampMixin
from projectmind.runs.domain import (
    RunAttemptStatus,
    RunSegmentStatus,
    RunSegmentTrigger,
    RunStatus,
    SessionContinuationMode,
    UserInteractionStatus,
)


class Organization(IdentityMixin, TimestampMixin, Base):
    """MVP の単一組織境界と既定 policy を保持する。"""

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    default_retention_days: Mapped[int] = mapped_column(Integer, nullable=False)


class User(IdentityMixin, TimestampMixin, Base):
    """Password credential と system role を持つ組織内 user。"""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_users_organization_email"),
        CheckConstraint("system_role IN ('ADMIN', 'USER')", name="users_system_role"),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="users_status"),
        CheckConstraint("row_version >= 1", name="users_row_version_positive"),
        # NULL は「未設定=browser 言語へ追従」を表すため許可する。
        CheckConstraint("ui_language IN ('zh', 'ja', 'en')", name="users_ui_language"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    system_role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    preferred_project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    ui_language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # 言語/Project preference と login 時刻では増やさず、管理・安全変更だけを競合検出する。
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )


class UserSecurityEvent(IdentityMixin, Base):
    """User 変更と同じ transaction に追加し、credential を含めない管理監査。"""

    __tablename__ = "user_security_events"
    __table_args__ = (
        UniqueConstraint("user_id", "row_version", name="uq_user_security_events_version"),
        CheckConstraint(
            "action IN ('CREATED', 'UPDATED', 'PASSWORD_CHANGED', 'SESSIONS_REVOKED')",
            name="user_security_events_action",
        ),
        CheckConstraint("row_version >= 1", name="user_security_events_version_positive"),
        CheckConstraint("revoked_sessions >= 0", name="user_security_events_revoked_nonnegative"),
        CheckConstraint(
            "previous_role IS NULL OR previous_role IN ('ADMIN', 'USER')",
            name="user_security_events_previous_role",
        ),
        CheckConstraint(
            "previous_status IS NULL OR previous_status IN ('ACTIVE', 'DISABLED')",
            name="user_security_events_previous_status",
        ),
        CheckConstraint("system_role IN ('ADMIN', 'USER')", name="user_security_events_role"),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="user_security_events_status"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    actor_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_role: Mapped[str | None] = mapped_column(String(16))
    previous_status: Mapped[str | None] = mapped_column(String(16))
    system_role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    revoked_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Project(IdentityMixin, TimestampMixin, Base):
    """Integration、Skill、Run を隔離する組織内 Project。"""

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("organization_id", "key", name="uq_projects_organization_key"),
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="projects_status"),
        CheckConstraint(
            "row_version >= 1 AND row_version <= 2147483647", name="projects_row_version_range",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class ProjectMember(IdentityMixin, TimestampMixin, Base):
    """User に新しい role を与えず Project access だけを付与する関係。"""

    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_members_project_user"),
        CheckConstraint("status IN ('ACTIVE', 'REMOVED')", name="project_members_status"),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectMemberEvent(IdentityMixin, Base):
    """Credential や任意 metadata を保存せず、所属の変更前後を追加式で保持する。"""

    __tablename__ = "project_member_events"
    __table_args__ = (
        CheckConstraint(
            "(action = 'ADDED' AND status = 'ACTIVE' AND "
            "(previous_status IS NULL OR previous_status = 'REMOVED')) OR "
            "(action = 'REMOVED' AND status = 'REMOVED' AND "
            "previous_status IS NOT NULL AND previous_status = 'ACTIVE')",
            name="project_member_events_transition",
        ),
        CheckConstraint(
            "(previous_status IS NULL) = (previous_joined_at IS NULL)",
            name="project_member_events_previous_state",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    member_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_members.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(16))
    previous_joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_id: Mapped[UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SecretReference(IdentityMixin, TimestampMixin, Base):
    """Worker が Secret を解決するための locator metadata だけを保持する。

    明文 credential は database へ保存しない。`locator` は deployment secret 名などの
    解決位置であり、公開 API の response model からも除外する。
    """

    __tablename__ = "secret_references"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "name", name="uq_secret_references_project_name"
        ),
        CheckConstraint(
            "resolver IN ('ENVIRONMENT', 'FILE', 'MANAGED')",
            name="secret_references_resolver",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')", name="secret_references_status"
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    resolver: Mapped[str] = mapped_column(String(32), nullable=False)
    locator: Mapped[str] = mapped_column(String(512), nullable=False)
    key_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[UUID] = mapped_column(nullable=False, index=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ManagedSecretMaterial(IdentityMixin, TimestampMixin, Base):
    """MANAGED SecretReference の KEK 封入済み密文を公開 read model と物理分離して保持する。

    明文は決して保存しない。どの response model もこの表を投影しないため、密文が API へ
    漏れる型経路は存在しない。復号は Worker が KEK と AAD(project_id + secret_reference_id)で行う。
    """

    __tablename__ = "managed_secret_material"
    __table_args__ = (
        UniqueConstraint(
            "secret_reference_id",
            name="uq_managed_secret_material_secret_reference_id",
        ),
    )

    # SecretReference と 1:1。UniqueConstraint が暗黙 index を兼ねるため index=True は付けない。
    # 規約名は Postgres の 63 byte 上限を超えて截断されるため、短い FK 名を明示する。
    secret_reference_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "secret_references.id",
            ondelete="RESTRICT",
            name="fk_managed_secret_material_reference",
        ),
        nullable=False,
    )
    # AAD 再構成と多層防御のため project_id を密文行にも冗長保持する。
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kek_version: Mapped[str] = mapped_column(String(32), nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class Integration(IdentityMixin, TimestampMixin, Base):
    """Project に属する具体 resource instance と非機密設定を保持する。"""

    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_integrations_project_name"),
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')", name="integrations_status"
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    secret_reference_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("secret_references.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_by: Mapped[UUID] = mapped_column(nullable=False, index=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResourceBinding(IdentityMixin, TimestampMixin, Base):
    """ResourceRequirement を Project/Task/Run の具体資源へ束縛する。

    Run level の行は immutable snapshot として扱い、元の Project/Task binding が更新されても
    provider、revision、scope と checksum を変えない。
    """

    __tablename__ = "resource_bindings"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "scope_level",
            "scope_key",
            "requirement_key",
            name="uq_resource_bindings_scope_requirement",
        ),
        CheckConstraint(
            "scope_level IN ('PROJECT_DEFAULT', 'TASK', 'RUN')",
            name="resource_bindings_scope_level",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    scope_level: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(256), nullable=False)
    requirement_key: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    integration_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("integrations.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    source_binding_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("resource_bindings.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_version: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    created_by: Mapped[UUID] = mapped_column(nullable=False, index=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(IdentityMixin, Base):
    """Opaque browser session の hash と失効・期限監査を保持する。"""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        CheckConstraint("credential_version IN (1, 2)", name="auth_sessions_credential_version"),
        CheckConstraint(
            "system_role_at_login IS NULL OR system_role_at_login IN ('ADMIN', 'USER')",
            name="auth_sessions_login_role",
        ),
        CheckConstraint(
            "credential_version = 1 OR system_role_at_login IS NOT NULL",
            name="auth_sessions_v2_role_required",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(71), nullable=False, unique=True, index=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    # 旧 writer の default は 1 のまま残し、v2 と誤認して受け入れない。
    credential_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    system_role_at_login: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_ip_hash: Mapped[str | None] = mapped_column(String(71))
    user_agent_hash: Mapped[str | None] = mapped_column(String(71))


class SkillSource(IdentityMixin, Base):
    """Organization へ導入した不変 Skill source snapshot。

    docs/01 §16 のとおり Skill 資産は Organization に属し、Project は
    `ProjectSkillVersion` で精確な PUBLISHED 版を明示的に有効化する。Project の可視性を
    source に刻むと同じ版を複数 Project で共有できないため、作用域は organization だけを保持する。
    """

    __tablename__ = "skill_sources"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "content_hash",
            name="uq_skill_sources_organization_content_hash",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(128))
    imported_by: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_snapshot_json: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False)
    # text snapshot だけでは binary asset を再現できないため、全 file の index を別保持する。
    source_file_index_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    interpretations: Mapped[list[SkillInterpretation]] = relationship(
        back_populates="skill_source",
        order_by="SkillInterpretation.created_at",
    )


class SkillInterpretation(IdentityMixin, Base):
    """一つの SkillSource に対する不変な parser/interpreter 結果。"""

    __tablename__ = "skill_interpretations"
    __table_args__ = (
        UniqueConstraint(
            "skill_source_id",
            "interpreter_version",
            "checksum",
            name="uq_skill_interpretations_source_version_checksum",
        ),
    )

    skill_source_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_sources.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    interpreter_version: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    compatibility_level: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    assumptions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    questions_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    diagnostics_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    normalized_package_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    manifest_draft_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # Model interpretation 用の追加列。Deterministic parser preview は NULL のままにする。
    report_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    execution_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Append-only な revision lineage。reinterpretation の親と適用した adjustment を保持する。
    parent_interpretation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("skill_interpretations.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    adjustment_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    skill_source: Mapped[SkillSource] = relationship(back_populates="interpretations")


class Skill(IdentityMixin, TimestampMixin, Base):
    """公開 version を束ねる組織内で一意な Skill identity。"""

    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint("organization_id", "key", name="uq_skills_organization_key"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class SkillVersion(IdentityMixin, TimestampMixin, Base):
    """Source と Interpretation を固定した Skill の version。"""

    __tablename__ = "skill_versions"
    __table_args__ = (
        UniqueConstraint("skill_id", "version", name="uq_skill_versions_skill_version"),
        UniqueConstraint("interpretation_id", name="uq_skill_versions_interpretation"),
    )

    skill_id: Mapped[UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    skill_source_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    interpretation_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_interpretations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    gate_report_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    published_by: Mapped[UUID | None] = mapped_column(index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuntimeManifest(IdentityMixin, Base):
    """SkillVersion に一対一で固定した実行 Manifest と checksum。"""

    __tablename__ = "runtime_manifests"

    interpretation_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_interpretations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    skill_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    manifest_version: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillComposition(IdentityMixin, TimestampMixin, Base):
    """複数 SkillVersion を role/module/task_group として束ねる組合。権限は付与しない。"""

    __tablename__ = "skill_compositions"
    __table_args__ = (
        CheckConstraint(
            "presentation IN ('role', 'module', 'task_group')",
            name="skill_compositions_presentation",
        ),
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="skill_compositions_status"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    presentation: Mapped[str] = mapped_column(String(16), nullable=False)
    behavior_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    items: Mapped[list[SkillCompositionItem]] = relationship(
        back_populates="composition",
        cascade="all, delete-orphan",
        order_by="SkillCompositionItem.sort_order",
    )


class SkillCompositionItem(IdentityMixin, Base):
    """組合内の SkillVersion 束縛。順序と将来の設定上書きを保持する。"""

    __tablename__ = "skill_composition_items"
    __table_args__ = (
        UniqueConstraint(
            "composition_id",
            "skill_version_id",
            name="uq_skill_composition_items_composition_version",
        ),
    )

    composition_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_compositions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    skill_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    config_override_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    composition: Mapped[SkillComposition] = relationship(back_populates="items")


class ProjectComposition(IdentityMixin, Base):
    """Project がどの SkillComposition を有効化しているかの関係。"""

    __tablename__ = "project_compositions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "composition_id",
            name="uq_project_compositions_project_composition",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    composition_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_compositions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enabled_by: Mapped[UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectSkillVersion(IdentityMixin, Base):
    """Project が明示的に有効化した精確 PUBLISHED SkillVersion の関係 (docs/04 §5.1)。

    有効化は権限を与えない。ここが答えるのは「その SkillVersion がこの Project から見えるか」
    だけで、見えた版が実行できるかは資源束縛と Run の権限 snapshot が別に決める
    (docs/11 §6.0 の「先に可視、後に就緒」)。

    version 単位で束縛するのは、Run が精確版を凍結し platform が PUBLISHED を自動切替しない
    ためである。Skill 単位で有効化すると、新版の発行が Project の実行内容を黙って変えてしまう。

    停用は行を消さずに `disabled_at` を立てる。監査上「いつ誰が実行面を広げ、いつ畳んだか」は
    残す必要があり、削除するとその事実ごと失われる。
    """

    __tablename__ = "project_skill_versions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "skill_version_id",
            name="uq_project_skill_versions_project_version",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    skill_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    enabled_by: Mapped[UUID] = mapped_column(nullable=False)
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Run(IdentityMixin, TimestampMixin, Base):
    """不変 snapshot と現在 status を保持する論理的な一回の実行。"""

    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "task_id",
            "idempotency_key",
            name="uq_runs_project_task_idempotency",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    task_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    trigger_type: Mapped[str] = mapped_column(String(32), default="immediate", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.QUEUED.value, nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    task_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    permission_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    selected_sources_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    limits_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    attempts: Mapped[list[RunAttempt]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunAttempt.created_at"
    )
    segments: Mapped[list[RunSegment]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunSegment.segment_no"
    )


class RunSkillSnapshot(IdentityMixin, Base):
    """Run が実行する published SkillVersion と Manifest checksum の不変 binding。"""

    __tablename__ = "run_skill_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", "sort_order", name="uq_run_skill_snapshots_run_order"),
        UniqueConstraint("run_id", "skill_version_id", name="uq_run_skill_snapshots_run_version"),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    skill_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    config_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    manifest_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunSegment(IdentityMixin, TimestampMixin, Base):
    """初回起動またはユーザー応答で始まる一つの連続作業区間。"""

    __tablename__ = "run_segments"
    __table_args__ = (
        UniqueConstraint("run_id", "segment_no", name="uq_run_segments_run_no"),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    segment_no: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_type: Mapped[str] = mapped_column(
        String(32), default=RunSegmentTrigger.INITIAL.value, nullable=False
    )
    trigger_ref: Mapped[UUID | None] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(
        String(32), default=RunSegmentStatus.CREATED.value, nullable=False
    )
    objective_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checkpoint_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    continuation_mode: Mapped[str] = mapped_column(
        String(16), default=SessionContinuationMode.INITIAL.value, nullable=False
    )
    parent_agent_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="RESTRICT"), index=True
    )
    instruction_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_task_brief_snapshots.id", ondelete="RESTRICT"), unique=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[Run] = relationship(back_populates="segments")


class AgentTaskBriefSnapshot(IdentityMixin, Base):
    """一つの Segment へ渡した AgentTaskBrief 本文と checksum を不変保存する。"""

    __tablename__ = "agent_task_brief_snapshots"

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_segment_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_segments.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    brief_version: Mapped[str] = mapped_column(String(64), nullable=False)
    brief_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunAttempt(IdentityMixin, TimestampMixin, Base):
    """再試行や復旧を上書きせず記録する Run の実行試行。"""

    __tablename__ = "run_attempts"
    __table_args__ = (
        UniqueConstraint(
            "run_segment_id", "attempt_no", name="uq_run_attempts_segment_attempt"
        ),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), index=True)
    # 歴史 Run は null のまま読み、Release G 以降の新規 Attempt だけが明示 Segment を持つ。
    run_segment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("run_segments.id", ondelete="RESTRICT"), index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=RunAttemptStatus.CREATED.value, nullable=False
    )
    worker_id: Mapped[str | None] = mapped_column(String(128))
    lease_token_hash: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    run: Mapped[Run] = relationship(back_populates="attempts")


class RunInputSnapshot(IdentityMixin, Base):
    """全 input の準備世代と、workspace から独立した file 検証回执を保持する。"""

    __tablename__ = "run_input_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_run_input_snapshots_run"),
        CheckConstraint("status IN ('PREPARING', 'READY')", name="ck_run_input_snapshot_status"),
        CheckConstraint(
            "total_files >= 0 AND total_bytes >= 0", name="ck_run_input_snapshot_totals"
        ),
        CheckConstraint(
            "(status = 'PREPARING' AND completed_at IS NULL AND tree_checksum IS NULL) OR "
            "(status = 'READY' AND completed_at IS NOT NULL AND tree_checksum IS NOT NULL)",
            name="ck_run_input_snapshot_completion",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    prepared_by_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    source_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    files_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    tree_checksum: Mapped[str | None] = mapped_column(String(71), nullable=True)
    total_files: Mapped[int] = mapped_column(Integer, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunBudgetAccount(IdentityMixin, TimestampMixin, Base):
    """Run 全体の凍結 policy と共通勘定。古い Run へゼロ残高を補填しない。"""

    __tablename__ = "run_budget_accounts"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_run_budget_accounts_run"),
        CheckConstraint("consumed_turns >= 0 AND reserved_turns >= 0", name="budget_account_turns"),
        CheckConstraint(
            "(consumed_cost_nanos IS NULL AND reserved_cost_nanos IS NULL) OR "
            "(consumed_cost_nanos IS NOT NULL AND reserved_cost_nanos IS NOT NULL "
            "AND consumed_cost_nanos >= 0 AND reserved_cost_nanos >= 0)",
            name="budget_account_cost",
        ),
        CheckConstraint("row_version >= 1", name="budget_account_version"),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False)
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    policy_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    limits_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    consumed_turns: Mapped[Decimal] = mapped_column(Numeric(38, 0), nullable=False)
    reserved_turns: Mapped[Decimal] = mapped_column(Numeric(38, 0), nullable=False)
    consumed_cost_nanos: Mapped[Decimal | None] = mapped_column(Numeric(38, 0))
    reserved_cost_nanos: Mapped[Decimal | None] = mapped_column(Numeric(38, 0))
    block_code: Mapped[str | None] = mapped_column(String(64))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False)


class RunBudgetReservation(IdentityMixin, TimestampMixin, Base):
    """一回の主/子実行に与えた上界と未決占用を、元の実行世代に結び付ける。"""

    __tablename__ = "run_budget_reservations"
    __table_args__ = (
        UniqueConstraint("run_id", "execution_key", name="uq_run_budget_execution"),
        UniqueConstraint("invocation_id", name="uq_run_budget_invocation_id"),
        UniqueConstraint("id", "invocation_id", name="uq_run_budget_reservation_invocation"),
        Index("ix_run_budget_group", "run_id", "group_key"),
        CheckConstraint(
            "(invocation_id IS NULL AND invocation_json IS NULL "
            "AND invocation_checksum IS NULL) OR "
            "(invocation_id IS NOT NULL AND invocation_json IS NOT NULL "
            "AND invocation_checksum IS NOT NULL)",
            name="budget_invocation_binding",
        ),
        CheckConstraint(
            "invocation_start_owner_hash IS NULL OR "
            "(invocation_id IS NOT NULL AND invocation_json IS NOT NULL "
            "AND invocation_checksum IS NOT NULL "
            "AND invocation_start_owner_hash ~ '^sha256:[0-9a-f]{64}$')",
            name="budget_invocation_start_owner",
        ),
        CheckConstraint(
            "status IN ('RESERVED', 'START_INTENT', 'SETTLED', 'RELEASED')",
            name="budget_reservation_status",
        ),
        CheckConstraint(
            "granted_turns > 0 AND reserved_turns >= 0 AND consumed_turns >= 0 "
            "AND reserved_turns <= granted_turns", name="budget_reservation_turns",
        ),
        CheckConstraint(
            "(granted_cost_nanos IS NULL AND reserved_cost_nanos IS NULL "
            "AND consumed_cost_nanos IS NULL) OR "
            "(granted_cost_nanos IS NOT NULL AND reserved_cost_nanos IS NOT NULL "
            "AND consumed_cost_nanos IS NOT NULL AND granted_cost_nanos > 0 "
            "AND reserved_cost_nanos >= 0 AND consumed_cost_nanos >= 0 "
            "AND reserved_cost_nanos <= granted_cost_nanos)", name="budget_reservation_cost",
        ),
        CheckConstraint(
            "(status IN ('RESERVED', 'RELEASED') AND start_intent_at IS NULL) OR "
            "(status IN ('START_INTENT', 'SETTLED') AND start_intent_at IS NOT NULL)",
            name="budget_reservation_start",
        ),
        CheckConstraint(
            "status NOT IN ('SETTLED', 'RELEASED') OR "
            "(reserved_turns = 0 AND (reserved_cost_nanos IS NULL OR reserved_cost_nanos = 0))",
            name="budget_reservation_closed",
        ),
        CheckConstraint(
            "status != 'SETTLED' OR (stop_confirmed_at IS NOT NULL AND final_usage_at IS NOT NULL)",
            name="budget_reservation_settlement",
        ),
        CheckConstraint(
            "status != 'RELEASED' OR (consumed_turns = 0 AND "
            "(consumed_cost_nanos IS NULL OR consumed_cost_nanos = 0))",
            name="budget_reservation_unstarted",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_budget_accounts.run_id", ondelete="RESTRICT"), nullable=False
    )
    run_segment_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_segments.id", ondelete="RESTRICT"), nullable=False
    )
    run_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    execution_key: Mapped[str] = mapped_column(String(128), nullable=False)
    group_key: Mapped[str] = mapped_column(String(128), nullable=False)
    group_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    request_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    invocation_id: Mapped[UUID | None] = mapped_column(nullable=True)
    # 未束縛は JSON null ではなく SQL NULL にし、三列の原子的な束縛を検査する。
    invocation_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    invocation_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 旧束縛へ所有者を補造せず、raw token は調整者の一回限りの局部変数に留める。
    invocation_start_owner_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    parent_reservation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("run_budget_reservations.id", ondelete="RESTRICT")
    )
    execution_lease_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    granted_turns: Mapped[Decimal] = mapped_column(Numeric(38, 0), nullable=False)
    reserved_turns: Mapped[Decimal] = mapped_column(Numeric(38, 0), nullable=False)
    consumed_turns: Mapped[Decimal] = mapped_column(Numeric(38, 0), nullable=False)
    granted_cost_nanos: Mapped[Decimal | None] = mapped_column(Numeric(38, 0))
    reserved_cost_nanos: Mapped[Decimal | None] = mapped_column(Numeric(38, 0))
    consumed_cost_nanos: Mapped[Decimal | None] = mapped_column(Numeric(38, 0))
    turns_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_intent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_usage_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconcile_worker_id: Mapped[str | None] = mapped_column(String(128))
    reconcile_token_hash: Mapped[str | None] = mapped_column(String(64))
    reconcile_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunBudgetReceipt(IdentityMixin, Base):
    """正規化用量・停止根拠・異常を追記保存する。終態 RunEvent を再び開かない。"""

    __tablename__ = "run_budget_receipts"
    __table_args__ = (
        UniqueConstraint("reservation_id", "receipt_key", name="uq_run_budget_receipt_key"),
        CheckConstraint(
            "kind IN ('USAGE', 'STOP', 'UNSTARTED', 'UNVERIFIABLE')", name="budget_receipt_kind"
        ),
    )

    reservation_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_budget_reservations.id", ondelete="RESTRICT"), nullable=False
    )
    receipt_key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    reconcile_worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunBudgetObservation(IdentityMixin, Base):
    """元の予約と invocation に原始観察を追記し、消費や停止の根拠には昇格しない。"""

    __tablename__ = "run_budget_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["reservation_id", "invocation_id"],
            ["run_budget_reservations.id", "run_budget_reservations.invocation_id"],
            name="fk_run_budget_observation_invocation",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("reservation_id", "observation_key", name="uq_run_budget_observation_key"),
    )

    reservation_id: Mapped[UUID] = mapped_column(nullable=False)
    invocation_id: Mapped[UUID] = mapped_column(nullable=False)
    observation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    reconcile_worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunEvent(IdentityMixin, Base):
    """SSE replay と監査に利用する Run 内で順序付けられた event。"""

    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), index=True)
    run_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("run_attempts.id", ondelete="RESTRICT"), index=True
    )
    agent_session_id: Mapped[UUID | None] = mapped_column(index=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(Text)


class OutboxMessage(IdentityMixin, Base):
    """Database transaction と同時に保存する非同期配送 message。"""

    __tablename__ = "outbox_messages"

    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    aggregate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    topic: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class AgentSessionTranscript(IdentityMixin, TimestampMixin, Base):
    """SDK transcript の project/session/subpath ごとの追加位置を管理する。"""

    __tablename__ = "agent_session_transcripts"
    __table_args__ = (
        UniqueConstraint(
            "project_key",
            "session_id",
            "subpath",
            name="uq_agent_session_transcripts_key",
        ),
    )

    project_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # Main transcript は空文字を使い、NULL の unique semantics による重複を防ぐ。
    subpath: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    next_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)


class AgentSessionEntry(IdentityMixin, Base):
    """Claude CLI から受信した一つの opaque JSONL entry を不変に保存する。"""

    __tablename__ = "agent_session_entries"
    __table_args__ = (
        UniqueConstraint(
            "transcript_id",
            "sequence",
            name="uq_agent_session_entries_sequence",
        ),
        Index(
            "uq_agent_session_entries_transcript_uuid",
            "transcript_id",
            "entry_uuid",
            unique=True,
            postgresql_where=text("entry_uuid IS NOT NULL"),
        ),
    )

    transcript_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_session_transcripts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_uuid: Mapped[str | None] = mapped_column(String(128))
    entry_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ToolCall(IdentityMixin, TimestampMixin, Base):
    """一つの SDK Tool 呼び出しと脱敏済み監査情報を保持する。"""

    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("run_id", "sdk_tool_use_id", name="uq_tool_calls_run_sdk_use"),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), index=True)
    run_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_attempts.id", ondelete="RESTRICT"), index=True
    )
    agent_session_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    sdk_tool_use_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    capability_version: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    integration_id: Mapped[UUID | None] = mapped_column(index=True)
    arguments_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class PermissionDecision(IdentityMixin, Base):
    """ToolCall に対する自動許可または拒否判断を追加式に保存する。"""

    __tablename__ = "permission_decisions"
    __table_args__ = (
        UniqueConstraint(
            "tool_call_id",
            "decision",
            "request_fingerprint",
            name="uq_permission_decisions_call_decision_request",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), index=True)
    tool_call_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="RESTRICT"), index=True
    )
    policy: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    decided_by: Mapped[UUID | None] = mapped_column(index=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Evidence(IdentityMixin, Base):
    """Tool が取得した事実を再定位できる追加式 Evidence index。"""

    __tablename__ = "evidence"
    __table_args__ = (
        Index(
            "uq_evidence_artifact_ref", "artifact_ref", unique=True,
            postgresql_where=text("artifact_ref IS NOT NULL"),
        ),
        CheckConstraint(
            "(artifact_ref IS NULL AND artifact_bytes IS NULL AND artifact_size IS NULL "
            "AND artifact_mime_type IS NULL AND artifact_path IS NULL) OR "
            "(artifact_ref IS NOT NULL AND artifact_bytes IS NOT NULL "
            "AND artifact_size IS NOT NULL AND artifact_mime_type IS NOT NULL "
            "AND artifact_path IS NOT NULL AND tool_call_id IS NOT NULL "
            "AND artifact_ref ~ '^art_[a-zA-Z0-9_-]+$' "
            "AND evidence_ref ~ '^ev_[a-zA-Z0-9_-]+$' "
            "AND artifact_size BETWEEN 0 AND 1048576 "
            "AND octet_length(artifact_bytes) = artifact_size "
            "AND artifact_mime_type = 'text/plain' "
            "AND content_hash ~ '^sha256:[0-9a-f]{64}$' "
            "AND char_length(artifact_path) BETWEEN 8 AND 4096 "
            "AND artifact_path LIKE 'output/%' AND artifact_path NOT LIKE '%/' "
            "AND artifact_path NOT LIKE '%//%' "
            r"AND artifact_path !~ '(^|/)(\.|\.\.)(/|$)' "
            "AND position(chr(92) in artifact_path) = 0 "
            "AND artifact_path !~ '[[:cntrl:]]')",
            name="artifact_binding",
        ),
    )

    evidence_ref: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), index=True)
    tool_call_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="RESTRICT"), index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_locator: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    snapshot_uri: Mapped[str | None] = mapped_column(String(2048))
    excerpt: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    artifact_ref: Mapped[str | None] = mapped_column(String(64))
    # 通常 detail/effect の Evidence SELECT は添付本文を取得せず、偶発的な lazy load も拒む。
    artifact_bytes: Mapped[bytes | None] = mapped_column(
        LargeBinary, deferred=True, deferred_raiseload=True,
    )
    artifact_size: Mapped[int | None] = mapped_column(Integer)
    artifact_mime_type: Mapped[str | None] = mapped_column(String(255))
    artifact_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentSession(IdentityMixin, TimestampMixin, Base):
    """一つの RunAttempt で活動した SDK session の metadata。"""

    __tablename__ = "agent_sessions"
    # 二つの一意条件はどちらも「Run 骨格の本体は一本」を守るもので、扇出の子には掛けない。
    # 子は親と同じ Attempt を共有し、最大 4 本が同時 ACTIVE になる (計画 §23 P3b / migration 0027)。
    __table_args__ = (
        Index(
            "uq_agent_sessions_primary_run_attempt",
            "run_attempt_id",
            unique=True,
            postgresql_where=text("session_kind = 'PRIMARY'"),
        ),
        Index(
            "uq_agent_sessions_active_run",
            "run_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE' AND session_kind = 'PRIMARY'"),
        ),
        Index(
            "ix_agent_sessions_parent_session_id_kind",
            "parent_session_id",
            "session_kind",
        ),
        CheckConstraint(
            "session_kind IN ('PRIMARY', 'SUBAGENT')",
            name="ck_agent_sessions_session_kind",
        ),
        CheckConstraint(
            "(continuation_mode = 'BRANCH') = (session_kind = 'SUBAGENT')",
            name="ck_agent_sessions_branch_is_subagent",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), index=True)
    run_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_attempts.id", ondelete="RESTRICT"), index=True
    )
    run_segment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("run_segments.id", ondelete="RESTRICT"), index=True
    )
    # SUBAGENT だけ NULL を許す。SDK が session を開く前に落ちた branch に実在しない ID を
    # 与えると、引けるように見えて引けない参照になる (計画 §23 P3b)。PRIMARY の NOT NULL は
    # ck_agent_sessions_primary_has_sdk_session が維持する。
    sdk_session_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    parent_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="RESTRICT"), index=True
    )
    continuation_mode: Mapped[str] = mapped_column(
        String(16), default=SessionContinuationMode.INITIAL.value, nullable=False
    )
    checkpoint_checksum: Mapped[str | None] = mapped_column(String(71))
    engine_options_checksum: Mapped[str | None] = mapped_column(String(71))
    engine: Mapped[str] = mapped_column(String(64), nullable=False)
    session_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    cwd: Mapped[str] = mapped_column(String(4096), nullable=False)
    sdk_version: Mapped[str] = mapped_column(String(32), nullable=False)
    cli_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    cost_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class UserInteraction(IdentityMixin, TimestampMixin, Base):
    """Agent が公開した構造化質問と待機 checkpoint を保持する。"""

    __tablename__ = "user_interactions"
    __table_args__ = (
        Index(
            "uq_user_interactions_open_required_run",
            "run_id",
            unique=True,
            postgresql_where=text("status = 'OPEN' AND required = true"),
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_segment_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_segments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    agent_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    interaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    options_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    required: Mapped[bool] = mapped_column(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=UserInteractionStatus.OPEN.value, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    continuation_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    checkpoint_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checkpoint_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    change_proposal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("change_proposals.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
        index=True,
    )


class EffectPreauthorization(IdentityMixin, TimestampMixin, Base):
    """ADMIN が設定した低 risk effect の精確な事前許可範囲を保持する。"""

    __tablename__ = "effect_preauthorizations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')", name="effect_preauthorizations_status"
        ),
        CheckConstraint(
            "max_risk_level = 'LOW'", name="effect_preauthorizations_low_risk_only"
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    integration_id: Mapped[UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    capability_version: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    max_risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[UUID] = mapped_column(nullable=False, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChangeProposal(IdentityMixin, TimestampMixin, Base):
    """Agent が生成し platform が検証した外部変更候補の不変本文を保持する。"""

    __tablename__ = "change_proposals"
    __table_args__ = (
        UniqueConstraint("proposal_ref", name="uq_change_proposals_ref"),
        UniqueConstraint(
            "run_id", "idempotency_key", name="uq_change_proposals_run_idempotency"
        ),
        CheckConstraint(
            "risk_level IN ('LOW', 'MEDIUM', 'HIGH')", name="change_proposals_risk"
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'APPLYING', "
            "'APPLIED', 'REJECTED', 'STALE', 'FAILED')",
            name="change_proposals_status",
        ),
    )

    proposal_ref: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_segment_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_segments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_attempts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    agent_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    skill_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    target_binding_id: Mapped[UUID] = mapped_column(
        ForeignKey("resource_bindings.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    integration_id: Mapped[UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    effect_intent_key: Mapped[str] = mapped_column(String(128), nullable=False)
    capability_version: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    target_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    preview_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    precondition_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    reversible: Mapped[bool] = mapped_column(nullable=False)
    rollback_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    verification_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    continuation_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    checkpoint_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checkpoint_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class ChangeApproval(IdentityMixin, Base):
    """Proposal version に対する user または preauthorization の一回限りの判断。"""

    __tablename__ = "change_approvals"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_change_approvals_proposal"),
        UniqueConstraint(
            "run_id", "idempotency_key", name="uq_change_approvals_run_idempotency"
        ),
        CheckConstraint(
            "source IN ('USER', 'PREAUTHORIZATION')", name="change_approvals_source"
        ),
        CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')", name="change_approvals_decision"
        ),
    )

    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("change_proposals.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    preauthorization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("effect_preauthorizations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    proposal_version: Mapped[int] = mapped_column(Integer, nullable=False)
    proposal_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EffectExecution(IdentityMixin, TimestampMixin, Base):
    """批准済み Proposal の idempotent apply、lease と read-back 検証を保持する。"""

    __tablename__ = "effect_executions"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_effect_executions_proposal"),
        UniqueConstraint("approval_id", name="uq_effect_executions_approval"),
        CheckConstraint(
            "status IN ('REQUESTED', 'LEASED', 'APPLYING', 'APPLIED', 'STALE', "
            "'FAILED', 'VERIFICATION_FAILED')",
            name="effect_executions_status",
        ),
    )

    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("change_proposals.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    approval_id: Mapped[UUID] = mapped_column(
        ForeignKey("change_approvals.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    tool_call_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="RESTRICT"), nullable=True, unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_version: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    before_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verification_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InteractionResponse(IdentityMixin, Base):
    """UserInteraction に対する一回限りの actor 回答を追加式に保存する。"""

    __tablename__ = "interaction_responses"
    __table_args__ = (
        UniqueConstraint("interaction_id", name="uq_interaction_responses_interaction"),
        UniqueConstraint(
            "run_id", "idempotency_key", name="uq_interaction_responses_run_idempotency"
        ),
    )

    interaction_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_interactions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    actor_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    interaction_version: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunResult(IdentityMixin, Base):
    """検証済み OutcomeEnvelope または歴史的 structured output を一度だけ保存する。"""

    __tablename__ = "run_results"

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, unique=True, index=True
    )
    agent_session_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    output_schema: Mapped[str] = mapped_column(String(512), nullable=False)
    result_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    artifact_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    change_proposal_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    optional_schema_identity_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None]
    needs_review: Mapped[bool] = mapped_column(nullable=False)
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    cost_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    validation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Evaluation(IdentityMixin, Base):
    """不変 RunResult に対する一回の追加式人工評価。"""

    __tablename__ = "evaluations"
    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="rating_range"),
        CheckConstraint(
            "verdict IN ('accurate', 'partially_accurate', 'inaccurate', 'uncertain')",
            name="verdict_value",
        ),
        CheckConstraint(
            "(submission_key IS NULL AND request_hash IS NULL) OR "
            "(submission_key IS NOT NULL AND request_hash IS NOT NULL "
            "AND submission_key <> '00000000-0000-0000-0000-000000000000' "
            "AND request_hash ~ '^sha256:[0-9a-f]{64}$')",
            name="submission_binding",
        ),
        UniqueConstraint(
            "result_id", "user_id", "submission_key", name="uq_evaluations_submission",
        ),
        Index("ix_evaluations_result_created_id", "result_id", "created_at", "id"),
    )

    result_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_results.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    revision_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    submission_key: Mapped[UUID | None] = mapped_column(nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectDocumentUpload(IdentityMixin, Base):
    """PUT 前の原要求と占用を保存し、公開後や清理要求後も帰属と課金量を保持する。

    PENDING は遠端 write の成功/失敗を断定しない。UNCONDITIONAL_V1 は一回の application
    dispatch だけを表し、SDK の wire retry や遅延 PUT の停止を保証しない。
    """

    __tablename__ = "document_upload_intents"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "project_id", "actor_id", "upload_key",
            name="uq_document_upload_intent_request",
        ),
        UniqueConstraint("document_id", name="uq_document_upload_intent_document"),
        UniqueConstraint(
            "storage_namespace_id", "storage_key", name="uq_document_upload_intent_object",
        ),
        UniqueConstraint(
            "id", "document_id", "project_id", name="uq_document_upload_intent_binding",
        ),
        Index(
            "uq_document_upload_intent_pending_path", "project_id", "folder", "name",
            unique=True, postgresql_where=text(
                "state = 'PENDING' AND publication_closed_at IS NULL",
            ),
        ),
        CheckConstraint(
            "upload_key <> '00000000-0000-0000-0000-000000000000' AND "
            "original_request_id <> '00000000-0000-0000-0000-000000000000' AND "
            "original_session_id <> '00000000-0000-0000-0000-000000000000' AND "
            "document_id <> '00000000-0000-0000-0000-000000000000' AND "
            "storage_namespace_id <> '00000000-0000-0000-0000-000000000000'",
            name="non_nil_identities",
        ),
        CheckConstraint("protocol_version = 1", name="protocol_version"),
        CheckConstraint("request_checksum ~ '^sha256:[0-9a-f]{64}$'", name="request_checksum"),
        CheckConstraint(
            "storage_descriptor_checksum ~ '^sha256:[0-9a-f]{64}$'",
            name="storage_descriptor_checksum",
        ),
        CheckConstraint("checksum ~ '^sha256:[0-9a-f]{64}$'", name="checksum"),
        CheckConstraint("name <> '' AND storage_key <> '' AND mime <> ''", name="nonempty_fields"),
        CheckConstraint("write_protocol = 'UNCONDITIONAL_V1'", name="write_protocol"),
        CheckConstraint("size > 0", name="positive_size"),
        CheckConstraint("state IN ('PENDING', 'PUBLISHED')", name="state"),
        CheckConstraint(
            "(state = 'PENDING' AND published_at IS NULL AND cleanup_requested_at IS NULL) OR "
            "(state = 'PUBLISHED' AND published_at IS NOT NULL AND published_at >= created_at "
            "AND (cleanup_requested_at IS NULL OR cleanup_requested_at >= published_at))",
            name="publication",
        ),
        CheckConstraint(
            "publication_closed_at IS NULL OR (state = 'PENDING' AND published_at IS NULL "
            "AND publication_closed_at >= created_at)",
            name="publication_closure",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False,
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    actor_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    upload_key: Mapped[UUID] = mapped_column(nullable=False)
    original_request_id: Mapped[UUID] = mapped_column(nullable=False)
    # Session 保留期間と意図の監査保持を分離し、元の ID だけを凍結する。
    original_session_id: Mapped[UUID] = mapped_column(nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    folder: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_namespace_id: Mapped[UUID] = mapped_column(nullable=False)
    storage_descriptor_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    storage_is_durable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    write_protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    # metadata 削除や一度の object 不在では占用を解放しない。精確な清理証明は別 protocol とする。
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleanup_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # 旧 writer も公開へ進めない制約で守り、占用や原 PENDING 回答は書き換えない。
    publication_closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class ProjectDocumentUploadClosure(IdentityMixin, Base):
    """原 PENDING の公開停止だけを記録し、遠端 PUT の停止や占用解放を表さない。

    原対象は RESTRICT 関連先に保持し、その全事実と今回の要求を別 checksum で固定する。
    """

    __tablename__ = "document_upload_closures"
    __table_args__ = (
        UniqueConstraint("upload_intent_id", name="uq_document_upload_closure_intent"),
        ForeignKeyConstraint(
            ["upload_intent_id", "document_id", "project_id"],
            [
                "document_upload_intents.id", "document_upload_intents.document_id",
                "document_upload_intents.project_id",
            ],
            name="fk_document_upload_closures_upload_intent", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "id <> '00000000-0000-0000-0000-000000000000' AND "
            "upload_intent_id <> '00000000-0000-0000-0000-000000000000' AND "
            "organization_id <> '00000000-0000-0000-0000-000000000000' AND "
            "project_id <> '00000000-0000-0000-0000-000000000000' AND "
            "actor_id <> '00000000-0000-0000-0000-000000000000' AND "
            "upload_key <> '00000000-0000-0000-0000-000000000000' AND "
            "document_id <> '00000000-0000-0000-0000-000000000000' AND "
            "requested_by <> '00000000-0000-0000-0000-000000000000' AND "
            "request_id <> '00000000-0000-0000-0000-000000000000' AND "
            "session_id <> '00000000-0000-0000-0000-000000000000'",
            name="non_nil_identities",
        ),
        CheckConstraint("protocol_version = 1", name="protocol_version"),
        CheckConstraint("actor_id = requested_by", name="original_actor"),
        CheckConstraint("binding_checksum ~ '^sha256:[0-9a-f]{64}$'", name="binding_checksum"),
    )

    upload_intent_id: Mapped[UUID] = mapped_column(nullable=False)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False,
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    actor_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    upload_key: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    requested_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    # Session の保留規則で原閉鎖要求を消さず、秘密ではない元 ID だけを保持する。
    request_id: Mapped[UUID] = mapped_column(nullable=False)
    session_id: Mapped[UUID] = mapped_column(nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectDocumentCleanup(IdentityMixin, Base):
    """目録削除と同じ transaction で原 object と占用を保持する独立清理要求。

    旧文書に upload 履歴を補造しない。DELETE 応答や一度の不在観察を清理完了/解放としない。
    """

    __tablename__ = "document_blob_cleanups"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_document_blob_cleanup_document"),
        ForeignKeyConstraint(
            ["upload_intent_id", "document_id", "project_id"],
            [
                "document_upload_intents.id", "document_upload_intents.document_id",
                "document_upload_intents.project_id",
            ],
            name="fk_document_blob_cleanups_upload_intent", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "document_id <> '00000000-0000-0000-0000-000000000000' AND "
            "request_id <> '00000000-0000-0000-0000-000000000000' AND "
            "session_id <> '00000000-0000-0000-0000-000000000000' AND "
            "uploaded_by <> '00000000-0000-0000-0000-000000000000' AND "
            "storage_namespace_id <> '00000000-0000-0000-0000-000000000000'",
            name="non_nil_identities",
        ),
        CheckConstraint("protocol_version = 1", name="protocol_version"),
        CheckConstraint(
            "(upload_intent_id IS NOT NULL AND source_protocol = 'UPLOAD_INTENT_V1') OR "
            "(upload_intent_id IS NULL AND source_protocol = 'LEGACY_UNVERIFIED')",
            name="source_protocol",
        ),
        CheckConstraint("name <> '' AND storage_key <> '' AND mime <> ''", name="nonempty_fields"),
        CheckConstraint("size > 0", name="positive_size"),
        CheckConstraint("checksum ~ '^sha256:[0-9a-f]{64}$'", name="checksum"),
        CheckConstraint(
            "storage_descriptor_checksum ~ '^sha256:[0-9a-f]{64}$'",
            name="storage_descriptor_checksum",
        ),
        CheckConstraint("created_at >= document_created_at", name="created_at_order"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False,
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    requested_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
    )
    # 目録や旧 Session を消しても清理の帰属を失わないよう、原 ID は独立して保持する。
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    request_id: Mapped[UUID] = mapped_column(nullable=False)
    session_id: Mapped[UUID] = mapped_column(nullable=False)
    upload_intent_id: Mapped[UUID | None] = mapped_column(nullable=True)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    folder: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    uploaded_by: Mapped[UUID] = mapped_column(nullable=False)
    document_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_namespace_id: Mapped[UUID] = mapped_column(nullable=False)
    storage_descriptor_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    storage_is_durable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectDocument(IdentityMixin, Base):
    """Project 内にアップロードした文書の metadata。blob 正文は object storage に置く。

    正本 metadata は PostgreSQL、正文は storage_key の指す object storage に分離する。
    同一 folder 内での name 重複は一意制約で拒否する。新文書の占用は upload intent に残す。
    """

    __tablename__ = "project_documents"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "folder",
            "name",
            name="uq_project_documents_project_folder_name",
        ),
        ForeignKeyConstraint(
            ["upload_intent_id", "id", "project_id"],
            [
                "document_upload_intents.id", "document_upload_intents.document_id",
                "document_upload_intents.project_id",
            ],
            name="fk_project_documents_upload_intent", ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(storage_namespace_id IS NULL AND storage_descriptor_checksum IS NULL "
            "AND storage_is_durable IS NULL) OR "
            "(storage_namespace_id IS NOT NULL AND storage_descriptor_checksum IS NOT NULL "
            "AND storage_is_durable IS NOT NULL "
            "AND storage_namespace_id <> '00000000-0000-0000-0000-000000000000' "
            "AND storage_descriptor_checksum ~ '^sha256:[0-9a-f]{64}$')",
            name="storage_namespace_binding",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    # 旧文書へ架空の原要求を補造せず、新方式の公開文書だけを正確な intent と結ぶ。
    upload_intent_id: Mapped[UUID | None] = mapped_column(nullable=True)
    folder: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    # 旧文書は三列とも NULL のまま保ち、現在の接続先から過去の帰属を推測しない。
    storage_namespace_id: Mapped[UUID | None] = mapped_column(nullable=True)
    storage_descriptor_checksum: Mapped[str | None] = mapped_column(String(71), nullable=True)
    storage_is_durable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    uploaded_by: Mapped[UUID] = mapped_column(nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
class TaskSchedule(IdentityMixin, TimestampMixin, Base):
    """Task の設定と摘要を保持し、発火の原設定と結果は occurrence に分離する。

    configuration_version は入力設定の世代で、tick による row_version と区別する。
    occurrence_protocol=0 の旧行は、摘要だけから過去の発火や正確な残枠を補造しない。
    """

    __tablename__ = "task_schedules"
    __table_args__ = (
        CheckConstraint("kind IN ('ONCE', 'CRON')", name="task_schedules_kind"),
        CheckConstraint(
            "status IN ('ACTIVE', 'PAUSED', 'COMPLETED', 'ERROR', 'ARCHIVED')",
            name="task_schedules_status",
        ),
        # kind ごとに必須 column を DB 側でも固定する。片方だけ入った行は発火時刻を決められない。
        CheckConstraint(
            "(kind = 'CRON' AND cron_expression IS NOT NULL AND run_at IS NULL)"
            " OR (kind = 'ONCE' AND run_at IS NOT NULL AND cron_expression IS NULL)",
            name="task_schedules_kind_fields",
        ),
        CheckConstraint("max_runs IS NULL OR max_runs > 0", name="task_schedules_max_runs"),
        CheckConstraint("run_count >= 0", name="task_schedules_run_count"),
        CheckConstraint("missed_count >= 0", name="task_schedules_missed_count"),
        CheckConstraint("configuration_version >= 1", name="configuration_version"),
        CheckConstraint("occurrence_protocol IN (0, 1)", name="occurrence_protocol"),
        Index(
            "ix_task_schedules_due",
            "status",
            "next_run_at",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    cron_expression: Mapped[str | None] = mapped_column(String(128), nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skill_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    task_key: Mapped[str] = mapped_column(String(200), nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    sources_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=True
    )
    last_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    missed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    # 旧 writer が作る行も 0 に留め、新しい認領門禁を暗黙に通過させない。
    configuration_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    occurrence_protocol: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )


class TaskScheduleOccurrence(IdentityMixin, TimestampMixin, Base):
    """原予定・凍結設定・認領世代・一回の結算を保持し、lease 切れでも監査を残す。"""

    __tablename__ = "task_schedule_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id", "occurrence_at", name="uq_task_schedule_occurrence_identity"
        ),
        UniqueConstraint("run_id", name="uq_task_schedule_occurrence_run"),
        Index(
            "uq_task_schedule_occurrence_pending",
            "schedule_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        CheckConstraint("configuration_version >= 1", name="configuration_version"),
        CheckConstraint("lease_generation >= 1", name="lease_generation"),
        CheckConstraint("attempt_count >= 1", name="attempt_count"),
        CheckConstraint("status IN ('PENDING', 'SETTLED')", name="status"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('RUN_CREATED', 'SKIPPED_OVERLAP', 'FAILED_PRECONDITION')",
            name="outcome",
        ),
        CheckConstraint(
            "(status = 'PENDING' AND run_id IS NULL AND outcome IS NULL AND settled_at IS NULL) OR "
            "(status = 'SETTLED' AND outcome IS NOT NULL AND settled_at IS NOT NULL AND "
            "((outcome = 'RUN_CREATED' AND run_id IS NOT NULL) OR "
            "(outcome IN ('SKIPPED_OVERLAP', 'FAILED_PRECONDITION') AND run_id IS NULL)))",
            name="settlement",
        ),
    )

    schedule_id: Mapped[UUID] = mapped_column(
        ForeignKey("task_schedules.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    skill_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="RESTRICT"), nullable=False
    )
    occurrence_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    configuration_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    snapshot_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"))
    outcome: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(String(512))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FrontendModuleVersion(IdentityMixin, TimestampMixin, Base):
    """生成 FrontendModule の一版を凍結する (計画 §24 M1 / `docs/07` §10)。

    source/lockfile/bundle の hash、Module API version、CSP、build 報告、静的検査結果を丸ごと
    固定する。どれか一つでも変われば別 version——「同じ version なのに中身が違う」を作らないため。

    精確 SkillVersion に束縛し、最新版へ勝手に漂流させない (`docs/07` §13)。回退は
    ProjectComposition が指す SkillVersion を戻すことで起き、この行自体は書き換えない。
    """

    __tablename__ = "frontend_module_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'BUILT', 'PUBLISHED', 'DISABLED')",
            name="frontend_module_versions_status",
        ),
        # bundle が無い状態で PUBLISHED にはできない。配信できない版を「公開済み」と呼ばない。
        CheckConstraint(
            "status IN ('DRAFT', 'DISABLED') OR bundle_hash IS NOT NULL",
            name="frontend_module_versions_bundle_required",
        ),
        UniqueConstraint(
            "skill_version_id",
            "source_hash",
            name="uq_frontend_module_versions_skill_source",
        ),
    )

    skill_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    module_api_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    lockfile_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    bundle_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    # 配信時に必ず付ける CSP。header が落ちると D2 の隔離が消えるため、値も版と一緒に凍結する。
    content_security_policy: Mapped[str] = mapped_column(String(512), nullable=False)
    static_report_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    build_report_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
