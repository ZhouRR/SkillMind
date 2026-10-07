"""Project 全体の読取 source が現在ユーザーと共有 Project 認可を使うことを検証する。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from skillmind.documents.domain import DocumentNotFoundError
from skillmind.documents.source import DatabaseProjectDocumentSource
from skillmind.projects.domain import ProjectNotFoundError
from skillmind.projects.repository import ProjectRepository
from skillmind.storage import InMemoryFileStorage


@pytest.mark.parametrize("failure", [None, "disabled", "membership"])
async def test_library_source_authorizes_current_user_and_project(monkeypatch, failure):
    """Run 当時の ADMIN を信用せず、現在資格で共有の同 Project/組織/所属検査へ渡す。"""
    user_id, project_id, organization_id = uuid4(), uuid4(), uuid4()
    user = SimpleNamespace(id=user_id, organization_id=organization_id, email="reader@example.test",
                           display_name="Reader", system_role="USER",
                           status="DISABLED" if failure == "disabled" else "ACTIVE")
    session = AsyncMock()
    session.get.return_value = user
    session.__aenter__.return_value = session
    shared = AsyncMock(side_effect=ProjectNotFoundError("Project not found")
                       if failure == "membership" else None)
    monkeypatch.setattr(ProjectRepository, "get_accessible", shared)
    source = DatabaseProjectDocumentSource(Mock(return_value=session),
                                          file_storage=InMemoryFileStorage())
    if failure is None:
        await source.authorize_reader(project_id=project_id, user_id=user_id)
        actor = shared.await_args.kwargs["actor"]
        assert actor.user_id == user_id and actor.organization_id == organization_id
        assert actor.system_role == "USER"
        assert shared.await_args.kwargs["project_id"] == project_id
    else:
        with pytest.raises(DocumentNotFoundError, match="not accessible"):
            await source.authorize_reader(project_id=project_id, user_id=user_id)
        if failure == "disabled":
            shared.assert_not_awaited()
