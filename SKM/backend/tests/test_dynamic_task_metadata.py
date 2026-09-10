"""Dynamic Task Contract phase の system metadata を検証する。"""

from __future__ import annotations

import pytest

from skillmind.api.main import create_app
from skillmind.api.routes import meta


@pytest.mark.asyncio
async def test_meta_exposes_generic_published_task_scope() -> None:
    """Metadata が 業務固有 固定 task ではなく published Skill task を公開する。"""

    response = await meta()

    assert response.phase == "dynamic task contract"
    assert response.task == "published-skill-task"
    assert response.ingress == "existing-traefik"


def test_openapi_contains_meta_route_without_legacy_create_run() -> None:
    """OpenAPI は generic metadata と task-runs POST だけを新規 Run 入口として公開する。"""

    paths = create_app().openapi()["paths"]

    assert "get" in paths["/api/v1/meta"]
    assert "post" in paths["/api/v1/projects/{project_id}/task-runs"]
    assert "post" not in paths["/api/v1/projects/{project_id}/runs"]
