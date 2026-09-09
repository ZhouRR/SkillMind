"""Login の入口と route で共用する、短期防護の公開 Problem 境界。"""

from __future__ import annotations

from typing import Any

from projectmind.api.problems import (
    NO_STORE_PROBLEM_HEADERS,
    ProblemException,
    problem_openapi_response,
)
from projectmind.auth.login_protection import (
    LoginProtectionUnavailableError,
    LoginRateLimitedError,
)

# Header は生成された秒数だけであり、どの account/IP が制限されたかは公開しない。
LOGIN_PROTECTION_RESPONSES: dict[int | str, dict[str, Any]] = {
    429: problem_openapi_response(
        "Login admission is temporarily limited.",
        headers={
            **NO_STORE_PROBLEM_HEADERS,
            "Retry-After": {
                "required": True,
                "schema": {"type": "integer", "minimum": 1, "maximum": 300},
            },
        },
    ),
    503: problem_openapi_response(
        "Login protection is unavailable; password verification was not attempted.",
        headers=NO_STORE_PROBLEM_HEADERS,
    ),
}


def login_protection_problem(
    error: LoginRateLimitedError | LoginProtectionUnavailableError,
) -> ProblemException:
    """Body 検証前と account 検証中で同じ status/code/期限を返す。"""

    if isinstance(error, LoginRateLimitedError):
        return ProblemException(
            status=429,
            title="Login temporarily limited",
            detail="Too many login attempts. Try again later.",
            code="login_rate_limited",
            headers={"Retry-After": str(error.retry_after_seconds)},
        )
    return ProblemException(
        status=503,
        title="Login temporarily unavailable",
        detail="Login protection is unavailable. Try again later.",
        code="login_protection_unavailable",
    )
