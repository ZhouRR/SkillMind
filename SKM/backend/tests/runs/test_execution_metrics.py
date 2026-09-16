"""監査相関だけから回復を数え、未証明の成功を作らない。"""

from types import SimpleNamespace
from uuid import uuid4

from skillmind.runs.execution_metrics import execution_metrics


def call(capability, status, identity, **extra):
    """値・SQL を含めず ToolCall 投影の最小例を作る。"""
    return SimpleNamespace(
        id=uuid4(),
        capability_version=capability,
        status=status,
        integration_id="same",
        arguments_summary={
            "database": {
                "table": "public.example",
                "binding_id": "same",
                "query_checksum": "same",
                **identity,
            }
        },
        result_json={},
        error_json={},
        **extra,
    )


def test_recovered_query_requires_linked_schema_and_read():
    """後続の無関係な成功は recovery と数えず、元 FAILED は残す。"""
    failed = call("database.read/v2", "FAILED", {})
    failed.error_json = {"database": {"reason_code": "undefined_column"}}
    unrelated = call("database.read/v2", "SUCCEEDED", {})
    metrics = execution_metrics([failed, unrelated], modern=True, manual_responses=1)
    assert metrics["corrected_reads"] == 0 and metrics["unresolved_tool_errors"] == 1
    inspection = call("database.describe/v1", "SUCCEEDED", {"recovery_from": str(failed.id)})
    inspection.result_json = {"evidence_refs": ["ev_schema"], "cached": False}
    correction = call(
        "database.read/v2",
        "SUCCEEDED",
        {"recovery_from": str(failed.id), "schema_evidence_ref": "ev_schema"},
    )
    metrics = execution_metrics([failed, inspection, correction], modern=True, manual_responses=1)
    assert metrics["corrected_reads"] == 1 and metrics["unresolved_tool_errors"] == 0
    assert metrics["manual_responses"] == 1 and metrics["query_error_interventions"] is None
    assert failed.status == "FAILED"
    assert execution_metrics([failed, correction], modern=False, manual_responses=1) is None
