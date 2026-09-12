"""SDK 非依存な Skill Interpreter 実行契約、失敗分類、idempotency key を定義する。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Any, Protocol

from skillmind.core.hashing import canonical_json, sha256_hex

# 進行 event(interpret.prompt / interpret.delta 等)を任意の transport へ転送する callback。
# None の場合は完全に無音で、実行結果へ一切影響しない。
InterpretProgressCallback = Callable[[str, Mapping[str, Any]], Awaitable[None]]


class InterpreterCallControl(Protocol):
    """表示通知とは独立し、実 completion の直前と return 観測を管理する。"""

    async def before_call(self, *, feedback: str | None) -> None:
        """全 prompt 準備/通知後に、今回だけの開始資格を取得する。"""

        ...

    async def returned(self) -> None:
        """Transport の return を記録し、停止や結果採用とは区別する。"""

        ...


class InterpreterErrorCode(StrEnum):
    """Interpreter 実行失敗の安定した分類。監査に使い Secret や来源本文は含めない。"""

    UNSAFE_SOURCE = "unsafe_source"
    IDENTITY_MISMATCH = "identity_mismatch"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    EMPTY_RESPONSE = "empty_response"
    INVALID_JSON = "invalid_json"
    TRUNCATED_OUTPUT = "truncated_output"
    STRUCTURED_OUTPUT_UNAVAILABLE = "structured_output_unavailable"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"


class InterpreterExecutionError(Exception):
    """安定 code を持つ分類済み Interpreter 失敗。message に来源値を入れない。"""

    def __init__(self, code: InterpreterErrorCode, message: str | None = None) -> None:
        """失敗 code と、source 値を含まない短い説明を保持する。"""

        super().__init__(message or code.value)
        self.code = code


_REPAIRABLE_CANDIDATE_ERRORS = frozenset(
    {
        InterpreterErrorCode.EMPTY_RESPONSE,
        InterpreterErrorCode.INVALID_JSON,
        InterpreterErrorCode.TRUNCATED_OUTPUT,
    }
)


def candidate_repair_feedback(code: InterpreterErrorCode) -> str | None:
    """候補本文を返さず、一度の完全再生成で直せる生成失敗だけを分類する。

    Provider/timeout/identity/structured-output 能力の失敗は同じ request の即時再生成で解消
    できないため対象外とし、権限境界や upstream 障害を曖昧にしない。
    """

    if code not in _REPAIRABLE_CANDIDATE_ERRORS:
        return None
    return f"candidate_generation:{code.value}"


class SkillInterpreter(Protocol):
    """Frozen request から構造化 interpretation 候補を生成する model 非依存契約。"""

    async def interpret(
        self,
        request: Mapping[str, Any],
        *,
        model: str,
        parameters: Mapping[str, Any],
        validation_feedback: str | None = None,
        on_event: InterpretProgressCallback | None = None,
        control: InterpreterCallControl | None = None,
    ) -> dict[str, Any]:
        """構造化 response mapping を返すか InterpreterExecutionError を送出する。

        on_event は進行の可視化専用で、成功/失敗の判定や永続化内容を変えてはならない。
        control がある場合は completion 直前に必ず認可し、拒否後は model を呼ばない。
        """

        ...


def compute_execution_key(
    request: Mapping[str, Any],
    *,
    model: str,
    parameters: Mapping[str, Any],
    scope_id: str | None = None,
    nonce: str | None = None,
) -> str:
    """Frozen request と model identity から execution key を作る。

    Request には source content hash、static analysis checksum、capability catalog
    checksum、interpreter identity、target contract、lineage が既に含まれるため、
    nonce なしなら同一 source と同一 model/parameter は必ず同じ key を返す (決定的)。
    Organization を跨ぐ同一 content が同じ Redis channel/job ID を共有すると進行 delta が
    漏れるため、multi-tenant 実行では scope_id も identity に含める。既存 offline caller は
    None のまま使える。
    nonce を渡すと実行ごとに一意な key になり、幂等複用を無効化する (ユーザーが明示的に
    起動するモデル解釈は毎回実行し直す方針。uq 制約は key 一意化で満たす)。
    """

    body: dict[str, Any] = {
        "request": _plain(request),
        "model": model,
        "parameters": _plain(parameters),
    }
    if scope_id is not None:
        body["scope_id"] = scope_id
    if nonce is not None:
        body["nonce"] = nonce
    return f"sha256:{sha256_hex(canonical_json(body))}"


def _plain(value: Any) -> Any:
    """Mapping を通常 dict へ再帰変換し、canonical JSON を安定化する。"""

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value
