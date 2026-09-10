"""最初の model adapter の成功経路と安定した失敗分類を検証する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from skillmind.skills.interpreter import (
    build_interpreter_generation_schema,
    load_interpreter_system_skill,
)
from skillmind.skills.interpreter_execution import (
    InterpreterErrorCode,
    InterpreterExecutionError,
)
from skillmind.skills.model_interpreter import (
    ModelCompletion,
    ModelProviderError,
    ModelSkillInterpreter,
    ModelStructuredOutputError,
    _compose_system_prompt,
)
from skillmind.skills.task_contract import TASK_CONTRACT_VERSION

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
SYSTEM_SKILL = ROOT / "skills" / "skillmind-skill-interpreter"


class _FakeClient:
    """設定した completion か例外を返し、呼び出し回数を記録する transport。"""

    def __init__(
        self,
        *,
        completion: ModelCompletion | None = None,
        error: Exception | None = None,
        stream_deltas: tuple[str, ...] = (),
    ) -> None:
        """成功時 completion か送出する例外のどちらかを保持する。"""

        self._completion = completion
        self._error = error
        self._stream_deltas = stream_deltas
        self.calls = 0
        self.partial_requested = False
        self.user_messages: list[str] = []

    async def complete(
        self,
        *,
        system_prompt: str,
        user_message: str,
        response_schema: Mapping[str, Any],
        model: str,
        parameters: Mapping[str, Any],
        on_text_delta: Any = None,
    ) -> ModelCompletion:
        """呼び出しを記録し、delta を転送しつつ設定済み結果か例外を返す。"""

        del system_prompt, response_schema, model, parameters
        self.calls += 1
        self.user_messages.append(user_message)
        self.partial_requested = on_text_delta is not None
        if on_text_delta is not None:
            for delta in self._stream_deltas:
                await on_text_delta(delta)
        if self._error is not None:
            raise self._error
        assert self._completion is not None
        return self._completion


def _response_schema() -> dict[str, Any]:
    """Nested artifact を完全拘束する model generation schema を組み立てる。"""

    return build_interpreter_generation_schema(CONTRACTS)


def test_generation_schema_constrains_report_and_manifest_recursively() -> None:
    """SDK に渡す Schema が envelope だけでなく nested Report/Manifest も拘束する。"""

    schema = _response_schema()
    properties = schema["properties"]
    report = properties["report"]
    manifest = properties["runtime_manifest_draft"]

    assert "summary" in report["required"]
    assert report["additionalProperties"] is False
    assert "tasks" in manifest["required"]
    task_schema = manifest["properties"]["tasks"]["items"]
    assert task_schema["additionalProperties"] is False
    assert task_schema["required"] == [
        "key",
        "input_contract",
        "contract_source_trace",
    ]
    assert "input_schema" not in task_schema["properties"]
    assert "output_schema_checksum" not in task_schema["properties"]
    # TaskContractDraft の bounded recursion だけは自己完結 `$defs` として model Schema に残す。
    assert "contractField" in manifest["$defs"]
    assert '"$ref": "#/$defs/contractField"' in json.dumps(schema)
    fixture = json.loads(
        (CONTRACTS / "examples" / "skill-interpreter-response.v1.json").read_text(
            encoding="utf-8"
        )
    )
    generated = json.loads(
        (CONTRACTS / "examples" / "generated-task-manifest.v1alpha1.json").read_text(
            encoding="utf-8"
        )
    )
    task = generated["tasks"][0]
    for key in (
        "input_schema",
        "output_schema",
        "input_schema_checksum",
        "output_schema_checksum",
    ):
        task.pop(key)
    fixture["runtime_manifest_draft"]["tasks"] = [task]
    # 蓝图は model 生成物であり、identity/compatibility だけは platform が束縛する。fixture は
    # 束縛後の姿を示すため、model 視点の Schema と突き合わせる前に platform 側 field を外す。
    blueprint = fixture["runtime_manifest_draft"]["capability_blueprint"]
    for key in ("identity", "compatibility"):
        blueprint.pop(key)
    Draft202012Validator(schema).validate(fixture)
    fixture["report"] = {}
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(fixture)


def test_generation_schema_requires_a_blueprint_without_platform_bound_identity() -> None:
    """Model には蓝图の生成を必須にし、identity/compatibility の複刻はさせない。"""

    manifest = _response_schema()["properties"]["runtime_manifest_draft"]
    blueprint = manifest["properties"]["capability_blueprint"]

    assert "capability_blueprint" in manifest["required"]
    # 能力・目標・資源・指導・根拠は蓝图の最低要件として model に課す。
    for key in ("capabilities", "tasks", "resource_requirements", "guidance", "source_traces"):
        assert key in blueprint["required"]
    # source_hash や互換 level を model に複刻させると manifest 側と食い違う。
    for key in ("identity", "compatibility"):
        assert key not in blueprint["properties"]
        assert key not in blueprint["required"]


def _interpreter(
    client: _FakeClient, *, accept_prompt_json: bool = False
) -> ModelSkillInterpreter:
    """実際の system Skill root を使う model adapter を構築する。"""

    return ModelSkillInterpreter(
        completion_client=client,
        system_skill_root=SYSTEM_SKILL,
        response_schema=_response_schema(),
        accept_prompt_json=accept_prompt_json,
    )


def _request() -> dict[str, Any]:
    """System Skill identity を正しく束ねた最小 request を作る。"""

    identity = load_interpreter_system_skill(
        SYSTEM_SKILL,
        generation_schema=_response_schema(),
    )
    return {"interpreter": identity.to_dict(), "source": {"content_hash": "sha256:" + "0" * 64}}


@pytest.mark.asyncio
async def test_structured_output_is_returned_directly() -> None:
    """Structured output があれば text を解析せずそのまま候補として返す。"""

    client = _FakeClient(
        completion=ModelCompletion(structured_output={"ok": True}, text=None, truncated=False)
    )

    result = await _interpreter(client).interpret(
        _request(), model="claude-opus-4-8", parameters={}
    )

    assert result == {"ok": True}
    assert client.calls == 1


@pytest.mark.asyncio
async def test_progress_events_emit_prompt_and_forward_text_deltas() -> None:
    """on_event 指定時は prompt を可視化し、transport の text delta を転送する。"""

    client = _FakeClient(
        completion=ModelCompletion(structured_output={"ok": True}, text=None, truncated=False),
        stream_deltas=("Analy", "zing"),
    )
    events: list[tuple[str, dict[str, Any]]] = []

    async def on_event(event: str, data: Any) -> None:
        """受信 event を記録する。"""

        events.append((event, dict(data)))

    result = await _interpreter(client).interpret(
        _request(), model="m", parameters={}, on_event=on_event
    )

    assert result == {"ok": True}
    # partial message を要求し、実際の prompt を event として可視化する。
    assert client.partial_requested is True
    assert events[0][0] == "interpret.prompt"
    assert events[0][1]["system_prompt"]
    assert events[0][1]["user_message"]
    # transport の delta が interpret.delta として同順で転送される。
    deltas = [data["text"] for name, data in events if name == "interpret.delta"]
    assert deltas == ["Analy", "zing"]


@pytest.mark.asyncio
async def test_no_progress_events_keep_completion_non_streaming() -> None:
    """on_event 未指定時は partial を要求せず、従来の一括受信のまま動く。"""

    client = _FakeClient(
        completion=ModelCompletion(structured_output={"ok": True}, text=None, truncated=False),
        stream_deltas=("ignored",),
    )

    result = await _interpreter(client).interpret(_request(), model="m", parameters={})

    assert result == {"ok": True}
    assert client.partial_requested is False


@pytest.mark.asyncio
async def test_validation_feedback_requests_a_complete_replacement_without_candidate_echo() -> None:
    """修復 retry は脱敏済み finding だけを追加し、完全 response の再生成を要求する。"""

    client = _FakeClient(
        completion=ModelCompletion(structured_output={"ok": True}, text=None, truncated=False)
    )

    await _interpreter(client).interpret(
        _request(),
        model="m",
        parameters={},
        validation_feedback="path=/report/confidence validator=required",
    )

    assert "complete replacement object" in client.user_messages[0]
    assert "path=/report/confidence validator=required" in client.user_messages[0]


@pytest.mark.asyncio
async def test_text_completion_is_rejected_when_structured_output_is_missing() -> None:
    """JSON text へ降格せず、structured-output 非対応 Provider を明示失敗にする。"""

    client = _FakeClient(
        completion=ModelCompletion(
            structured_output=None, text='{"ok": true}', truncated=False
        )
    )

    with pytest.raises(InterpreterExecutionError) as excinfo:
        await _interpreter(client).interpret(_request(), model="m", parameters={})

    assert excinfo.value.code is InterpreterErrorCode.STRUCTURED_OUTPUT_UNAVAILABLE


@pytest.mark.asyncio
async def test_identity_mismatch_fails_before_model_call() -> None:
    """Request の system Skill identity 不一致は model 呼び出し前に閉じる。"""

    client = _FakeClient(
        completion=ModelCompletion(structured_output={"ok": True}, text=None, truncated=False)
    )
    request = _request()
    request["interpreter"] = {**request["interpreter"], "version": "9.9.9"}

    with pytest.raises(InterpreterExecutionError) as excinfo:
        await _interpreter(client).interpret(request, model="m", parameters={})

    assert excinfo.value.code is InterpreterErrorCode.IDENTITY_MISMATCH
    assert client.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("completion", "error", "expected"),
    [
        (
            ModelCompletion(structured_output={"ok": True}, text=None, truncated=True),
            None,
            InterpreterErrorCode.TRUNCATED_OUTPUT,
        ),
        (None, TimeoutError("slow"), InterpreterErrorCode.TIMEOUT),
        (
            None,
            ModelStructuredOutputError("retry exhausted"),
            InterpreterErrorCode.STRUCTURED_OUTPUT_UNAVAILABLE,
        ),
        (None, ModelProviderError("provider"), InterpreterErrorCode.PROVIDER_ERROR),
    ],
)
async def test_failure_is_classified_into_stable_taxonomy(
    completion: ModelCompletion | None,
    error: Exception | None,
    expected: InterpreterErrorCode,
) -> None:
    """timeout、provider、invalid JSON、truncated、empty を安定 code へ分類する。"""

    client = _FakeClient(completion=completion, error=error)

    with pytest.raises(InterpreterExecutionError) as excinfo:
        await _interpreter(client).interpret(_request(), model="m", parameters={})

    assert excinfo.value.code is expected


@pytest.mark.asyncio
async def test_interpret_rejects_a_wrapping_code_fence_without_structured_output() -> None:
    """Fence 付き JSON を受理せず、Provider の structured-output 非対応を隠さない。"""

    payload = {"response_version": "skillmind.skill-interpreter.response/v1", "ok": True}
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    client = _FakeClient(
        completion=ModelCompletion(structured_output=None, text=fenced, truncated=False)
    )

    with pytest.raises(InterpreterExecutionError) as excinfo:
        await _interpreter(client).interpret(_request(), model="m", parameters={})

    assert excinfo.value.code is InterpreterErrorCode.STRUCTURED_OUTPUT_UNAVAILABLE


@pytest.mark.asyncio
async def test_interpret_accepts_prompt_guided_json_text_when_opted_in() -> None:
    """accept_prompt_json=True の時、structured output 非対応 endpoint (Kimi 等) の fence 付き
    text JSON を候補として受理する。下流の schema 検証が契約を担保する。"""

    payload = {"response_version": "skillmind.skill-interpreter.response/v1", "ok": True}
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    client = _FakeClient(
        completion=ModelCompletion(structured_output=None, text=fenced, truncated=False)
    )

    result = await _interpreter(client, accept_prompt_json=True).interpret(
        _request(), model="m", parameters={}
    )

    assert result == payload


def test_compose_system_prompt_embeds_envelope_contract_and_schema() -> None:
    """System prompt 本文に envelope の top-level key・厳密指示・Schema 本体が入る。"""

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["response_version", "report", "runtime_manifest_draft"],
        "properties": {"response_version": {}, "report": {}, "runtime_manifest_draft": {}},
    }
    prompt = _compose_system_prompt("# Interpreter\nDo the task.", "- Bind source_hash.", schema)

    assert "# Interpreter" in prompt  # SKILL.md 本文
    assert "Bind source_hash" in prompt  # output contract 連結
    # Envelope の top-level key を明示し、余分な field、code fence、request echo を禁じる。
    assert "response_version" in prompt and "runtime_manifest_draft" in prompt
    assert "no additional top-level fields" in prompt and "no code fences" in prompt
    assert "never flatten" in prompt
    # 観測された平铺失敗への直接対策: manifest 系 field は runtime_manifest_draft へ入れ子と明示。
    assert "belong INSIDE `runtime_manifest_draft`" in prompt
    # 追加観察は新 top-level key ではなく report に入れるよう誘導(view_spec_needs 混入への対策)。
    assert "never as a new top-level key" in prompt
    assert '"runtime_manifest_draft":{"capabilities":[...]' in prompt  # 骨架例
    assert '"additionalProperties":false' in prompt  # schema 本体を同梱する


def test_generation_schema_constrains_contract_drafts_nested_in_the_blueprint() -> None:
    """蓝图内の任意 contract draft も再帰 `$defs` で拘束され続ける。

    蓝图を manifest の下へ入れ子にすると `#/$defs` の解決先が変わり得る。解決が壊れると
    JSON Schema keyword の密輸が SDK 検証を素通りするため、拒否されることまで確かめる。
    """

    schema = _response_schema()
    response = json.loads(
        (CONTRACTS / "examples" / "skill-interpreter-response.v1.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = response["runtime_manifest_draft"]
    for key in ("identity", "compatibility"):
        manifest["capability_blueprint"].pop(key)
    for task in manifest["tasks"]:
        for key in (
            "input_schema",
            "output_schema",
            "input_schema_checksum",
            "output_schema_checksum",
        ):
            task.pop(key)
    blueprint_task = manifest["capability_blueprint"]["tasks"][0]

    blueprint_task["parameter_contract"] = {
        "contract_version": TASK_CONTRACT_VERSION,
        "type": "object",
        "fields": [
            {"key": "target_path", "type": "string", "required": True, "min_length": 1}
        ],
    }
    Draft202012Validator(schema).validate(response)

    # 実行式や外部参照は元モデル外の keyword であり、入れ子でも拒否されなければならない。
    blueprint_task["parameter_contract"]["fields"][0]["$ref"] = "http://example.invalid/x"
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(response)
