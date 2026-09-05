"""認証境界を含む公開 API route を資源別 module から一つの router へ組み立てる。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from projectmind import __version__
from projectmind.api.routes import (
    auth,
    compositions,
    documents,
    effects,
    evaluations,
    integrations,
    projects,
    realtime,
    runs,
    schedules,
    skills,
    users,
)

router = APIRouter(prefix="/api/v1")


class MetaResponse(BaseModel):
    """Web が現在の実装 scope と配備経路を確認するための metadata。"""

    name: str
    version: str
    phase: str
    task: str
    ingress: str


@router.get("/meta", response_model=MetaResponse, tags=["system"])
async def meta() -> MetaResponse:
    """稼働中 version と現在の generic task scope を返す。"""

    return MetaResponse(
        name="ProjectMind",
        version=__version__,
        phase="dynamic task contract",
        task="published-skill-task",
        ingress="existing-traefik",
    )


router.include_router(auth.router)
router.include_router(projects.router)
router.include_router(users.router)
router.include_router(users.language_router)
router.include_router(skills.router)
router.include_router(runs.router)
router.include_router(schedules.router)
router.include_router(evaluations.router)
router.include_router(integrations.router)
router.include_router(documents.router)
router.include_router(effects.router)
router.include_router(compositions.router)
router.include_router(realtime.router)
