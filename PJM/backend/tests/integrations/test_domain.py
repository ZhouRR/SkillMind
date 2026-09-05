"""Integration metadata、Secret locator、scope と capability 境界を検証する。"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from projectmind.integrations.domain import (
    INSTALLED_PROVIDER_CAPABILITIES,
    MANAGED_SECRET_LOCATOR,
    PROVIDER_DEFINITIONS,
    CreateIntegrationCommand,
    CreateSecretReferenceCommand,
    IntegrationValidationError,
    SecretResolver,
    ensure_explicit_scope,
    normalize_integration_command,
    scope_is_subset,
    scope_values_allow,
    validate_secret_reference,
)


def _redmine_command() -> CreateIntegrationCommand:
    """明示 scope と SecretReference を持つ Redmine write Integration を返す。"""

    return CreateIntegrationCommand(
        project_id=uuid4(),
        name="Project tracker",
        kind="issue",
        provider="redmine",
        capabilities=("issue.update/v1", "issue.read/v1"),
        scope={"issue_ids": ["42"], "field_keys": ["status_id", "done_ratio"]},
        config={"base_url": "https://redmine.example.test/"},
        secret_reference_id=uuid4(),
        created_by=uuid4(),
    )


def test_redmine_write_integration_normalizes_exact_scope_and_url() -> None:
    """Write capability は明示 issue/field allowlist と正規化済み origin に固定する。"""

    normalized = normalize_integration_command(_redmine_command())

    assert normalized.capabilities == ("issue.read/v1", "issue.update/v1")
    assert normalized.scope == {
        "issue_ids": ["42"],
        "field_keys": ["done_ratio", "status_id"],
    }
    assert normalized.config == {"base_url": "https://redmine.example.test"}


@pytest.mark.parametrize(
    ("scope", "message"),
    [
        ({"issue_ids": ["42"], "field_keys": []}, "write scope"),
        ({"issue_ids": [], "field_keys": ["status_id"]}, "explicit issue_ids"),
        (
            {"issue_ids": ["42"], "field_keys": ["status_id"], "all_projects": True},
            "unknown fields",
        ),
    ],
)
def test_redmine_write_scope_cannot_be_unbounded(
    scope: dict[str, object], message: str
) -> None:
    """空または未知 field を使った scope 拡張を Integration 作成時に拒否する。"""

    with pytest.raises(IntegrationValidationError, match=message):
        normalize_integration_command(replace(_redmine_command(), scope=scope))


@pytest.mark.parametrize(
    "config",
    [
        {"base_url": "https://user:password@redmine.example.test"},
        {"base_url": "https://redmine.example.test?api_key=hidden"},
        {"base_url": "file:///etc/passwd"},
        {"base_url": "https://redmine.example.test", "api_key": "hidden"},
    ],
)
def test_redmine_config_rejects_credentials_and_non_http_targets(
    config: dict[str, str],
) -> None:
    """Credential-like metadata と非 HTTP target を永続 config に入れない。"""

    with pytest.raises(IntegrationValidationError):
        normalize_integration_command(replace(_redmine_command(), config=config))


def test_git_may_declare_the_registered_write_capability() -> None:
    """§20 R1 で登録した repository.write/v1 は git Integration が明示宣言できる。"""

    command = CreateIntegrationCommand(
        project_id=uuid4(),
        name="Repository",
        kind="repository",
        provider="git",
        capabilities=("repository.read/v1", "repository.write/v1"),
        scope={"paths": ["src"], "revisions": ["main"]},
        config={
            "repository_uri": "https://git.example.test/project/repository.git",
            "default_revision": "main",
        },
        secret_reference_id=None,
        created_by=uuid4(),
    )

    normalized = normalize_integration_command(command)

    assert normalized.capabilities == ("repository.read/v1", "repository.write/v1")
    # 既定の落とし方は direct (計画 §20 D6)。
    assert normalized.config["write_mode"] == "direct"


def test_svn_may_declare_the_registered_write_capability() -> None:
    """§20 R4b で svn の書き込み Provider を配線したため、svn も write を宣言できる。"""

    command = CreateIntegrationCommand(
        project_id=uuid4(),
        name="Repository",
        kind="repository",
        provider="svn",
        capabilities=("repository.read/v1", "repository.write/v1"),
        scope={"paths": ["src"], "revisions": ["HEAD"]},
        config={"repository_uri": "https://svn.example.test/project"},
        secret_reference_id=uuid4(),
        created_by=uuid4(),
    )

    normalized = normalize_integration_command(command)

    assert normalized.capabilities == ("repository.read/v1", "repository.write/v1")
    # svn の `HEAD` は正当な revision 式のため、direct 既定でも登録できる。
    assert normalized.config["write_mode"] == "direct"


def test_repository_write_mode_is_restricted_to_known_values() -> None:
    """未知の write_mode は登録段階で拒否する。"""

    command = CreateIntegrationCommand(
        project_id=uuid4(),
        name="Repository",
        kind="repository",
        provider="git",
        capabilities=("repository.write/v1",),
        scope={"paths": ["src"], "revisions": ["main"]},
        config={
            "repository_uri": "https://git.example.test/project.git",
            "default_revision": "main",
            "write_mode": "force",
        },
        secret_reference_id=None,
        created_by=uuid4(),
    )

    with pytest.raises(IntegrationValidationError, match="write mode"):
        normalize_integration_command(command)


def test_git_direct_write_requires_an_explicit_default_branch() -> None:
    """既定 mode が direct のため、branch 名でない `HEAD` のままでは登録を拒否する。"""

    command = CreateIntegrationCommand(
        project_id=uuid4(),
        name="Repository",
        kind="repository",
        provider="git",
        capabilities=("repository.read/v1", "repository.write/v1"),
        scope={"paths": ["src"], "revisions": ["main"]},
        config={"repository_uri": "https://git.example.test/project.git"},
        secret_reference_id=None,
        created_by=uuid4(),
    )

    with pytest.raises(IntegrationValidationError, match="explicit default branch"):
        normalize_integration_command(command)

    # branch mode を選ぶなら既定 revision は `HEAD` のままでよい。
    normalized = normalize_integration_command(
        replace(
            command,
            config={
                "repository_uri": "https://git.example.test/project.git",
                "write_mode": "branch",
            },
        )
    )
    assert normalized.config["write_mode"] == "branch"


def test_registered_providers_are_installed() -> None:
    """§19 W4 で svn client を配線したため、宣言済み Provider は全て installed である。"""

    assert PROVIDER_DEFINITIONS["svn"].installed is True
    assert PROVIDER_DEFINITIONS["git"].installed is True
    assert PROVIDER_DEFINITIONS["redmine"].installed is True


def test_installed_provider_index_binds_capabilities_to_wired_providers() -> None:
    """installed 索引は配線済み Provider だけを能力へ束ねる。"""

    assert INSTALLED_PROVIDER_CAPABILITIES["repository.read/v1"] == frozenset({"git", "svn"})
    assert INSTALLED_PROVIDER_CAPABILITIES["issue.read/v1"] == frozenset({"redmine"})
    # 全ての値が実際に installed な Provider 名だけで構成される。
    installed_names = {
        definition.provider
        for definition in PROVIDER_DEFINITIONS.values()
        if definition.installed
    }
    for providers in INSTALLED_PROVIDER_CAPABILITIES.values():
        assert providers <= installed_names


@pytest.mark.parametrize(
    ("resolver", "locator"),
    [
        (SecretResolver.ENVIRONMENT, "redmine_token"),
        (SecretResolver.FILE, "/etc/projectmind/token"),
        (SecretResolver.FILE, "/run/secrets/../outside"),
    ],
)
def test_secret_locator_stays_in_deployment_owned_namespaces(
    resolver: SecretResolver, locator: str
) -> None:
    """SecretReference は環境変数 allowlist または /run/secrets 配下だけを指す。"""

    command = CreateSecretReferenceCommand(
        project_id=uuid4(),
        name="Tracker credential",
        provider="redmine",
        resolver=resolver,
        locator=locator,
        key_version="2026-07",
        created_by=uuid4(),
    )

    with pytest.raises(IntegrationValidationError):
        validate_secret_reference(command)


def test_secret_reference_accepts_locator_without_storing_secret_body() -> None:
    """Deployment が管理する locator と key version だけを受理する。"""

    validate_secret_reference(
        CreateSecretReferenceCommand(
            project_id=uuid4(),
            name="Tracker credential",
            provider="redmine",
            resolver=SecretResolver.ENVIRONMENT,
            locator="PROJECTMIND_REDMINE_TOKEN",
            key_version="2026-07",
            created_by=uuid4(),
        )
    )


def _managed_command(**overrides: object) -> CreateSecretReferenceCommand:
    """sentinel locator と明文を持つ有効な MANAGED command を返す。"""

    fields: dict[str, object] = {
        "project_id": uuid4(),
        "name": "Tracker credential",
        "provider": "redmine",
        "resolver": SecretResolver.MANAGED,
        "locator": MANAGED_SECRET_LOCATOR,
        "key_version": "2026-07",
        "created_by": uuid4(),
        "secret_value": "redmine-api-key",
    }
    fields.update(overrides)
    return CreateSecretReferenceCommand(**fields)  # type: ignore[arg-type]


def test_managed_secret_reference_requires_inline_secret_value() -> None:
    """MANAGED は sentinel locator と非空明文を受理する。"""

    validate_secret_reference(_managed_command())

    with pytest.raises(IntegrationValidationError):
        validate_secret_reference(_managed_command(secret_value=None))
    with pytest.raises(IntegrationValidationError):
        validate_secret_reference(_managed_command(secret_value=""))


def test_managed_secret_reference_rejects_external_locator() -> None:
    """MANAGED は外部 locator を持てない(sentinel 固定)。"""

    with pytest.raises(IntegrationValidationError):
        validate_secret_reference(_managed_command(locator="/run/secrets/token"))


def test_managed_secret_value_has_size_limit() -> None:
    """MANAGED 明文は上限を超えると拒否する。"""

    with pytest.raises(IntegrationValidationError):
        validate_secret_reference(_managed_command(secret_value="x" * 9000))


@pytest.mark.parametrize("resolver", [SecretResolver.ENVIRONMENT, SecretResolver.FILE])
def test_deployment_resolvers_reject_inline_secret_value(resolver: SecretResolver) -> None:
    """ENVIRONMENT/FILE は明文を受け取らない(明文は DB に落とさない)。"""

    command = CreateSecretReferenceCommand(
        project_id=uuid4(),
        name="Tracker credential",
        provider="redmine",
        resolver=resolver,
        locator="PROJECTMIND_REDMINE_TOKEN" if resolver is SecretResolver.ENVIRONMENT
        else "/run/secrets/token",
        key_version="2026-07",
        created_by=uuid4(),
        secret_value="should-not-be-here",
    )

    with pytest.raises(IntegrationValidationError):
        validate_secret_reference(command)


def test_redmine_scope_accepts_explicit_wildcard_and_collapses_to_canonical() -> None:
    """明示 wildcard は個別値との混在を正準形 ["*"] へ畳み、write でも受理する。"""

    normalized = normalize_integration_command(
        replace(
            _redmine_command(),
            scope={"issue_ids": ["*", "42"], "field_keys": ["*"]},
        )
    )

    assert normalized.scope == {"issue_ids": ["*"], "field_keys": ["*"]}


def test_repository_scope_rejects_reserved_wildcard_token() -> None:
    """Repository scope は literal path と区別できないため "*" の登録自体を拒否する。"""

    command = CreateIntegrationCommand(
        project_id=uuid4(),
        name="Repository",
        kind="repository",
        provider="git",
        capabilities=("repository.read/v1",),
        scope={"paths": ["*"], "revisions": ["HEAD"]},
        config={"repository_uri": "https://git.example.test/project.git"},
        secret_reference_id=None,
        created_by=uuid4(),
    )

    with pytest.raises(IntegrationValidationError, match="wildcard"):
        normalize_integration_command(command)


def test_scope_subset_treats_wildcard_as_full_grant_without_self_escalation() -> None:
    """Wildcard allowed は任意値を許すが、explicit allowed への wildcard 要求は拒否する。"""

    wildcard = {"issue_ids": ["*"], "field_keys": ["*"]}
    explicit = {"issue_ids": ["42"], "field_keys": ["status_id"]}
    narrowed_empty = {"issue_ids": ["42"], "field_keys": []}

    assert scope_is_subset(requested=explicit, allowed=wildcard)
    assert scope_is_subset(requested=wildcard, allowed=wildcard)
    assert scope_is_subset(requested=narrowed_empty, allowed=wildcard)
    assert not scope_is_subset(requested=wildcard, allowed=explicit)
    assert not scope_is_subset(
        requested={"issue_ids": ["*"], "field_keys": ["status_id"]}, allowed=explicit
    )


def test_scope_values_allow_is_the_single_membership_rule() -> None:
    """空 list は何も許可せず、wildcard list は任意 candidate を許可する。"""

    assert scope_values_allow(["*"], "9999")
    assert scope_values_allow(["42"], "42")
    assert not scope_values_allow(["42"], "43")
    assert not scope_values_allow([], "42")


def test_ensure_explicit_scope_blocks_wildcard_for_unattended_paths() -> None:
    """事前許可など無人経路の scope は wildcard を含んではならない。"""

    ensure_explicit_scope({"issue_ids": ["42"], "field_keys": ["status_id"]})

    with pytest.raises(IntegrationValidationError, match="explicit"):
        ensure_explicit_scope({"issue_ids": ["*"], "field_keys": ["status_id"]})
