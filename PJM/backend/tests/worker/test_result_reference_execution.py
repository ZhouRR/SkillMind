"""正式な ResultValidator の参照拒否が主終態と子結論の両方へ届くことを検証する。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from projectmind.agent.domain import AgentEngine, AgentEventType, RunContext
from projectmind.agent.outcome import compile_outcome_schema
from projectmind.agent.result_validation import ResultValidationError, ResultValidator
from projectmind.agent.subagent_result import SubagentTranscript
from projectmind.runs.domain import ClaimedRun, RunStatus
from projectmind.worker.executor import AgentRunExecutor
from tests.agent.test_artifact_references import ArtifactIndex, artifact_outcome
from tests.agent.test_result_references import ProposalLookup, effect_claim
from tests.agent.test_result_validation import MemoryEvidenceLookup, _outcome
from tests.worker.test_agent_run_executor import (
    ContextBuilder,
    SequenceEngine,
    _claimed,
    _event,
    _service,
)


class OutcomeContextBuilder(ContextBuilder):
    """通常 builder の identity を維持し、正式包絡の凍結 Schema だけを供給する。"""

    async def build(self, claimed_run: ClaimedRun, *, sequence_start: int) -> RunContext:
        """主と子が同じ原包絡を検証する context を返す。"""

        context = await super().build(claimed_run, sequence_start=sequence_start)
        compiled = compile_outcome_schema(None, task_schema_checksum=None)
        return replace(
            context, result_schema=compiled.schema,
            task_snapshot={
                **context.task_snapshot, "result_kind": "OUTCOME_ENVELOPE",
                "output_schema_checksum": compiled.checksum,
            },
        )


@pytest.mark.parametrize("invalid", ["artifact", "effect"])
async def test_primary_and_child_reject_the_same_unverified_result(
    tmp_path: Path, invalid: str,
) -> None:
    """SDK の成功通知でも未核実参照を Result 保存や子 COMPLETED に変換しない。"""

    claimed = _claimed()
    value: dict[str, Any] = _outcome(evidence_refs=[])
    if invalid == "artifact":
        value["deliverables"] = [
            {"key": "file", "kind": "artifact", "title": "Report", "artifact_ref": "art_fake"},
        ]
        code = "artifact_reference_unavailable"
    else:
        value["effects"] = [effect_claim()]
        code = "effect_summary_invalid"
    effect_lookup = AsyncMock()
    effect_lookup.invalid_refs.return_value = frozenset({"cp_original"})
    validator = ResultValidator(
        MemoryEvidenceLookup(frozenset({"ev_before", "ev_after"})),
        ProposalLookup(claimed.run_id), effect_lookup=effect_lookup,
    )
    event = _event(
        claimed, 10, AgentEventType.RESULT_COMPLETED, session_id=str(uuid4()),
        payload={"structured_output": value},
    )
    service = _service()
    builder = OutcomeContextBuilder(tmp_path)
    executor = AgentRunExecutor(
        run_service=service, context_builder=builder,
        engine=cast(AgentEngine, SequenceEngine([event])),
        result_validator=validator, lease_seconds=60,
    )
    await executor.execute(claimed)
    final = service.finalize_execution.await_args.kwargs
    assert final["target"] is RunStatus.FAILED
    assert final["result"] is None
    assert final["error_json"]["code"] == code

    context = await builder.build(claimed, sequence_start=10)
    transcript = SubagentTranscript(context)
    await transcript.consume(SequenceEngine([event]).execute(context))
    with pytest.raises(ResultValidationError) as captured:
        await transcript.completed("branch", validator)
    assert captured.value.code == code


@pytest.mark.parametrize("valid", [False, True])
async def test_primary_and_child_share_saved_artifact_verification(
    tmp_path: Path, valid: bool,
) -> None:
    """原 snapshot lookup が主 Result と子 COMPLETED の両方を許可・拒否する。"""

    claimed = _claimed()
    refs = frozenset({"art_original"}) if valid else frozenset()
    lookup = ArtifactIndex(claimed.run_id, refs)
    validator = ResultValidator(MemoryEvidenceLookup(frozenset()), artifact_lookup=lookup)
    event = _event(
        claimed, 10, AgentEventType.RESULT_COMPLETED, session_id=str(uuid4()),
        payload={"structured_output": artifact_outcome(top=False, nested=True)},
    )
    service = _service()
    builder = OutcomeContextBuilder(tmp_path)
    executor = AgentRunExecutor(
        run_service=service, context_builder=builder,
        engine=cast(AgentEngine, SequenceEngine([event])),
        result_validator=validator, lease_seconds=60,
    )
    await executor.execute(claimed)
    final = service.finalize_execution.await_args.kwargs
    context = await builder.build(claimed, sequence_start=10)
    transcript = SubagentTranscript(context)
    await transcript.consume(SequenceEngine([event]).execute(context))
    if valid:
        assert final["target"] is RunStatus.SUCCEEDED
        assert final["result"].artifact_refs == ("art_original",)
        assert final["result"].validation["artifact_refs_valid"] is True
        assert (await transcript.completed("branch", validator)).outcome == "COMPLETED"
    else:
        assert final["target"] is RunStatus.FAILED and final["result"] is None
        assert final["error_json"]["code"] == "artifact_reference_invalid"
        with pytest.raises(ResultValidationError, match="unavailable Artifact"):
            await transcript.completed("branch", validator)
    assert lookup.calls == [(claimed.run_id, frozenset({"art_original"}))] * 2
