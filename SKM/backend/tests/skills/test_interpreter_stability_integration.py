"""本番 adapter/service の接続を fake completion で検証し、実モデルや DB は呼ばない。"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from skillmind.skills.runtime_profile import InterpreterRuntimeProfile


@pytest.mark.asyncio
async def test_model_identity_binds_profile_and_freezes_operation_catalog() -> None:
    """実 adapter に渡す identity と prompt の操作集合が同じ凍結値である。"""

    import json

    from skillmind.effects.operation_policy import WRITE_OPERATIONS
    from skillmind.skills.direct_candidate import candidate_schema
    from skillmind.skills.model_interpreter import ModelCompletion, ModelSkillInterpreter
    from tests.skills.test_skill_service import CONTRACTS, SYSTEM_SKILL
    from tests.skills.test_source_execution import direct_case

    request, candidate = direct_case()
    calls: list[dict[str, Any]] = []

    class Completion:
        """外部接続せず、実 adapter が構成した入力を記録する。"""

        async def complete(self, **kwargs: Any) -> ModelCompletion:
            """候補を defensive copy で返し、次の呼出しと共有しない。"""

            calls.append(deepcopy(kwargs))
            return ModelCompletion(deepcopy(candidate), None, False)

    profile = InterpreterRuntimeProfile("codex", "test-model", "medium", "1", "1")
    interpreter = ModelSkillInterpreter(
        completion_client=Completion(), system_skill_root=SYSTEM_SKILL,
        response_schema=candidate_schema(CONTRACTS), runtime_profile=profile,
    )
    request["interpreter"] = interpreter.system_identity.to_dict()
    public_profile = interpreter.runtime_profile
    assert public_profile is not None
    public_profile["model"] = "modified-copy"
    assert interpreter.runtime_profile["model"] == "test-model"
    await interpreter.interpret(request, model="test-model", parameters={})
    assert len(calls) == 1
    sent = json.loads(calls[0]["user_message"])
    assert sent["write_operations"] == json.loads(json.dumps(WRITE_OPERATIONS))
    assert calls[0]["parameters"] == {}


@pytest.mark.asyncio
async def test_invalid_parameters_and_model_drift_never_start_completion(tmp_path: Any) -> None:
    """誤った設定は原呼出し許可や SDK 起動より前に拒否する。"""

    from skillmind.skills.interpreter_execution import InterpreterErrorCode, InterpreterExecutionError
    from skillmind.skills.model_interpreter import ModelSkillInterpreter

    class NoCompletion:
        """呼出しが発生すると直ちに失敗する境界 fake。"""

        async def complete(self, **kwargs: Any) -> Any:
            """無効入力で transport に到達する退行を検出する。"""

            raise AssertionError("Completion must not be called")

    interpreter = ModelSkillInterpreter(
        completion_client=NoCompletion(), system_skill_root=tmp_path,
        response_schema={"type": "object"},
        runtime_profile=InterpreterRuntimeProfile("codex", "test-model", "medium", "1", "1"),
    )
    with pytest.raises(InterpreterExecutionError) as caught:
        await interpreter.interpret({}, model="test-model", parameters={"private-key": "value"})
    assert caught.value.code == InterpreterErrorCode.INVALID_PARAMETERS
    with pytest.raises(InterpreterExecutionError) as caught:
        await interpreter.interpret({}, model="different-model", parameters={})
    assert caught.value.code == InterpreterErrorCode.IDENTITY_MISMATCH


def test_service_parent_projection_is_complete_without_rewriting_parent() -> None:
    """service が親の最小宣言を保持し、要約だけの調整へ戻らない。"""

    from skillmind.skills.service import SkillService
    from tests.skills.test_candidate_revision import manifest

    frozen = manifest()
    original = deepcopy(frozen)
    parent = SimpleNamespace(
        interpretation_id="parent", execution_key="key", compatibility_level="adapted",
        summary="summary", report={}, preview=SimpleNamespace(runtime_manifest_draft=frozen),
    )
    service = object.__new__(SkillService)
    context = service._previous_interpretation_summary(parent)
    assert context["launch_contract"]["input_contract"] == frozen["tasks"][0]["input_contract"]
    assert context["launch_contract"]["resource_requirements"] == (
        frozen["skill_execution"]["resource_requirements"]
    )
    assert "launch_contract_checksum" in context
    assert frozen == original


def test_frozen_request_detects_runtime_profile_drift() -> None:
    """API 受理と Worker 再照合が、実設定を含む同じ payload を比較する。"""

    from skillmind.skills.service import _interpretation_request_input

    class Snapshot:
        """DB や parser を呼ばず frozen payload 合成だけを検証する。"""

        def to_dict(self) -> dict[str, Any]:
            """合成境界用の固定値を返す。"""

            return {"value": "fixed"}

    snapshot = Snapshot()
    args = dict(
        prepared=SimpleNamespace(package=snapshot, analysis=snapshot, request={}),
        catalog=snapshot, identity=snapshot, model="test-model", parameters={},
        previous=None, adjustment=None, parent_id=None, nonce=None,
    )
    legacy = _interpretation_request_input(**args)
    assert "runtime_profile" not in legacy
    before = _interpretation_request_input(**args, runtime_profile={"reasoning_effort": "medium"})
    after = _interpretation_request_input(**args, runtime_profile={"reasoning_effort": "high"})
    assert before != after
    assert before["runtime_profile"] == {"reasoning_effort": "medium"}
