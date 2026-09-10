"""API 例外を共通 Problem Details response へ変換する。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# Auth tag と login path の middleware が必ず付ける非 cache 応答の公開 metadata。
NO_STORE_PROBLEM_HEADERS: dict[str, Any] = {
    "Cache-Control": {"required": True, "schema": {"type": "string", "const": "no-store"}},
    "X-Request-ID": {"required": True, "schema": {"type": "string", "minLength": 1}},
}

# 配備時の作業 directory に依存させない。正本との完全一致を契約 test で守り、
# 各 route が同義の Problem body を別々に定義することを防ぐ。
PROBLEM_DETAILS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://schemas.skillmind.local/errors/problem/v1.schema.json",
    "title": "Skillmind Problem Details v1",
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "title", "status", "detail", "instance", "code", "request_id"],
    "properties": {
        "type": {"type": "string", "format": "uri"},
        "title": {"type": "string", "minLength": 1},
        "status": {"type": "integer", "minimum": 400, "maximum": 599},
        "detail": {"type": "string"},
        "instance": {"type": "string", "pattern": "^/"},
        "code": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
        "request_id": {"type": ["string", "null"]},
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["location", "message", "kind"],
                "properties": {
                    "location": {"type": "array", "items": {"type": "string"}},
                    "message": {"type": "string"},
                    "kind": {"type": "string"},
                },
            },
        },
    },
}


def problem_openapi_response(
    description: str, *, headers: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """実際の Problem media type と既存 v1 Schema を独立した応答定義へ投影する。"""

    return {
        "description": description,
        "content": {"application/problem+json": {"schema": deepcopy(PROBLEM_DETAILS_SCHEMA)}},
        "headers": deepcopy(dict(headers or {})),
    }


class ProblemException(Exception):
    """Application error を安定した公開 Problem Details へ変換するための例外。"""

    def __init__(
        self,
        *,
        status: int,
        title: str,
        detail: str,
        code: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """公開可能な status、説明、安定 code を保持する。"""

        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail
        self.code = code
        self.headers = dict(headers or {})


def problem_response(
    *,
    request: Request,
    status: int,
    title: str,
    detail: str,
    code: str,
    errors: list[dict[str, Any]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """公開 error contract に準拠した Problem Details response を生成する。"""

    body: dict[str, Any] = {
        "type": f"https://skillmind.local/problems/{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
        "request_id": getattr(request.state, "request_id", None),
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(
        body,
        status_code=status,
        media_type="application/problem+json",
        headers=headers,
    )


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
        headers=exc.headers,
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
