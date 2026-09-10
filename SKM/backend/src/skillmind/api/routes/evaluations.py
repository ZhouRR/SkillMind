"""認証済み評価者 identity を保持する追加式 Evaluation API route を提供する。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from skillmind.api.auth_dependencies import (
    ProjectReadActor,
    ProjectWriteActor,
    authentication_required_problem,
    csrf_rejected_problem,
    project_archived_problem,
    project_not_found_problem,
    user_access,
)
from skillmind.api.problems import (
    NO_STORE_PROBLEM_HEADERS,
    ProblemException,
    problem_openapi_response,
)
from skillmind.api.routes.runs import run_not_found_problem
from skillmind.auth.sessions import CsrfRejectedError, UnauthorizedSessionError
from skillmind.evaluations import (
    CreateEvaluationCommand,
    EvaluationIntegrityError,
    EvaluationResultMismatchError,
    EvaluationResultNotFoundError,
    EvaluationRevisionProposal,
    EvaluationService,
    EvaluationSubmissionConflictError,
    EvaluationSubmissionNotFoundError,
    EvaluationVerdict,
    InvalidEvaluationCommandError,
    InvalidEvaluationCursorError,
    InvalidEvaluationRevisionError,
    StoredEvaluation,
    StoredEvaluationPage,
    StoredEvaluationSubmission,
)
from skillmind.evaluations.domain import evaluation_request_hash
from skillmind.projects.domain import ProjectArchivedError, ProjectNotFoundError
from skillmind.runs.domain import RunNotFoundError

router = APIRouter(tags=["auth"])
_READ_PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: problem_openapi_response(description, headers=NO_STORE_PROBLEM_HEADERS)
    for code, description in (
        (401, "The original session must remain valid"),
        (404, "The Project or Run is not accessible"),
        (409, "result_not_available"),
        (422, "Invalid request identity or query parameter"),
        (503, "evaluation_record_invalid or evaluation_storage_unavailable"),
    )
}
_WRITE_PROBLEMS: dict[int | str, dict[str, Any]] = {
    **_READ_PROBLEMS,
    400: problem_openapi_response("invalid_evaluation_revision", headers=NO_STORE_PROBLEM_HEADERS),
    403: problem_openapi_response("Origin or CSRF was rejected", headers=NO_STORE_PROBLEM_HEADERS),
    409: problem_openapi_response(
        "result_not_available or project_archived", headers=NO_STORE_PROBLEM_HEADERS,
    ),
    422: problem_openapi_response(
        "Invalid request or invalid_evaluation_request", headers=NO_STORE_PROBLEM_HEADERS,
    ),
}


def _non_nil_identity(value: UUID) -> UUID:
    """新しい原要求と確認だけに非 nil の UUID 契約を適用する。"""

    if value.int == 0:
        raise ValueError("A non-zero UUID is required")
    return value


EvaluationIdentity = Annotated[UUID, AfterValidator(_non_nil_identity), Field(
    json_schema_extra={"not": {"const": "00000000-0000-0000-0000-000000000000"}},
)]


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


class SubmitEvaluationRequest(CreateEvaluationRequest):
    """原内容と明示 Result を一つの actor-scoped submission key に結び付ける。"""

    submission_key: EvaluationIdentity
    result_id: EvaluationIdentity
    rating: int = Field(ge=1, le=5, strict=True)


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


class ConfirmedEvaluationRevisionResponse(EvaluationRevisionResponse):
    """新しい回执で原値の欠落と余分な保存 field を成功に変換しない。"""

    model_config = ConfigDict(extra="forbid")

    pointer: str = Field(min_length=1, max_length=512, pattern=r"^/")
    reason: str = Field(min_length=1, max_length=1000)


class ConfirmedEvaluationResponse(BaseModel):
    """既存の九項目を維持し、新しい回执・ページの厳密な保存形状を定義する。"""

    model_config = ConfigDict(extra="forbid")

    evaluation_id: EvaluationIdentity
    result_id: EvaluationIdentity
    run_id: EvaluationIdentity
    user_id: EvaluationIdentity
    rating: int = Field(ge=1, le=5, strict=True)
    verdict: EvaluationVerdict
    comment: str = Field(max_length=4000)
    revisions: list[ConfirmedEvaluationRevisionResponse] = Field(max_length=100)
    created_at: AwareDatetime = Field(json_schema_extra={
        "pattern": r"(?:Z|[+-][0-9]{2}:[0-9]{2})$",
    })


class EvaluationSubmissionResponse(BaseModel):
    """原要求に対応する評価だけを返し、内部 hash/session/replay 判定は公開しない。"""

    model_config = ConfigDict(extra="forbid")

    project_id: EvaluationIdentity
    run_id: EvaluationIdentity
    submission_key: EvaluationIdentity
    evaluation: ConfirmedEvaluationResponse


class EvaluationPageResponse(BaseModel):
    """一 Result の追加順ページ。総数や複数ページの固定 snapshot は宣言しない。"""

    model_config = ConfigDict(extra="forbid")

    project_id: EvaluationIdentity
    run_id: EvaluationIdentity
    result_id: EvaluationIdentity
    items: list[ConfirmedEvaluationResponse] = Field(max_length=100)
    next_cursor: EvaluationIdentity | None


@router.post(
    "/projects/{project_id}/runs/{run_id}/evaluations",
    response_model=EvaluationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={201: {"headers": NO_STORE_PROBLEM_HEADERS}, **_WRITE_PROBLEMS},
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
    async with _evaluation_errors():
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
            ),
            access=user_access(request, actor),
        )
        return _evaluation_response(evaluation)


@router.get(
    "/projects/{project_id}/runs/{run_id}/evaluations",
    response_model=EvaluationHistoryResponse,
    responses={200: {"headers": NO_STORE_PROBLEM_HEADERS}, **_READ_PROBLEMS},
    tags=["evaluations"],
)
async def list_evaluations(
    request: Request,
    project_id: UUID,
    run_id: UUID,
    actor: ProjectReadActor,
) -> EvaluationHistoryResponse:
    """Project access 後に Result の Evaluation 履歴を返す。"""

    service: EvaluationService = request.app.state.evaluation_service
    async with _evaluation_errors():
        evaluations = await service.list_for_run(
            project_id=project_id, run_id=run_id, access=user_access(request, actor),
        )
        return EvaluationHistoryResponse(items=[_evaluation_response(item) for item in evaluations])


@router.post(
    "/projects/{project_id}/runs/{run_id}/evaluation-submissions",
    response_model=EvaluationSubmissionResponse,
    status_code=201,
    responses={
        200: {"model": EvaluationSubmissionResponse, "description": "Original request replay",
              "headers": NO_STORE_PROBLEM_HEADERS},
        201: {"description": "Original request committed", "headers": NO_STORE_PROBLEM_HEADERS},
        **_WRITE_PROBLEMS,
        409: problem_openapi_response(
            "evaluation_submission_conflict, evaluation_result_mismatch, result_not_available, "
            "or project_archived", headers=NO_STORE_PROBLEM_HEADERS,
        ),
    },
    tags=["evaluations"],
)
async def submit_evaluation(
    request: Request, response: Response, project_id: UUID, run_id: UUID,
    body: SubmitEvaluationRequest, actor: ProjectWriteActor,
) -> EvaluationSubmissionResponse:
    """同 key の完全な原要求だけを再取得し、新しい評価との違いを HTTP に反映する。"""

    service: EvaluationService = request.app.state.evaluation_service
    async with _evaluation_errors():
        command = CreateEvaluationCommand(
            project_id=project_id, run_id=run_id, user_id=actor.user_id,
            rating=body.rating, verdict=body.verdict, comment=body.comment,
            revisions=tuple(EvaluationRevisionProposal(
                pointer=item.pointer, suggested_value=item.suggested_value, reason=item.reason,
            ) for item in body.revisions),
        )
        expected = deepcopy(command)
        submitted = await service.submit(
            command,
            submission_key=body.submission_key, result_id=body.result_id,
            access=user_access(request, actor),
        )
        projected = _submission_response(
            submitted, project_id=project_id, run_id=run_id, result_id=body.result_id,
            submission_key=body.submission_key, user_id=actor.user_id, command=expected,
        )
        response.status_code = 200 if submitted.idempotent_replay else 201
        return projected


@router.get(
    "/projects/{project_id}/runs/{run_id}/evaluation-submissions/{submission_key}",
    response_model=EvaluationSubmissionResponse,
    responses={
        200: {"headers": NO_STORE_PROBLEM_HEADERS}, **_READ_PROBLEMS,
        404: problem_openapi_response(
            "Project/Run inaccessible or evaluation_submission_not_found",
            headers=NO_STORE_PROBLEM_HEADERS,
        ),
        409: problem_openapi_response(
            "evaluation_result_mismatch or result_not_available", headers=NO_STORE_PROBLEM_HEADERS,
        ),
    },
    tags=["evaluations"],
)
async def get_evaluation_submission(
    request: Request, project_id: UUID, run_id: UUID, submission_key: EvaluationIdentity,
    result_id: EvaluationIdentity, actor: ProjectReadActor,
) -> EvaluationSubmissionResponse:
    """同 actor の原 Result/要求だけを確認し、未発見から新規評価を作らない。"""

    service: EvaluationService = request.app.state.evaluation_service
    async with _evaluation_errors():
        submitted = await service.get_submission(
            project_id=project_id, run_id=run_id, submission_key=submission_key,
            result_id=result_id, access=user_access(request, actor),
        )
        return _submission_response(
            submitted, project_id=project_id, run_id=run_id, result_id=result_id,
            submission_key=submission_key, user_id=actor.user_id,
        )


@router.get(
    "/projects/{project_id}/runs/{run_id}/evaluations/page",
    response_model=EvaluationPageResponse,
    responses={
        200: {"headers": NO_STORE_PROBLEM_HEADERS}, **_READ_PROBLEMS,
        400: problem_openapi_response(
            "invalid_evaluation_cursor", headers=NO_STORE_PROBLEM_HEADERS,
        ),
    },
    tags=["evaluations"],
)
async def list_evaluation_page(
    request: Request, project_id: UUID, run_id: UUID, actor: ProjectReadActor,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    after: Annotated[str | None, Query(json_schema_extra={"format": "uuid"})] = None,
) -> EvaluationPageResponse:
    """同 Result の保存 cursor で追加順を継ぎ、部分一覧を全量と呼ばない。"""

    service: EvaluationService = request.app.state.evaluation_service
    async with _evaluation_errors():
        cursor = _cursor_identity(after)
        page = await service.list_page(
            project_id=project_id, run_id=run_id, limit=limit, after=cursor,
            access=user_access(request, actor),
        )
        return _page_response(page, project_id=project_id, run_id=run_id, limit=limit, after=cursor)


def _cursor_identity(value: str | None) -> UUID | None:
    """文字形式と保存 cursor の失敗を同じ静的 400 契約に揃える。"""

    if value is None:
        return None
    try:
        return _non_nil_identity(UUID(value))
    except ValueError as error:
        raise InvalidEvaluationCursorError("Invalid Evaluation cursor") from error


def _confirmed_evaluation_response(evaluation: StoredEvaluation) -> ConfirmedEvaluationResponse:
    """旧九項目の allowlist を再利用し、新しい回执の厳密な形状を検証する。"""

    return ConfirmedEvaluationResponse.model_validate(_evaluation_fields(evaluation))


def _submission_response(
    value: StoredEvaluationSubmission, *, project_id: UUID, run_id: UUID, result_id: UUID,
    submission_key: UUID, user_id: UUID, command: CreateEvaluationCommand | None = None,
) -> EvaluationSubmissionResponse:
    """許可済み URL/actor/原要求と一致する保存回执だけを公開する。"""

    evaluation = value.evaluation
    if (
        (value.project_id, value.run_id, value.submission_key)
        != (project_id, run_id, submission_key)
        or (evaluation.run_id, evaluation.result_id, evaluation.user_id)
        != (run_id, result_id, user_id)
        or type(value.idempotent_replay) is not bool
    ):
        raise EvaluationIntegrityError("Evaluation receipt scope does not match")
    projected = EvaluationSubmissionResponse(
        project_id=value.project_id, run_id=value.run_id, submission_key=value.submission_key,
        evaluation=_confirmed_evaluation_response(evaluation),
    )
    if command is not None:
        saved = CreateEvaluationCommand(
            project_id=value.project_id, run_id=evaluation.run_id, user_id=evaluation.user_id,
            rating=evaluation.rating, verdict=evaluation.verdict, comment=evaluation.comment,
            revisions=tuple(EvaluationRevisionProposal(
                pointer=item.pointer, suggested_value=item.suggested_value, reason=item.reason,
            ) for item in evaluation.revisions),
        )
        try:
            if evaluation_request_hash(
                saved, result_id=result_id, submission_key=submission_key,
            ) != evaluation_request_hash(
                command, result_id=result_id, submission_key=submission_key,
            ):
                raise EvaluationIntegrityError("Evaluation receipt content does not match")
        except (InvalidEvaluationCommandError, InvalidEvaluationRevisionError) as error:
            raise EvaluationIntegrityError("Evaluation receipt content is invalid") from error
    return projected


def _page_response(
    value: StoredEvaluationPage, *, project_id: UUID, run_id: UUID, limit: int,
    after: UUID | None,
) -> EvaluationPageResponse:
    """別 Result・混在・過大ページ・偽 cursor を内容公開前に閉じる。"""

    if (value.project_id, value.run_id) != (project_id, run_id) or len(value.items) > limit:
        raise EvaluationIntegrityError("Evaluation page scope does not match")
    page = EvaluationPageResponse(
        project_id=value.project_id, run_id=value.run_id, result_id=value.result_id,
        items=[_confirmed_evaluation_response(item) for item in value.items],
        next_cursor=value.next_cursor,
    )
    order = [(item.created_at, item.evaluation_id) for item in page.items]
    identities = [item.evaluation_id for item in page.items]
    if (
        any((item.run_id, item.result_id) != (run_id, page.result_id) for item in page.items)
        or len(identities) != len(set(identities))
        or order != sorted(order)
        or after in identities
        or (page.next_cursor is not None and (
            not page.items or len(page.items) != limit
            or page.next_cursor != page.items[-1].evaluation_id
        ))
    ):
        raise EvaluationIntegrityError("Evaluation page content does not match")
    return page


@asynccontextmanager
async def _evaluation_errors() -> AsyncIterator[None]:
    """各入口の認可/保存失敗を共通工場と静的 Problem に揃え、取消は捕捉しない。"""

    try:
        yield
    except UnauthorizedSessionError as error:
        raise authentication_required_problem() from error
    except CsrfRejectedError as error:
        raise csrf_rejected_problem() from error
    except ProjectNotFoundError as error:
        raise project_not_found_problem() from error
    except ProjectArchivedError as error:
        raise project_archived_problem() from error
    except RunNotFoundError as error:
        raise run_not_found_problem(RunNotFoundError("The Run was not found.")) from error
    except EvaluationResultNotFoundError as error:
        raise _evaluation_problem(
            409, "result_not_available", "The Result is not available.",
        ) from error
    except EvaluationResultMismatchError as error:
        raise _evaluation_problem(
            409, "evaluation_result_mismatch", "The request does not match this Result.",
        ) from error
    except EvaluationSubmissionConflictError as error:
        raise _evaluation_problem(
            409, "evaluation_submission_conflict", "The original submission content conflicts.",
        ) from error
    except EvaluationSubmissionNotFoundError as error:
        raise _evaluation_problem(
            404, "evaluation_submission_not_found", "The original submission was not found.",
        ) from error
    except InvalidEvaluationCursorError as error:
        raise _evaluation_problem(
            400, "invalid_evaluation_cursor", "The Evaluation history cursor is invalid.",
        ) from error
    except InvalidEvaluationRevisionError as error:
        raise _evaluation_problem(
            400, "invalid_evaluation_revision", "The Evaluation revision is invalid.",
        ) from error
    except InvalidEvaluationCommandError as error:
        raise _evaluation_problem(
            422, "invalid_evaluation_request", "The Evaluation request is invalid.",
        ) from error
    except (EvaluationIntegrityError, ValidationError) as error:
        raise _evaluation_problem(
            503, "evaluation_record_invalid", "The saved Evaluation could not be verified.",
        ) from error
    except (SQLAlchemyError, TimeoutError, ConnectionError) as error:
        raise _evaluation_problem(
            503, "evaluation_storage_unavailable", "Evaluation storage is unavailable; "
            "an earlier submission may still have committed.",
        ) from error


def _evaluation_problem(status_code: int, code: str, detail: str) -> ProblemException:
    """候補、pointer、保存 identity や接続情報を例外文字列から公開しない。"""

    return ProblemException(
        status=status_code, title="Evaluation request unavailable", detail=detail, code=code,
        headers={"Cache-Control": "no-store"},
    )


def _evaluation_response(evaluation: StoredEvaluation) -> EvaluationResponse:
    """Evaluation DTO を raw ORM field を含まない公開 response へ変換する。"""

    return EvaluationResponse.model_validate(_evaluation_fields(evaluation))


def _evaluation_fields(evaluation: StoredEvaluation) -> dict[str, Any]:
    """旧 response の型変換を経由せず、原九項目の値だけを明示投影する。"""

    return {
        "evaluation_id": evaluation.evaluation_id,
        "result_id": evaluation.result_id,
        "run_id": evaluation.run_id,
        "user_id": evaluation.user_id,
        "rating": evaluation.rating,
        "verdict": evaluation.verdict,
        "comment": evaluation.comment,
        "revisions": [
            {"pointer": revision.pointer, "original_value": revision.original_value,
             "suggested_value": revision.suggested_value, "reason": revision.reason}
            for revision in evaluation.revisions
        ],
        "created_at": evaluation.created_at,
    }
