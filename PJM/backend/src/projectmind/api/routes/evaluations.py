"""認証済み評価者 identity を保持する追加式 Evaluation API route を提供する。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict, Field

from projectmind.api.auth_dependencies import ProjectReadActor, ProjectWriteActor
from projectmind.api.problems import ProblemException
from projectmind.api.routes.runs import run_not_found_problem
from projectmind.evaluations import (
    CreateEvaluationCommand,
    EvaluationResultNotFoundError,
    EvaluationRevisionProposal,
    EvaluationService,
    EvaluationVerdict,
    InvalidEvaluationRevisionError,
    StoredEvaluation,
)
from projectmind.runs.domain import RunNotFoundError

router = APIRouter()


class EvaluationRevisionRequest(BaseModel):
    """Result field に対する一つの JSON Pointer 修正提案。"""

    model_config = ConfigDict(extra="forbid")

    pointer: str = Field(min_length=1, max_length=512, pattern=r"^/")
    suggested_value: Any
    reason: str = Field(min_length=1, max_length=1000)


class CreateEvaluationRequest(BaseModel):
    """Result を変更せず追加する人工評価 request。"""

    model_config = ConfigDict(extra="forbid")

    rating: int = Field(ge=1, le=5)
    verdict: EvaluationVerdict
    comment: str = Field(default="", max_length=4000)
    revisions: list[EvaluationRevisionRequest] = Field(default_factory=list, max_length=100)


class EvaluationRevisionResponse(BaseModel):
    """AI 原値と人工提案値を並列表示する revision response。"""

    pointer: str
    original_value: Any
    suggested_value: Any
    reason: str


class EvaluationResponse(BaseModel):
    """一件の追加式 Evaluation の公開 response。"""

    evaluation_id: UUID
    result_id: UUID
    run_id: UUID
    user_id: UUID
    rating: int
    verdict: EvaluationVerdict
    comment: str
    revisions: list[EvaluationRevisionResponse]
    created_at: datetime


class EvaluationHistoryResponse(BaseModel):
    """Run Result に追加された Evaluation 履歴 response。"""

    items: list[EvaluationResponse]


@router.post(
    "/projects/{project_id}/runs/{run_id}/evaluations",
    response_model=EvaluationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"description": "Invalid revision"}, 404: {"description": "Run not found"}},
    tags=["evaluations"],
)
async def create_evaluation(
    request: Request,
    project_id: UUID,
    run_id: UUID,
    body: CreateEvaluationRequest,
    actor: ProjectWriteActor,
) -> EvaluationResponse:
    """評価者の実 user identity を保持して Project-scoped Evaluation を追加する。"""

    service: EvaluationService = request.app.state.evaluation_service
    try:
        evaluation = await service.create(
            CreateEvaluationCommand(
                project_id=project_id,
                run_id=run_id,
                user_id=actor.user_id,
                rating=body.rating,
                verdict=body.verdict,
                comment=body.comment,
                revisions=tuple(
                    EvaluationRevisionProposal(
                        pointer=revision.pointer,
                        suggested_value=revision.suggested_value,
                        reason=revision.reason,
                    )
                    for revision in body.revisions
                ),
            )
        )
    except RunNotFoundError as error:
        raise run_not_found_problem(error) from error
    except EvaluationResultNotFoundError as error:
        raise ProblemException(
            status=409,
            title="Result not available",
            detail=str(error),
            code="result_not_available",
        ) from error
    except InvalidEvaluationRevisionError as error:
        raise ProblemException(
            status=400,
            title="Invalid evaluation revision",
            detail=str(error),
            code="invalid_evaluation_revision",
        ) from error
    return _evaluation_response(evaluation)


@router.get(
    "/projects/{project_id}/runs/{run_id}/evaluations",
    response_model=EvaluationHistoryResponse,
    responses={404: {"description": "Run not found"}},
    tags=["evaluations"],
)
async def list_evaluations(
    request: Request,
    project_id: UUID,
    run_id: UUID,
    actor: ProjectReadActor,
) -> EvaluationHistoryResponse:
    """Project access 後に Result の Evaluation 履歴を返す。"""

    del actor
    service: EvaluationService = request.app.state.evaluation_service
    try:
        evaluations = await service.list_for_run(project_id=project_id, run_id=run_id)
    except RunNotFoundError as error:
        raise run_not_found_problem(error) from error
    except EvaluationResultNotFoundError as error:
        raise ProblemException(
            status=409,
            title="Result not available",
            detail=str(error),
            code="result_not_available",
        ) from error
    return EvaluationHistoryResponse(items=[_evaluation_response(item) for item in evaluations])


def _evaluation_response(evaluation: StoredEvaluation) -> EvaluationResponse:
    """Evaluation DTO を raw ORM field を含まない公開 response へ変換する。"""

    return EvaluationResponse(
        evaluation_id=evaluation.evaluation_id,
        result_id=evaluation.result_id,
        run_id=evaluation.run_id,
        user_id=evaluation.user_id,
        rating=evaluation.rating,
        verdict=evaluation.verdict,
        comment=evaluation.comment,
        revisions=[
            EvaluationRevisionResponse(
                pointer=revision.pointer,
                original_value=revision.original_value,
                suggested_value=revision.suggested_value,
                reason=revision.reason,
            )
            for revision in evaluation.revisions
        ],
        created_at=evaluation.created_at,
    )
