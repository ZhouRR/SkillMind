"""配備上限と共有段階認可を批准済み Provider registry へ束縛する。"""

from __future__ import annotations

from functools import partial

from skillmind.agent.repository_client import (
    GitWritableRepositoryClient,
    SvnWritableRepositoryClient,
)
from skillmind.documents.library import DOCUMENT_LIBRARY_PROVIDER, DOCUMENT_WRITE_CAPABILITY
from skillmind.effects.catalog import resolve_effect_capability
from skillmind.effects.database_provider import DatabaseWriteProvider
from skillmind.effects.database_write import (
    DATABASE_WRITE_CAPABILITY,
    DATABASE_WRITE_PROVIDER_VERSION,
)
from skillmind.effects.document_provider import DocumentWriteProvider
from skillmind.effects.document_service import DocumentEffectService
from skillmind.effects.forge import UrllibForgeTransport
from skillmind.effects.postgres_write import PostgresDatabaseWriteSource
from skillmind.effects.provider import (
    EffectProvider,
    EffectProviderDefinition,
    EffectProviderRegistry,
)
from skillmind.effects.redmine import create_redmine_effect_provider
from skillmind.effects.release import ExecutionFeatures
from skillmind.effects.repository_effect import (
    GitRepositoryWriteProvider,
    SvnRepositoryWriteProvider,
)
from skillmind.effects.service import EffectService
from skillmind.integrations.secrets import DeploymentSecretResolver
from skillmind.storage.s3_effect import S3ObjectWriteSource


def create_effect_provider_registry(
    *,
    features: ExecutionFeatures,
    effect_service: EffectService,
    secret_resolver: DeploymentSecretResolver,
    git_client: GitWritableRepositoryClient,
    svn_client: SvnWritableRepositoryClient,
    document_service: DocumentEffectService | None = None,
    document_source: S3ObjectWriteSource | None = None,
) -> EffectProviderRegistry:
    """API readiness と同じ capability 上限、catalog の原 version でだけ登録する。"""

    implementations: list[tuple[str, str, EffectProvider]] = []
    if features.deferred:
        implementations.extend(
            [
                ("issue.update/v1", "redmine", create_redmine_effect_provider()),
                (
                    "repository.write/v1",
                    "git",
                    GitRepositoryWriteProvider(git_client, forge_transport=UrllibForgeTransport()),
                ),
                ("repository.write/v1", "svn", SvnRepositoryWriteProvider(svn_client)),
            ]
        )
    if features.database_writes:
        implementations.append(
            (
                DATABASE_WRITE_CAPABILITY,
                "postgres",
                DatabaseWriteProvider(
                    source=PostgresDatabaseWriteSource(),
                    authorize=partial(
                        effect_service.authorize_effect_step,
                        provider_version=DATABASE_WRITE_PROVIDER_VERSION,
                        secret_resolver=secret_resolver,
                    ),
                ),
            )
        )
    if features.document_writes:
        if document_service is None or document_source is None:
            raise ValueError("Document writes require the configured service and storage source")
        implementations.append((
            DOCUMENT_WRITE_CAPABILITY, DOCUMENT_LIBRARY_PROVIDER,
            DocumentWriteProvider(service=document_service, source=document_source),
        ))
    return EffectProviderRegistry(
        tuple(
            EffectProviderDefinition(
                capability_version=capability,
                provider=provider,
                provider_version=resolve_effect_capability(capability).provider_versions[provider],
                implementation=implementation,
                requires_secret=capability != DOCUMENT_WRITE_CAPABILITY,
                supervised=resolve_effect_capability(capability).staged_authorization,
            )
            for capability, provider, implementation in implementations
        )
    )
