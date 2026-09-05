"""Active Integration を ResourceRequirement 候補へ投影する catalog を提供する。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.integrations.repository import IntegrationRepository
from projectmind.skills.resource_binding import (
    ProjectResourceCandidate,
    ProjectResourceCatalog,
)


class IntegrationResourceCatalog:
    """Project 内の ACTIVE Integration を Secret/config なしの候補として列挙する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Database session factory を保持する。"""

        self._session_factory = session_factory

    async def candidates(self, *, project_id: UUID) -> tuple[ProjectResourceCandidate, ...]:
        """Project ownership と status を DB で絞り、安定順の候補を返す。"""

        async with self._session_factory() as session:
            integrations = await IntegrationRepository(session).list_integrations(
                project_id=project_id,
                active_only=True,
            )
        return tuple(
            ProjectResourceCandidate(
                key=f"integration:{integration.integration_id}",
                kind=integration.kind,
                provider=integration.provider,
                label=integration.name,
                capabilities=integration.capabilities,
                integration_id=integration.integration_id,
                revision=str(integration.revision),
                scope=integration.scope,
            )
            for integration in integrations
        )


class CompositeProjectResourceCatalog:
    """複数の catalog を結合し、候補 key の衝突を fail closed に拒否する。"""

    def __init__(self, catalogs: tuple[ProjectResourceCatalog, ...]) -> None:
        """少なくとも一つの resource catalog を固定する。"""

        if not catalogs:
            raise ValueError("Composite resource catalog requires at least one catalog")
        self._catalogs = catalogs

    async def candidates(self, *, project_id: UUID) -> tuple[ProjectResourceCandidate, ...]:
        """各 catalog の候補を key 順に統合する。"""

        merged: dict[str, ProjectResourceCandidate] = {}
        for catalog in self._catalogs:
            for candidate in await catalog.candidates(project_id=project_id):
                if candidate.key in merged:
                    raise ValueError("Project resource candidate key is duplicated")
                merged[candidate.key] = candidate
        return tuple(merged[key] for key in sorted(merged))
