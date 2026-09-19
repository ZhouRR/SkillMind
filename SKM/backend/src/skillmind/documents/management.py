"""文書 ID と保存先を維持する目录操作。呼出し元が組織・Project の書込門禁を保持する。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.db.models import ProjectDocument, ProjectDocumentFolder
from skillmind.documents.domain import DocumentConflictError, DocumentNotFoundError
from skillmind.documents.paths import validate_document_path
from skillmind.documents.upload_repository import DocumentUploadRepository
from skillmind.storage import UploadRejectedError


def folder_path(project_id: UUID, value: str) -> str:
    """空 root 以外の正規相対目录を検証する。"""
    folder, _ = validate_document_path(project_id=project_id, folder=value, name="folder")
    if not folder or folder != value or any(p in {".", "..", ""} for p in value.split("/")):
        raise UploadRejectedError("invalid_document_folder", "A canonical folder is required")
    return folder


def parents(path: str) -> set[str]:
    """目录自身と親目录を列挙する。"""
    parts = path.split("/") if path else []
    return {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}


class DocumentManagementRepository:
    """同一 transaction で文書・目录の一括変更を検証して適用する。"""

    def __init__(self, session: AsyncSession) -> None:
        """呼出元が認可 gate を保持している session を受け取る。"""
        self.session = session

    async def folders(self, project_id: UUID) -> list[str]:
        """空目录と文書から投影された目录を併合する。"""
        explicit = await self.session.scalars(
            select(ProjectDocumentFolder.path).where(ProjectDocumentFolder.project_id == project_id)
        )
        implicit = await self.session.scalars(
            select(ProjectDocument.folder).where(
                ProjectDocument.project_id == project_id, ProjectDocument.deleted_at.is_(None)
            )
        )
        return sorted({p for path in [*explicit, *implicit] for p in parents(path)})

    async def apply(
        self,
        project_id: UUID,
        action: str,
        changes: list[dict[str, Any]],
        source: str | None,
        target: str | None,
        *,
        actor_id: UUID,
    ) -> None:
        """原 path の比較と衝突検査に成功した場合だけ一括変更する。blob は変更しない。"""
        if action not in {
            "CREATE_FOLDER",
            "MOVE",
            "MOVE_FOLDER",
            "DELETE_FOLDER",
            "TRASH",
            "RESTORE",
        }:
            raise ValueError("Unsupported document action")
        documents = list(
            await self.session.scalars(
                select(ProjectDocument)
                .where(ProjectDocument.project_id == project_id)
                .with_for_update()
            )
        )
        directories = list(
            await self.session.scalars(
                select(ProjectDocumentFolder)
                .where(ProjectDocumentFolder.project_id == project_id)
                .with_for_update()
            )
        )
        active_documents = [d for d in documents if d.deleted_at is None]
        known = {
            p
            for path in [*(d.path for d in directories), *(d.folder for d in active_documents)]
            for p in parents(path)
        }
        if action in {"TRASH", "RESTORE"}:
            from skillmind.documents.reference_repository import DocumentReferenceRepository
            from skillmind.runs.history_deletion import require_restore_path

            for change in changes:
                document = next(
                    (d for d in documents if str(d.id) == str(change["document_id"])), None
                )
                if document is None:
                    raise DocumentNotFoundError("Document is not available")
                if (document.folder, document.name) != (
                    change["expected_folder"],
                    change["expected_name"],
                ):
                    raise DocumentConflictError("Document path changed")
                if action == "TRASH" and document.deleted_at is None:
                    await DocumentReferenceRepository(self.session).require_unreferenced(
                        project_id=project_id, document_id=document.id
                    )
                    await require_output_finished(self.session, document)
                    document.deleted_at = datetime.now(UTC)
                    document.deleted_by = actor_id
                    document.deleted_by_run_id = None
                elif action == "RESTORE":
                    await require_restore_path(self.session, document)
                    document.deleted_at = document.deleted_by = document.deleted_by_run_id = None
            return
        documents = active_documents
        reserved = await pending_paths(self.session, project_id)
        if action == "CREATE_FOLDER":
            path = folder_path(project_id, target or "")
            if any(value in parents(path) for value in reserved):
                raise DocumentConflictError("Folder conflicts with a pending upload")
            if path in known or any(
                "/".join(filter(None, (d.folder, d.name))) in parents(path) for d in documents
            ):
                raise DocumentConflictError("Folder already exists")
            self.session.add(ProjectDocumentFolder(id=uuid4(), project_id=project_id, path=path))
            return
        if action in {"MOVE_FOLDER", "DELETE_FOLDER"}:
            source = folder_path(project_id, source or "")
            if source not in known:
                raise DocumentNotFoundError("Folder is not available")

            def inside(path: str) -> bool:
                return path == source or path.startswith(source + "/")

            if any(inside(path) for path in reserved):
                raise DocumentConflictError("Folder contains a pending upload")
            members = [d for d in documents if inside(d.folder)]
            nested = [d for d in directories if inside(d.path)]
            if action == "DELETE_FOLDER":
                if members:
                    raise DocumentConflictError("Folder is not empty")
                for directory in nested:
                    await self.session.delete(directory)
                return
            target = folder_path(project_id, target or "")
            if (
                inside(target)
                or target in known
                or any(
                    value in parents(target) or value.startswith(target + "/") for value in reserved
                )
            ):
                raise DocumentConflictError("Destination folder conflicts")
            changes = [
                dict(
                    document_id=d.id,
                    expected_folder=d.folder,
                    expected_name=d.name,
                    folder=target + d.folder[len(source) :],
                    name=d.name,
                )
                for d in members
            ]
        by_id = {d.id: d for d in documents}
        moved: dict[UUID, tuple[str, str]] = {}
        uploads = DocumentUploadRepository(self.session)
        for change in changes:
            row = by_id.get(UUID(str(change["document_id"])))
            if row is None:
                raise DocumentNotFoundError("Document is not available")
            if row.id in moved or (row.folder, row.name) != (
                change["expected_folder"],
                change["expected_name"],
            ):
                raise DocumentConflictError("Document path changed; refresh before editing")
            folder, name = validate_document_path(
                project_id=project_id, folder=change["folder"], name=change["name"]
            )
            if (folder, name) != (row.folder, row.name) and await uploads.path_reserved(
                project_id=project_id, folder=folder, name=name
            ):
                raise DocumentConflictError("Destination is reserved by an upload")
            moved[row.id] = (folder, name)
        for folder, name in moved.values():
            destination = "/".join(filter(None, (folder, name)))
            if any(
                value == destination
                or value in parents(folder)
                or value.startswith(destination + "/")
                for value in reserved
            ):
                raise DocumentConflictError("Destination conflicts with a pending upload")
        destinations = [moved.get(d.id, (d.folder, d.name)) for d in documents]
        if len(destinations) != len(set(destinations)):
            raise DocumentConflictError("Destination already exists")
        directory_paths = set(known)
        if action == "MOVE_FOLDER":
            assert source is not None and target is not None
            directory_paths = {
                (target + p[len(source) :]) if (p == source or p.startswith(source + "/")) else p
                for p in known
            }
            directory_paths.add(target)
        directory_paths.update(p for folder, _ in destinations for p in parents(folder))
        if any(
            "/".join(filter(None, (folder, name))) in directory_paths
            for folder, name in destinations
        ):
            raise DocumentConflictError("A file and folder cannot share a path")
        # 相互 swap は一意制約の中間衝突を避けるため明示的に拒否する。
        occupied = {(d.folder, d.name): d.id for d in documents}
        if any(path in occupied and occupied[path] != doc_id for doc_id, path in moved.items()):
            raise DocumentConflictError("Destination already exists")
        for doc_id, (folder, name) in moved.items():
            by_id[doc_id].folder, by_id[doc_id].name = folder, name
        if action == "MOVE_FOLDER":
            assert source is not None and target is not None
            for directory in nested:
                directory.path = folder_path(project_id, target + directory.path[len(source) :])
            if not nested:
                self.session.add(
                    ProjectDocumentFolder(id=uuid4(), project_id=project_id, path=target)
                )


async def require_output_finished(session: AsyncSession, document: ProjectDocument) -> None:
    """稼働中の生成元が利用する公開成果を削除しない。"""
    from skillmind.db.models import ProjectDocumentEffectUpload, Run
    from skillmind.documents.domain import DocumentInUseError
    from skillmind.runs.history_deletion import RunHistoryConflict, require_finished

    if document.effect_upload_id is None:
        return
    run = await session.scalar(
        select(Run)
        .join(ProjectDocumentEffectUpload, ProjectDocumentEffectUpload.run_id == Run.id)
        .where(ProjectDocumentEffectUpload.id == document.effect_upload_id)
    )
    if run is None:
        raise DocumentInUseError("Original execution cannot be verified")
    try:
        await require_finished(session, run)
    except RunHistoryConflict as error:
        raise DocumentInUseError("Original execution still requires reconciliation") from error


async def path_conflicts(
    session: AsyncSession, project_id: UUID, folder: str, name: str, excluding: UUID | None = None
) -> bool:
    """upload・移動・復元が共通の file/目录名前空間を使う。"""
    documents = await session.scalars(
        select(ProjectDocument).where(
            ProjectDocument.project_id == project_id, ProjectDocument.deleted_at.is_(None)
        )
    )
    path = "/".join(filter(None, (folder, name)))
    directories = await session.scalars(
        select(ProjectDocumentFolder.path).where(ProjectDocumentFolder.project_id == project_id)
    )
    if any(path in parents(directory) for directory in directories):
        return True
    for document in documents:
        if document.id == excluding:
            continue
        existing = "/".join(filter(None, (document.folder, document.name)))
        if existing == path or existing in parents(folder) or path in parents(document.folder):
            return True
    return False


async def pending_paths(session: AsyncSession, project_id: UUID) -> list[str]:
    """公開前のファイルを目录操作で別の名前空間に重ねない。"""
    from skillmind.db.models import ProjectDocumentEffectUpload, ProjectDocumentUpload

    uploads = await session.scalars(
        select(ProjectDocumentUpload).where(
            ProjectDocumentUpload.project_id == project_id,
            ProjectDocumentUpload.state == "PENDING",
            ProjectDocumentUpload.publication_closed_at.is_(None),
        )
    )
    effects = await session.scalars(
        select(ProjectDocumentEffectUpload).where(
            ProjectDocumentEffectUpload.project_id == project_id,
            ProjectDocumentEffectUpload.state != "PUBLISHED",
        )
    )
    return ["/".join(filter(None, (row.folder, row.name))) for row in [*uploads, *effects]]
