"""API 例外を共通 Problem Details response へ変換する。"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ProblemException(Exception):
    """Application error を安定した公開 Problem Details へ変換するための例外。"""

    def __init__(self, *, status: int, title: str, detail: str, code: str) -> None:
        """公開可能な status、説明、安定 code を保持する。"""

        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail
        self.code = code


def problem_response(
    *,
    request: Request,
    status: int,
    title: str,
    detail: str,
    code: str,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    """公開 error contract に準拠した Problem Details response を生成する。"""

    body: dict[str, Any] = {
        "type": f"https://projectmind.local/problems/{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
        "request_id": getattr(request.state, "request_id", None),
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


async def problem_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Application の ProblemException を公開 error contract へ変換する。"""

    if not isinstance(exc, ProblemException):
        raise exc
    return problem_response(
        request=request,
        status=exc.status,
        title=exc.title,
        detail=exc.detail,
        code=exc.code,
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI の HTTPException を公開 error contract へ正規化する。"""

    if not isinstance(exc, HTTPException):
        raise exc
    return problem_response(
        request=request,
        status=exc.status_code,
        title="Request failed",
        detail=str(exc.detail),
        code="http_error",
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Request validation error を field 単位の情報付きで返す。"""

    if not isinstance(exc, RequestValidationError):
        raise exc
    return problem_response(
        request=request,
        status=422,
        title="Validation failed",
        detail="The request did not satisfy the API contract.",
        code="validation_error",
        errors=[
            {
                "location": [str(part) for part in error["loc"]],
                "message": error["msg"],
                "kind": error["type"],
            }
            for error in exc.errors()
        ],
    )
