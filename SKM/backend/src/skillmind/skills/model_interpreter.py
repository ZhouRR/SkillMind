"""Model completion を構造化 interpretation 候補へ変換する最初の model adapter。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from skillmind.core.json_text import strip_code_fence
from skillmind.skills.interpreter import (
    InterpreterSystemSkillIdentity,
    load_interpreter_system_skill,
)
from skillmind.skills.interpreter_execution import (
    InterpreterCallControl,
    InterpreterErrorCode,
    InterpreterExecutionError,
    InterpretProgressCallback,
)

# 進行 event の prompt 本文はこの長さで切り、SSE payload の肥大化を防ぐ。監査には影響しない。
PROMPT_EVENT_MAX_CHARS = 65_536


class ModelProviderError(Exception):
    """Model transport の provider 側失敗を表す。分類は adapter が行う。"""


class ModelStructuredOutputError(Exception):
    """Provider/SDK が構造化出力を確定できなかったことを表す。"""


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    """一回の model completion の SDK 非依存な結果。"""

    structured_output: Mapping[str, Any] | None
    text: str | None
    truncated: bool


class ModelCompletionClient(Protocol):
    """一回の構造化 completion を返す model transport の最小契約。"""

    async def complete(
        self,
        *,
        system_prompt: str,
        user_message: str,
        response_schema: Mapping[str, Any],
        model: str,
        parameters: Mapping[str, Any],
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> ModelCompletion:
        """System/user prompt と response schema から一回の completion を返す。"""

        ...


class ModelSkillInterpreter:
    """Versioned system Skill prompt と model transport を束ねる最初の model adapter。"""

    def __init__(
        self,
        *,
        completion_client: ModelCompletionClient,
        system_skill_root: Path,
        response_schema: Mapping[str, Any],
        accept_prompt_json: bool = False,
    ) -> None:
        """Transport、system Skill root、response schema を保持する。

        accept_prompt_json は endpoint が Anthropic structured outputs 非対応(例 Kimi の
        json_object)の時だけ、prompt 誘導の text JSON を候補として許可する opt-in。
        """

        self._client = completion_client
        self._system_skill_root = system_skill_root.resolve()
        self._response_schema = dict(response_schema)
        self._accept_prompt_json = accept_prompt_json
        self._identity: InterpreterSystemSkillIdentity | None = None
        self._prompt_text: str | None = None

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
        """Request identity を検証し、model completion を構造化 dict へ復元する。"""

        identity, prompt_text = self._load_system_skill()
        requested = request.get("interpreter")
        if not isinstance(requested, Mapping) or dict(requested) != identity.to_dict():
            # 別 system Skill の request を実行させないため、model 呼び出し前に閉じる。
            raise InterpreterExecutionError(InterpreterErrorCode.IDENTITY_MISMATCH)
        user_message = json.dumps(request, ensure_ascii=False, sort_keys=True)
        if validation_feedback is not None:
            # Platform validator の脱敏済み path/code だけを返し、前回 candidate 本文は再送しない。
            user_message += (
                "\n\nThe previous candidate failed deterministic validation. Return a complete "
                "replacement object, not a patch. Validation feedback: "
                + json.dumps(validation_feedback, ensure_ascii=False)
            )
        if on_event is not None:
            # 実際に model へ渡す prompt を可視化する。切り詰めは表示専用で実行本体へ影響しない。
            await on_event("interpret.prompt", {
                "system_prompt": _clipped(prompt_text),
                "user_message": _clipped(user_message),
                "system_prompt_chars": len(prompt_text),
                "user_message_chars": len(user_message),
                "interpreter": identity.to_dict(),
            })

        async def forward_delta(text: str) -> None:
            """Transport の text delta を進行 event へ転送する。"""

            if on_event is not None:
                await on_event("interpret.delta", {"text": text})

        if control is not None:
            # Redis の prompt 通知が待機しても、古い開始資格で model を呼ばない。
            await control.before_call(feedback=validation_feedback)
        try:
            try:
                completion = await self._client.complete(
                    system_prompt=prompt_text,
                    user_message=user_message,
                    response_schema=self._response_schema,
                    model=model,
                    parameters=parameters,
                    on_text_delta=forward_delta if on_event is not None else None,
                )
            except TimeoutError as error:
                raise InterpreterExecutionError(InterpreterErrorCode.TIMEOUT) from error
            except ModelStructuredOutputError as error:
                raise InterpreterExecutionError(
                    InterpreterErrorCode.STRUCTURED_OUTPUT_UNAVAILABLE
                ) from error
            except ModelProviderError as error:
                raise InterpreterExecutionError(InterpreterErrorCode.PROVIDER_ERROR) from error
        except InterpreterExecutionError:
            if control is not None:
                await control.returned()
            raise
        else:
            if control is not None:
                await control.returned()
        if completion.truncated:
            raise InterpreterExecutionError(InterpreterErrorCode.TRUNCATED_OUTPUT)
        return self._decode(completion)

    def _decode(self, completion: ModelCompletion) -> dict[str, Any]:
        """Structured output を優先し、opt-in 時だけ prompt 誘導の text JSON を受理する。

        既定 (accept_prompt_json=False) は SDK 検証済み structured output のみを受理し、非対応は
        明示的な設定失敗として閉じる (output_format を無視した事実を隠さない)。json_object しか
        無い Anthropic 互換 endpoint (例 Kimi) は structured output を返さず本文 text に JSON を
        置くため、許可時のみ strip_code_fence + json.loads で候補化する。いずれの経路も下流の
        schema 検証 + schema-repair retry が契約を担保する。
        """

        if completion.structured_output is not None:
            return dict(completion.structured_output)
        text = completion.text
        if self._accept_prompt_json and text is not None and text.strip():
            try:
                value = json.loads(strip_code_fence(text))
            except json.JSONDecodeError as error:
                raise InterpreterExecutionError(InterpreterErrorCode.INVALID_JSON) from error
            if not isinstance(value, dict):
                raise InterpreterExecutionError(InterpreterErrorCode.INVALID_JSON)
            return value
        raise InterpreterExecutionError(InterpreterErrorCode.STRUCTURED_OUTPUT_UNAVAILABLE)

    def _load_system_skill(self) -> tuple[InterpreterSystemSkillIdentity, str]:
        """System Skill identity と SKILL.md prompt を一度だけ読み込みキャッシュする。"""

        if self._identity is None or self._prompt_text is None:
            self._identity = load_interpreter_system_skill(
                self._system_skill_root,
                generation_schema=self._response_schema,
            )
            skill_md = (self._system_skill_root / "SKILL.md").read_text(encoding="utf-8")
            contract_path = self._system_skill_root / "references" / "output-contract.md"
            output_contract = (
                contract_path.read_text(encoding="utf-8") if contract_path.is_file() else ""
            )
            # 応答 schema と output contract を system prompt 本文へ埋め込む。structured-output を
            # 無視する endpoint にも、応答 envelope の形状を prompt から明示する。
            self._prompt_text = _compose_system_prompt(
                skill_md, output_contract, self._response_schema
            )
        return self._identity, self._prompt_text


def _compose_system_prompt(
    skill_md: str, output_contract: str, response_schema: Mapping[str, Any]
) -> str:
    """SKILL.md に output contract と応答 schema を連結し、厳密な出力形状を prompt 本文へ固定する。

    structured-output を無視する endpoint にも応答 envelope の形状を伝えるため、トップレベル
    key・入れ子・余分な field、code fence、request echo の禁止を明記し、Schema 本体を同梱する。
    """

    required = response_schema.get("required")
    top_level = (
        [str(key) for key in required]
        if isinstance(required, list)
        else sorted(response_schema.get("properties", {}))
    )
    schema_json = json.dumps(
        dict(response_schema), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    sections = [skill_md.rstrip()]
    if output_contract.strip():
        sections.append(f"## Output contract\n\n{output_contract.rstrip()}")
    sections.append(
        "## Response format (strict)\n\n"
        "Return ONLY one JSON object and nothing else: no Markdown, no code fences, no "
        "commentary, and no additional top-level fields. Do NOT echo the request or the "
        "capability catalog. The top-level keys MUST be exactly: "
        f"{', '.join(top_level)}. Keep every other field nested inside `report` and "
        "`runtime_manifest_draft` as the schema requires; never flatten nested fields to the "
        "top level. In particular, `capabilities`, `tasks`, `tools`, "
        "`workflows`, and `capability_blueprint` belong INSIDE `runtime_manifest_draft`, not at "
        "the top level. Any other observation, ViewSpec need, assumption, or caveat MUST go "
        "inside `report` (its `diagnostics`, `assumptions`, or `questions`), never as a new "
        "top-level key. The object must have this exact shape (placeholders to fill in, keep the "
        "nesting):\n"
        '{"response_version":"...","source_hash":"...","interpreter":{...},'
        '"report":{...},"runtime_manifest_draft":{"capabilities":[...],"tasks":[...],'
        '"tools":[...],"workflows":[...],'
        '"capability_blueprint":{"blueprint_version":"skillmind.capability-blueprint/v1",'
        '"capabilities":[...],"tasks":[...],"resource_requirements":[...],"guidance":{...},'
        '"source_traces":[...]}}}\n'
        f"It MUST validate against this JSON Schema:\n{schema_json}"
    )
    return "\n\n".join(sections)


def _clipped(text: str) -> str:
    """進行 event 用に prompt 本文を上限で切り詰める。"""

    if len(text) <= PROMPT_EVENT_MAX_CHARS:
        return text
    return text[:PROMPT_EVENT_MAX_CHARS]
