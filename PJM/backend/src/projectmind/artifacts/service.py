"""Artifact の認証後読取を短い database session に閉じ込める。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from projectmind.artifacts.domain import ArtifactContent, ArtifactMetadata
from projectmind.artifacts.repository import ArtifactRepository


class ArtifactService:
    """可変 workspace を参照せず、保存済み原字節と帰属だけを読み出す。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """他の application service と同じ session factory を保持する。"""

        self._session_factory = session_factory

    async def list_metadata(
        self, *, project_id: UUID, run_id: UUID,
    ) -> tuple[ArtifactMetadata, ...]:
        """同 Project/Run の検証済み公開 metadata を返す。"""

        async with self._session_factory() as session:
            return await ArtifactRepository(session).list_metadata(
                project_id=project_id, run_id=run_id,
            )

    async def get_content(
        self, *, project_id: UUID, run_id: UUID, artifact_ref: str,
    ) -> ArtifactContent | None:
        """同じ保存内容の hash と size を repository が照合した後で返す。"""

        async with self._session_factory() as session:
            return await ArtifactRepository(session).get_content(
                project_id=project_id, run_id=run_id, artifact_ref=artifact_ref,
            )
