"""Integration、SecretReference と持続 ResourceBinding の domain 契約を定義する。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AnyUrl

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.core.redaction import find_sensitive_key
from skillmind.core.secret_crypto import EncryptedSecret

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_ENVIRONMENT_LOCATOR_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")

# MANAGED は外部 locator を持たず密文を DB に保持するため、locator 列には固定 sentinel を入れる。
MANAGED_SECRET_LOCATOR = "managed"
# API 経由で受け取る MANAGED 明文の上限(byte)。API key/token 想定で短く抑える。
_MANAGED_SECRET_MAX_BYTES = 8192
_VERSIONED_CAPABILITY_PATTERN = re.compile(
    r"^[a-z][a-z0-9_.-]*/[vV][0-9][a-zA-Z0-9_.-]*$"
)

# Scope list の予約 token。管理者が「全許可」を明示的に付与した場合だけ現れる。
# 空 list(黙示の全許可)とは区別し、fail closed の既定は維持する。
SCOPE_WILDCARD = "*"

# 承認済み書き込みの落とし方 (計画 §20 R5 / D6)。
# - direct (既定): 承認後に既定 branch (`default_revision`) へ直接 commit する。ChangeProposal の
#   承認が唯一かつ十分な闸門である、という運用判断に基づく既定値。
# - branch: 予約 namespace の新規 branch を作る。保護 branch 運用や PR 評審を挟みたい Integration
#   が明示的に選ぶ。
# どちらでも force は使わず、CAS (git は fast-forward、svn は out-of-date 拒否) は共通に効く。
REPOSITORY_WRITE_MODE_BRANCH = "branch"
REPOSITORY_WRITE_MODE_DIRECT = "direct"
REPOSITORY_WRITE_MODES = frozenset({REPOSITORY_WRITE_MODE_BRANCH, REPOSITORY_WRITE_MODE_DIRECT})
REPOSITORY_WRITE_MODE_DEFAULT = REPOSITORY_WRITE_MODE_DIRECT

# 承認済み書き込みが作れる branch の予約 namespace (計画 §20)。Integration は config の
# `write_branch_prefix` でこの内側へさらに狭められるが、外へは広げられない。
REPOSITORY_WRITE_BRANCH_PREFIX = "skillmind/"
_BRANCH_PREFIX_PATTERN = re.compile(r"^skillmind/[A-Za-z0-9][A-Za-z0-9._/-]{0,110}$")

# Repository Provider が実際に接続できる URI scheme。実装 (§19 W4 の git/svn client) が
# 扱えない scheme を登録時に弾き、就緒度が「配線済み Provider を反映する」規約 (§19 W1) を
# scheme 粒度でも守る。ssh 系は鍵管理を platform が持たないため受理しない。
REPOSITORY_URI_SCHEMES: Mapping[str, frozenset[str]] = {
    "git": frozenset({"http", "https", "file"}),
    "svn": frozenset({"http", "https", "svn", "file"}),
}


class IntegrationStatus(StrEnum):
    """Integration が新規 binding/effect に利用できるかを表す。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class SecretResolver(StrEnum):
    """Worker が Secret を解決する方法。"""

    ENVIRONMENT = "ENVIRONMENT"
    FILE = "FILE"
    # MANAGED は platform が KEK で封入した密文を DB から解決する。明文は API を一度通るだけ。
    MANAGED = "MANAGED"


class ResourceBindingLevel(StrEnum):
    """ResourceBinding の override 優先層。"""

    PROJECT_DEFAULT = "PROJECT_DEFAULT"
    TASK = "TASK"
    RUN = "RUN"


class IntegrationNotFoundError(LookupError):
    """Project 境界内に Integration が存在しないことを表す。"""


class SecretReferenceNotFoundError(LookupError):
    """Project 境界内に SecretReference が存在しないことを表す。"""


class ResourceBindingNotFoundError(LookupError):
    """Project 境界内に ResourceBinding が存在しないことを表す。"""


class IntegrationConflictError(ValueError):
    """名前、revision または既存状態が command と競合したことを表す。"""


class IntegrationValidationError(ValueError):
    """Integration または binding が登録済み Provider 境界に違反したことを表す。"""


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    """Platform が認識する Provider と提供可能 capability の固定定義。"""

    kind: str
    provider: str
    capabilities: frozenset[str]
    write_capabilities: frozenset[str]
    requires_secret: bool
    # 宣言済みだが Runtime 実装がまだ配線されていない Provider は installed=False。就緒度は
    # 「catalog に載っている」ではなく「実際に実行できる」を答えねばならず、宣言だけで RUNNABLE
    # を出すと preflight/実行で `Tool Provider is not installed` に至る (計画 §19 W1)。svn の
    # 実クライアントは §19 W4 で実装され、その時点で installed=True へ切り替える。
    installed: bool


PROVIDER_DEFINITIONS: dict[str, ProviderDefinition] = {
    "postgres": ProviderDefinition(
        kind="other",
        provider="postgres",
        capabilities=frozenset({"database.read/v1", "database.write/v1"}),
        write_capabilities=frozenset({"database.write/v1"}),
        requires_secret=True,
        installed=True,
    ),
    "mcp": ProviderDefinition(
        kind="other",
        provider="mcp",
        capabilities=frozenset({"mcp.read/v1"}),
        write_capabilities=frozenset(),
        requires_secret=False,
        installed=True,  # resource 読取 Provider のみ。遠端 tools の実行権は付与しない。
    ),
    "redmine": ProviderDefinition(
        kind="issue",
        provider="redmine",
        capabilities=frozenset({"issue.read/v1", "issue.update/v1"}),
        write_capabilities=frozenset({"issue.update/v1"}),
        requires_secret=True,
        installed=True,
    ),
    "git": ProviderDefinition(
        kind="repository",
        provider="git",
        # repository.write/v1 は §20 R1 で登録。Integration が明示的に宣言したときだけ、その
        # Project で代码変更提案を apply できる (宣言しなければ提案は検証段階で閉じる)。
        capabilities=frozenset({"repository.read/v1", "repository.write/v1"}),
        write_capabilities=frozenset({"repository.write/v1"}),
        # 匿名 clone の読取だけなら不要。push は凭据必須だが、それは write capability 側の
        # EffectProviderDefinition が要求する (capability 粒度の判断を provider 粒度へ潰さない)。
        requires_secret=False,
        installed=True,
    ),
    "svn": ProviderDefinition(
        kind="repository",
        provider="svn",
        # §20 R4b で書き込み Provider を配線したため write も宣言できる。branch の置き場は
        # 平台固定の約定 (`branches/skillmind/`) であり、仓库布局を推測しない。
        capabilities=frozenset({"repository.read/v1", "repository.write/v1"}),
        write_capabilities=frozenset({"repository.write/v1"}),
        requires_secret=True,
        # §19 W4 で `svn` command 実装 (`SvnCommandRepositoryClient`) を配線したため True。
        installed=True,
    ),
}

REGISTERED_WRITE_CAPABILITIES = frozenset(
    capability
    for definition in PROVIDER_DEFINITIONS.values()
    for capability in definition.write_capabilities
)


def _installed_provider_capabilities() -> dict[str, frozenset[str]]:
    """実装が配線済みの Provider だけを能力→Provider 名で索引する。

    候補 (`ProjectResourceCandidate.provider`) と同じ語彙 (Integration provider 名) で返し、
    就緒度側が候補の provider をそのまま照合できるようにする。未 installed の Provider は
    一切寄与しないため、svn だけを束ねた repository 資源は AVAILABLE にならない。
    """

    index: dict[str, set[str]] = {}
    for definition in PROVIDER_DEFINITIONS.values():
        if not definition.installed:
            continue
        for capability in definition.capabilities:
            index.setdefault(capability, set()).add(definition.provider)
    return {capability: frozenset(providers) for capability, providers in index.items()}


# 能力→installed Provider 名。document など Integration 外の Provider は配線側 (api/main.py) で
# 合流させる。ここは Integration Provider だけを正本として持つ。
INSTALLED_PROVIDER_CAPABILITIES: Mapping[str, frozenset[str]] = _installed_provider_capabilities()


@dataclass(frozen=True, slots=True)
class CreateSecretReferenceCommand:
    """SecretReference の作成 command。

    ``secret_value`` は MANAGED のときだけ渡す一過性の明文で、repository が即座に KEK 封入し、
    平文のまま永続化・返却・ログ化しない。ENVIRONMENT/FILE では常に None とする。
    """

    project_id: UUID
    name: str
    provider: str
    resolver: SecretResolver
    locator: str
    key_version: str
    created_by: UUID
    # repr から除外し、例外 traceback や debug 出力へ明文が混入する経路を塞ぐ。
    secret_value: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class StoredSecretReference:
    """公開可能な SecretReference read model。locator は意図的に含めない。"""

    secret_reference_id: UUID
    project_id: UUID
    name: str
    provider: str
    resolver: SecretResolver
    key_version: str
    status: IntegrationStatus
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None


@dataclass(frozen=True, slots=True)
class ResolvedSecretReference:
    """Worker 内だけで使用する解決用 SecretReference。

    ``managed_material`` は MANAGED のとき DB から読み出した密文(未復号)で、復号は Worker の
    ``DeploymentSecretResolver`` が KEK と AAD を使って行う。ENVIRONMENT/FILE では None。
    """

    secret_reference_id: UUID
    project_id: UUID
    provider: str
    resolver: SecretResolver
    locator: str
    key_version: str
    status: IntegrationStatus
    # 密文でも repr へ出さない。debug 出力から暗号材料を収集される面を最小化する。
    managed_material: EncryptedSecret | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class CreateIntegrationCommand:
    """登録済み Provider instance を Project に追加する command。"""

    project_id: UUID
    name: str
    kind: str
    provider: str
    capabilities: tuple[str, ...]
    scope: dict[str, Any]
    config: dict[str, Any]
    secret_reference_id: UUID | None
    created_by: UUID


@dataclass(frozen=True, slots=True)
class StoredIntegration:
    """Secret locator と接続設定正文を除いた Integration read model。"""

    integration_id: UUID
    project_id: UUID
    name: str
    kind: str
    provider: str
    status: IntegrationStatus
    revision: int
    capabilities: tuple[str, ...]
    scope: dict[str, Any]
    config_keys: tuple[str, ...]
    secret_reference_id: UUID | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None


@dataclass(frozen=True, slots=True)
class ResolvedIntegration:
    """Worker/束縛解決層だけが使用する非機密 config 付き Integration snapshot。"""

    integration_id: UUID
    project_id: UUID
    name: str
    kind: str
    provider: str
    status: IntegrationStatus
    revision: int
    capabilities: tuple[str, ...]
    scope: dict[str, Any]
    config: dict[str, Any]
    secret_reference_id: UUID | None


@dataclass(frozen=True, slots=True)
class PutResourceBindingCommand:
    """Project default または Task override を保存する command。"""

    project_id: UUID
    scope_level: ResourceBindingLevel
    scope_key: str
    requirement_key: str
    resource_kind: str
    integration_id: UUID
    capability_version: str
    requested_scope: dict[str, Any]
    created_by: UUID


@dataclass(frozen=True, slots=True)
class StoredResourceBinding:
    """Provider/revision/scope/checksum を固定した ResourceBinding read model。"""

    binding_id: UUID
    project_id: UUID
    scope_level: ResourceBindingLevel
    scope_key: str
    requirement_key: str
    resource_kind: str
    integration_id: UUID | None
    run_id: UUID | None
    source_binding_id: UUID | None
    provider: str
    capability_version: str
    revision: str
    scope: dict[str, Any]
    checksum: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None


@dataclass(frozen=True, slots=True)
class ResolvedRunBinding:
    """Run snapshot 作成前に優先順位を解決した資源束縛。"""

    requirement_key: str
    resource_kind: str
    integration: ResolvedIntegration
    capability_version: str
    scope: dict[str, Any]
    source_binding_id: UUID | None


def validate_secret_reference(command: CreateSecretReferenceCommand) -> None:
    """Secret locator/明文を resolver ごとの安全な形式へ制限する。"""

    _validate_name(command.name, label="SecretReference name")
    if command.provider not in PROVIDER_DEFINITIONS:
        raise IntegrationValidationError("SecretReference Provider is not registered")
    if not 1 <= len(command.key_version) <= 128:
        raise IntegrationValidationError("SecretReference key version is invalid")
    if command.resolver is SecretResolver.MANAGED:
        # MANAGED は明文を必須とし、外部 locator を持たない(sentinel 固定)。明文は
        # repository が即座に KEK 封入するため、ここでは形式と上限だけを検査する。
        if not command.secret_value:
            raise IntegrationValidationError("Managed SecretReference requires a secret value")
        if len(command.secret_value.encode("utf-8")) > _MANAGED_SECRET_MAX_BYTES:
            raise IntegrationValidationError("Managed secret exceeds the size limit")
        if command.locator != MANAGED_SECRET_LOCATOR:
            raise IntegrationValidationError("Managed SecretReference must not carry a locator")
        return
    # ENVIRONMENT/FILE は明文を保存しない。誤って明文を渡す経路を fail closed で塞ぐ。
    if command.secret_value is not None:
        raise IntegrationValidationError("This resolver does not accept an inline secret value")
    if command.resolver is SecretResolver.ENVIRONMENT:
        if _ENVIRONMENT_LOCATOR_PATTERN.fullmatch(command.locator) is None:
            raise IntegrationValidationError("Environment Secret locator is invalid")
        return
    path = PurePosixPath(command.locator)
    if (
        not path.is_absolute()
        or not path.is_relative_to(PurePosixPath("/run/secrets"))
        or ".." in path.parts
        or len(command.locator) > 512
    ):
        raise IntegrationValidationError("File Secret locator must stay under /run/secrets")


def normalize_integration_command(command: CreateIntegrationCommand) -> CreateIntegrationCommand:
    """Provider 定義、capability、scope と非機密 config を検証して安定順へ正規化する。"""

    _validate_name(command.name, label="Integration name")
    definition = PROVIDER_DEFINITIONS.get(command.provider)
    if definition is None or command.kind != definition.kind:
        raise IntegrationValidationError("Integration kind/provider pair is not registered")
    capabilities = tuple(sorted(set(command.capabilities)))
    if not capabilities or any(
        _VERSIONED_CAPABILITY_PATTERN.fullmatch(item) is None for item in capabilities
    ):
        raise IntegrationValidationError("Integration capabilities are invalid")
    if not set(capabilities).issubset(definition.capabilities):
        raise IntegrationValidationError("Integration declares an unregistered capability")
    if definition.requires_secret and command.secret_reference_id is None:
        raise IntegrationValidationError("Integration Provider requires a SecretReference")
    if (
        find_sensitive_key(command.config) is not None
        or find_sensitive_key(command.scope) is not None
    ):
        raise IntegrationValidationError("Integration metadata contains a credential-like field")
    config = _validate_provider_config(command.provider, command.config)
    if (
        command.provider == "git"
        and "repository.write/v1" in capabilities
        and config.get("write_mode") == REPOSITORY_WRITE_MODE_DIRECT
        and str(config.get("default_revision", "")).upper() == "HEAD"
    ):
        # direct は既定 branch そのものへ commit する。`HEAD` は branch 名ではないため具体名を
        # 要求する。既定 mode が direct なので、この取りこぼしは「repository_uri だけ設定した
        # git Integration」で必ず起きる——登録段階で閉じる。読取専用 Integration は対象外。
        raise IntegrationValidationError("Direct write requires an explicit default branch name")
    if (command.provider == "postgres" and "database.write/v1" in capabilities
            and "database.read/v1" not in capabilities):
        raise IntegrationValidationError("Database write requires its observe capability")
    scope = normalize_provider_scope(
        command.provider,
        command.scope,
        write_enabled=bool(set(capabilities) & definition.write_capabilities),
    )
    return CreateIntegrationCommand(
        project_id=command.project_id,
        name=command.name.strip(),
        kind=command.kind,
        provider=command.provider,
        capabilities=capabilities,
        scope=scope,
        config=config,
        secret_reference_id=command.secret_reference_id,
        created_by=command.created_by,
    )


def normalize_provider_scope(
    provider: str, scope: dict[str, Any], *, write_enabled: bool
) -> dict[str, Any]:
    """Provider scope を allowlist 形式へ正規化し、黙示の無制限を拒否する。

    空 list は従来通り拒否し、全許可は Redmine の issue_ids/field_keys に限り
    明示 wildcard(``SCOPE_WILDCARD``)としてのみ受け付ける。write の apply は
    引き続き承認または explicit 事前許可(wildcard 不可)で gate される。
    """

    if provider == "postgres" and write_enabled:
        if set(scope) != {"tables", "write_columns", "operations"}:
            raise IntegrationValidationError(
                "Database write scope requires explicit tables, columns and operations"
            )
        tables = normalize_provider_scope(
            provider, {"tables": scope["tables"]}, write_enabled=False
        )["tables"]
        columns = _unique_strings(
            scope["write_columns"], maximum=1000, key_pattern=False, maximum_length=256
        )
        operations = _unique_strings(scope["operations"], maximum=2, key_pattern=False)
        if (
            not columns
            or not operations
            or set(operations) - {"INSERT", "UPDATE"}
            or any(table.startswith("skillmind_effects.") for table in tables)
            or any(
                len(column.split(".")) != 3
                or ".".join(column.split(".")[:2]) not in tables
                or re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_$]{0,62}", column.split(".")[-1]) is None
                for column in columns
            )
        ):
            raise IntegrationValidationError(
                "Database write scope exceeds explicit table/column/operation limits"
            )
        return {"tables": tables, "write_columns": columns, "operations": operations}
    if provider in {"postgres", "mcp"}:
        key = "tables" if provider == "postgres" else "resource_uris"
        if set(scope) != {key} or write_enabled:
            raise IntegrationValidationError("Read-only resource scope contains unknown fields")
        values = _unique_strings(
            scope[key], maximum=200, key_pattern=False,
            maximum_length=2048 if provider == "mcp" else 256,
        )
        if not values or SCOPE_WILDCARD in values:
            raise IntegrationValidationError("Read-only resource scope requires explicit values")
        if provider == "postgres" and any(
            re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_$]{0,62}\.[a-zA-Z_][a-zA-Z0-9_$]{0,62}", value)
            is None for value in values
        ):
            raise IntegrationValidationError("PostgreSQL tables require schema.table names")
        if provider == "mcp":
            normalized_uris: list[str] = []
            for value in values:
                try:
                    uri = urlsplit(value)
                    normalized = str(AnyUrl(value))
                except ValueError as error:
                    raise IntegrationValidationError("MCP resource URI is invalid") from error
                if not uri.scheme or uri.username or uri.password or len(normalized) > 2048:
                    raise IntegrationValidationError(
                        "MCP resources require credential-free absolute URIs"
                    )
                normalized_uris.append(normalized)
            values = sorted(set(normalized_uris))
        return {key: values}
    if provider == "redmine":
        if set(scope) - {"issue_ids", "field_keys"}:
            raise IntegrationValidationError("Redmine scope contains unknown fields")
        issue_ids = _scope_values(scope.get("issue_ids"), maximum=500, key_pattern=False)
        field_keys = _scope_values(scope.get("field_keys"), maximum=50, key_pattern=True)
        if not issue_ids:
            raise IntegrationValidationError("Redmine scope requires explicit issue_ids")
        if write_enabled and not field_keys:
            raise IntegrationValidationError(
                "Redmine write scope requires explicit issue_ids and field_keys"
            )
        return {"issue_ids": issue_ids, "field_keys": field_keys}
    if provider in {"git", "svn"}:
        if set(scope) - {"paths", "revisions"}:
            raise IntegrationValidationError("Repository scope contains unknown fields")
        paths = _unique_strings(scope.get("paths"), maximum=200, key_pattern=False)
        revisions = _unique_strings(scope.get("revisions"), maximum=100, key_pattern=False)
        if not paths:
            raise IntegrationValidationError("Repository scope requires at least one path")
        if any(path.startswith("/") or ".." in PurePosixPath(path).parts for path in paths):
            raise IntegrationValidationError("Repository scope path is invalid")
        # "*" は platform 全体の予約 token。repository 側は literal path と区別できないため、
        # subset 判定との意味の食い違いを避けるべく登録自体を拒否する。
        if SCOPE_WILDCARD in paths or SCOPE_WILDCARD in revisions:
            raise IntegrationValidationError("Repository scope does not support the wildcard token")
        return {"paths": paths, "revisions": revisions}
    raise IntegrationValidationError("Integration Provider is not registered")


def scope_values_allow(allowed: list[str] | tuple[str, ...], candidate: str) -> bool:
    """Scope の値 list が candidate を許可するかを判定する唯一の membership 実装。

    ``SCOPE_WILDCARD`` を含む list は管理者が明示付与した全許可。空 list は何も許可しない。
    """

    return SCOPE_WILDCARD in allowed or candidate in allowed


def ensure_explicit_scope(scope: dict[str, Any]) -> None:
    """無人経路(事前許可など)の scope が wildcard を含まないことを強制する。

    docs/06 の「明示的低 risk 事前許可は scope を固定する」を守る唯一の検査点。
    """

    if any(
        isinstance(values, list) and SCOPE_WILDCARD in values
        for values in scope.values()
    ):
        raise IntegrationValidationError(
            "Preauthorization scope must enumerate explicit values"
        )


def path_within_scope(path: str, scope_paths: Sequence[str]) -> bool:
    """Repository path が scope path のいずれかと同一か、その配下かを判定する。

    物化・live 読取・書き込み提案がすべて同じ包含判定を使うための単一実装。単純な前方一致に
    しないのは、`src-vendor` を `src` 配下と誤認しないため。
    """

    if not path or path.startswith("/") or "\\" in path:
        return False
    candidate = PurePosixPath(path)
    if ".." in candidate.parts or "." in candidate.parts:
        return False
    for allowed in scope_paths:
        base = PurePosixPath(allowed)
        if candidate == base or candidate.is_relative_to(base):
            return True
    return False


def scope_is_subset(*, requested: dict[str, Any], allowed: dict[str, Any]) -> bool:
    """Array allowlist だけから成る requested scope が Integration scope 内かを判定する。

    ``SCOPE_WILDCARD`` を含む allowed は当該 key の任意値を許可する。requested 側の
    wildcard は allowed も wildcard の場合だけ許し、全許可の勝手な自己拡大を防ぐ。
    """

    if set(requested) != set(allowed):
        return False
    for key, requested_value in requested.items():
        allowed_value = allowed.get(key)
        if not isinstance(requested_value, list) or not isinstance(allowed_value, list):
            return False
        if SCOPE_WILDCARD in allowed_value:
            continue
        if SCOPE_WILDCARD in requested_value:
            return False
        if not set(requested_value).issubset(set(allowed_value)):
            return False
    return True


def binding_checksum(
    *,
    project_id: UUID,
    scope_level: ResourceBindingLevel,
    scope_key: str,
    requirement_key: str,
    resource_kind: str,
    integration_id: UUID | None,
    provider: str,
    capability_version: str,
    revision: str,
    scope: dict[str, Any],
) -> str:
    """ResourceBinding の permission-sensitive 本文へ canonical checksum を付ける。"""

    return f"sha256:{sha256_hex(canonical_json({
        'project_id': str(project_id),
        'scope_level': scope_level.value,
        'scope_key': scope_key,
        'requirement_key': requirement_key,
        'resource_kind': resource_kind,
        'integration_id': str(integration_id) if integration_id else None,
        'provider': provider,
        'capability_version': capability_version,
        'revision': revision,
        'scope': scope,
    }))}"


def _validate_provider_config(provider: str, config: dict[str, Any]) -> dict[str, Any]:
    """Provider の非機密 connection metadata を最小 allowlist で検証する。"""

    if provider == "postgres":
        if set(config) != {"host", "port", "database", "username", "sslmode"}:
            raise IntegrationValidationError("PostgreSQL config contains invalid fields")
        for key in ("host", "database", "username"):
            value = config[key]
            if not isinstance(value, str) or not 1 <= len(value) <= 253 or any(
                character.isspace() or ord(character) < 32 for character in value
            ):
                raise IntegrationValidationError("PostgreSQL connection field is invalid")
        if any(character in config["host"] for character in "/\\@?#"):
            raise IntegrationValidationError("PostgreSQL host must not contain a connection URI")
        port = config["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise IntegrationValidationError("PostgreSQL port is invalid")
        if not isinstance(config["sslmode"], str) or config["sslmode"] not in {
            "disable", "require", "verify-full",
        }:
            raise IntegrationValidationError("PostgreSQL TLS mode is invalid")
        return dict(config)
    if provider == "mcp":
        if set(config) != {"server_url", "transport"} or config["transport"] != "streamable_http":
            raise IntegrationValidationError("MCP requires Streamable HTTP connection fields")
        value = config["server_url"]
        if not isinstance(value, str) or not 1 <= len(value) <= 2048:
            raise IntegrationValidationError("MCP server URL is invalid")
        try:
            parsed = urlsplit(value)
            # 不正な port は属性を読む時点で ValueError になる。
            _ = parsed.port
        except ValueError as error:
            raise IntegrationValidationError("MCP server URL is invalid") from error
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or any(
            (parsed.username, parsed.password, parsed.query, parsed.fragment)
        ) or any(character.isspace() or ord(character) < 32 for character in value):
            raise IntegrationValidationError("MCP server URL contains forbidden components")
        return {"server_url": value, "transport": "streamable_http"}
    if provider == "redmine":
        if set(config) != {"base_url"}:
            raise IntegrationValidationError("Redmine config requires only base_url")
        base_url = config.get("base_url")
        if not isinstance(base_url, str) or not 1 <= len(base_url) <= 2048:
            raise IntegrationValidationError("Redmine base_url is invalid")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise IntegrationValidationError("Redmine base_url must be an absolute HTTP URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise IntegrationValidationError("Redmine base_url contains forbidden components")
        return {"base_url": base_url.rstrip("/")}
    if provider in {"git", "svn"}:
        allowed = {
            "repository_uri",
            "default_revision",
            "write_mode",
            "write_branch_prefix",
            "forge_kind",
            "forge_api_base_url",
            "forge_project",
        }
        if set(config) - allowed or "repository_uri" not in config:
            raise IntegrationValidationError("Repository config is invalid")
        uri = config.get("repository_uri")
        revision = config.get("default_revision", "HEAD")
        prefix = config.get("write_branch_prefix")
        mode = config.get("write_mode", REPOSITORY_WRITE_MODE_DEFAULT)
        if not isinstance(uri, str) or not 1 <= len(uri) <= 2048:
            raise IntegrationValidationError("Repository URI is invalid")
        if not isinstance(revision, str) or not 1 <= len(revision) <= 128:
            raise IntegrationValidationError("Repository revision is invalid")
        parsed = urlsplit(uri)
        if parsed.scheme not in REPOSITORY_URI_SCHEMES[provider] or not (
            parsed.netloc or parsed.path
        ):
            raise IntegrationValidationError("Repository URI scheme is not supported")
        # URI 内の user:password は Secret 管理を迂回し、config として平文で残る。凭据は
        # SecretReference だけが運ぶ。query/fragment も接続先を曖昧にするため拒否する。
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise IntegrationValidationError("Repository URI contains forbidden components")
        normalized_forge = _validate_forge_config(provider, config)
        if mode not in REPOSITORY_WRITE_MODES:
            raise IntegrationValidationError("Repository write mode is not supported")
        normalized = {
            "repository_uri": uri,
            "default_revision": revision,
            "write_mode": mode,
            **normalized_forge,
        }
        if prefix is not None:
            # 書き込み先 branch を platform 予約 namespace の内側でさらに狭める任意設定
            # (計画 §20 R4)。予約外へ広げる指定は受け付けない——狭める方向にしか効かせない。
            if (
                not isinstance(prefix, str)
                or not prefix.startswith(REPOSITORY_WRITE_BRANCH_PREFIX)
                or _BRANCH_PREFIX_PATTERN.fullmatch(prefix) is None
            ):
                raise IntegrationValidationError(
                    "Repository write branch prefix must narrow the skillmind/ namespace"
                )
            normalized["write_branch_prefix"] = prefix
        return normalized
    raise IntegrationValidationError("Integration Provider is not registered")


def _validate_forge_config(provider: str, config: dict[str, Any]) -> dict[str, str]:
    """PR/MR を開くための forge 設定を検証する (計画 §20 R4)。

    三つ揃っているか、一つも無いかのどちらかだけを受理する。部分設定を許すと「PR を開く設定の
    つもりが開かない」状態が生まれ、利用者は原因を追えない。host 名から種別を推測しないのも
    同じ理由で、外れたときの結果が「別の場所へ書きに行く」になる。
    """

    keys = {"forge_kind", "forge_api_base_url", "forge_project"}
    present = {key for key in keys if config.get(key) is not None}
    if not present:
        return {}
    if present != keys or provider != "git":
        raise IntegrationValidationError(
            "Forge configuration requires kind, API base URL and project on a git Integration"
        )
    kind = config["forge_kind"]
    api_base_url = config["forge_api_base_url"]
    project = config["forge_project"]
    if not isinstance(kind, str) or kind not in {"github", "gitlab"}:
        raise IntegrationValidationError("Forge kind is not supported")
    if not isinstance(api_base_url, str) or not 1 <= len(api_base_url) <= 2048:
        raise IntegrationValidationError("Forge API base URL is invalid")
    parsed = urlsplit(api_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise IntegrationValidationError("Forge API base URL must be an absolute HTTP URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise IntegrationValidationError("Forge API base URL contains forbidden components")
    if (
        not isinstance(project, str)
        or not 1 <= len(project) <= 512
        or any(ord(character) < 32 for character in project)
    ):
        raise IntegrationValidationError("Forge project identifier is invalid")
    return {
        "forge_kind": kind,
        "forge_api_base_url": api_base_url.rstrip("/"),
        "forge_project": project,
    }


def _validate_name(value: str, *, label: str) -> None:
    """公開表示名から制御文字と空白だけの値を除外する。"""

    if not 1 <= len(value.strip()) <= 200 or any(ord(character) < 32 for character in value):
        raise IntegrationValidationError(f"{label} is invalid")


def _scope_values(value: Any, *, maximum: int, key_pattern: bool) -> list[str]:
    """Wildcard を許す scope list。wildcard を含む場合は正準形 ``["*"]`` へ畳む。

    個別値と wildcard の混在は意味的に wildcard と等価なため、checksum と subset
    判定が単一表現になるよう畳み込む。空 list はそのまま返し呼び出し側で拒否させる。
    """

    if not isinstance(value, list) or len(value) > maximum:
        raise IntegrationValidationError("Integration scope list is invalid")
    if any(item == SCOPE_WILDCARD for item in value):
        return [SCOPE_WILDCARD]
    return _unique_strings(value, maximum=maximum, key_pattern=key_pattern)


def _unique_strings(
    value: Any, *, maximum: int, key_pattern: bool, maximum_length: int = 256,
) -> list[str]:
    """Bounded string allowlist を重複なしの安定順へ正規化する。"""

    if not isinstance(value, list) or len(value) > maximum:
        raise IntegrationValidationError("Integration scope list is invalid")
    if any(
        not isinstance(item, str)
        or not 1 <= len(item) <= maximum_length
        or any(ord(character) < 32 for character in item)
        or (key_pattern and _KEY_PATTERN.fullmatch(item) is None)
        for item in value
    ):
        raise IntegrationValidationError("Integration scope value is invalid")
    return sorted(set(value))
