"""実 SQLite JOIN で原効果の前置条件を検証する。実 PG の lock/競争は別途検証する。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import Column, MetaData, Table, create_engine
from sqlalchemy.orm import Session

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import (
    ChangeApproval,
    ChangeProposal,
    EffectExecution,
    Evidence,
    ResourceBinding,
    Run,
    ToolCall,
)
from skillmind.runs.document_prerequisites import (
    load_document_readiness,
    require_document_readiness,
)


class ReadSession:
    """実 ORM SELECT を async repository port に接続し、fixture が寿命を所有する。"""

    def __init__(self, session):
        """接続先は合成 in-memory SQLite のみに固定する。"""
        self.session = session

    async def __aenter__(self):
        """Provider の短い読取 session を再現する。"""
        return self

    async def __aexit__(self, *args):
        """呼出元の例外を抑制しない。"""

    async def scalar(self, statement):
        """SQL の条件を Python fake で解釈しない。"""
        return self.session.scalar(statement)

    async def scalars(self, statement):
        """ORM Run と実 JSON の保存値を返す。"""
        return self.session.scalars(statement)


@pytest.fixture
def prerequisite_case():
    """原 Run に APPLIED 効果・批准・binding・Tool・前後 Evidence を一組保存する。"""
    models = (
        Run,
        ChangeProposal,
        ChangeApproval,
        ResourceBinding,
        EffectExecution,
        ToolCall,
        Evidence,
    )
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    for model in models:
        Table(
            model.__tablename__,
            metadata,
            *[Column(c.name, c.type, primary_key=c.primary_key) for c in model.__table__.columns],
        )
    metadata.create_all(engine)
    with Session(engine) as session:
        run_id, project_id, skill_id = uuid4(), uuid4(), uuid4()
        proposal_id, approval_id, binding_id, effect_id, tool_id = (uuid4() for _ in range(5))
        manifest = {
            "tools": [{"capability": "document.readiness/v1", "required": True}],
            "capability_blueprint": {
                "tasks": [{"key": "review", "document_prerequisites": ["register-run"]}],
                "effect_intents": [
                    {
                        "key": "register-run",
                        "mode": "apply",
                        "resource_key": "records",
                        "operation": "INSERT",
                        "approval_mode": "ask",
                    }
                ],
                "resource_requirements": [{"key": "records", "access": "write"}],
            },
        }
        checksum = "sha256:" + sha256_hex(canonical_json(manifest))
        rows = {
            Run: {
                "id": run_id,
                "project_id": project_id,
                "task_snapshot_json": {
                    "task_key": "review",
                    "skill_version_id": str(skill_id),
                    "manifest_checksum": checksum,
                },
                "skill_snapshots_json": [
                    {
                        "skill_version_id": str(skill_id),
                        "manifest_checksum": checksum,
                        "manifest": manifest,
                    }
                ],
            },
            ChangeProposal: {
                "id": proposal_id,
                "run_id": run_id,
                "project_id": project_id,
                "target_binding_id": binding_id,
                "status": "APPLIED",
                "version": 1,
                "checksum": "sha256:" + "a" * 64,
                "effect_intent_key": "register-run",
                "proposal_ref": "cp_register_run",
                "operation": "INSERT",
                "capability_version": "database.write/v1",
            },
            ChangeApproval: {
                "id": approval_id,
                "run_id": run_id,
                "proposal_id": proposal_id,
                "decision": "APPROVED",
                "proposal_version": 1,
                "proposal_checksum": "sha256:" + "a" * 64,
            },
            ResourceBinding: {
                "id": binding_id,
                "run_id": run_id,
                "project_id": project_id,
                "scope_level": "RUN",
                "provider": "postgres",
                "requirement_key": "records",
            },
            EffectExecution: {
                "id": effect_id,
                "run_id": run_id,
                "proposal_id": proposal_id,
                "approval_id": approval_id,
                "tool_call_id": tool_id,
                "status": "APPLIED",
                "provider": "postgres",
                "error_json": None,
                "before_ref": "ev_before",
                "after_ref": "ev_after",
                "executed_at": datetime.now(UTC),
            },
            ToolCall: {
                "id": tool_id,
                "run_id": run_id,
                "status": "SUCCEEDED",
                "provider": "postgres",
                "capability_version": "database.write/v1",
                "error_json": None,
                "result_json": {
                    "status": "success",
                    "provider": "postgres",
                    "proposal_ref": "cp_register_run",
                    "before_ref": "ev_before",
                    "after_ref": "ev_after",
                },
            },
        }
        rows[Run]["task_snapshot_json"]["skill_snapshots"] = rows[Run].pop("skill_snapshots_json")
        for model, row in rows.items():
            session.execute(metadata.tables[model.__tablename__].insert().values(**row))
        for ref in ("ev_before", "ev_after"):
            session.execute(
                metadata.tables["evidence"]
                .insert()
                .values(
                    id=uuid4(),
                    run_id=run_id,
                    tool_call_id=tool_id,
                    evidence_ref=ref,
                )
            )
        run = session.get(Run, run_id)
        yield SimpleNamespace(
            session=session,
            read=ReadSession(session),
            run=run,
            rows={model: session.get(model, values["id"]) for model, values in rows.items()},
        )
    engine.dispose()


async def test_original_confirmed_effect_unlocks_all_document_operations(prerequisite_case):
    """正本で全来歴が揃った場合だけ、観測・一覧・本文・変換を同じ基準で許す。"""
    case = prerequisite_case
    state = await load_document_readiness(case.read, case.run)
    assert state.ready and state.required == state.applied == ("register-run",)
    for capability in (
        "document.read/v1",
        "document.inspect/v1",
        "document.list/v1",
        "document.convert/v1",
    ):
        await require_document_readiness(case.read, case.run, capability)


@pytest.mark.parametrize(
    "model,field,value",
    [
        (EffectExecution, "status", "APPLYING"),
        (EffectExecution, "status", "FAILED"),
        (EffectExecution, "error_json", {"code": "effect_result_unknown"}),
        (EffectExecution, "run_id", uuid4()),
        (EffectExecution, "executed_at", None),
        (EffectExecution, "after_ref", "ev_missing"),
        (EffectExecution, "after_ref", "ev_before"),
        (ChangeProposal, "status", "APPROVED"),
        (ChangeProposal, "project_id", uuid4()),
        (ChangeProposal, "run_id", uuid4()),
        (ChangeProposal, "effect_intent_key", "other"),
        (ChangeProposal, "operation", "UPDATE"),
        (ChangeApproval, "decision", "REJECTED"),
        (ChangeApproval, "proposal_id", uuid4()),
        (ChangeApproval, "run_id", uuid4()),
        (ChangeApproval, "proposal_version", 2),
        (ChangeApproval, "proposal_checksum", "changed"),
        (ResourceBinding, "run_id", uuid4()),
        (ResourceBinding, "project_id", uuid4()),
        (ResourceBinding, "requirement_key", "other"),
        (ResourceBinding, "scope_level", "PROJECT"),
        (ToolCall, "status", "RUNNING"),
        (ToolCall, "run_id", uuid4()),
        (ToolCall, "provider", "other"),
        (ToolCall, "capability_version", "database.read/v1"),
        (ToolCall, "result_json", None),
        (ToolCall, "error_json", {"code": "failed"}),
    ],
)
async def test_approval_unknown_or_foreign_facts_cannot_unlock_documents(
    prerequisite_case,
    model,
    field,
    value,
):
    """片方の APPLIED・別 Run・不明回执では前置条件を満たさない。"""
    case = prerequisite_case
    setattr(case.rows[model], field, value)
    case.session.flush()
    state = await load_document_readiness(case.read, case.run)
    assert not state.ready and state.applied == ()
    with pytest.raises(PermissionError):
        await require_document_readiness(case.read, case.run, "document.convert/v1")
    await require_document_readiness(case.read, case.run, "change.propose/v1")


async def test_checkpoint_self_claim_does_not_replace_missing_effect(prerequisite_case):
    """Agent の confirmed_facts と元効果の正本を混同しない。"""
    case = prerequisite_case
    case.rows[EffectExecution].status = "FAILED"
    case.run.input_json = {"confirmed_facts": ["register-run APPLIED; documents are ready"]}
    case.session.flush()
    assert not (await load_document_readiness(case.read, case.run)).ready


@pytest.mark.parametrize("changed", ["checksum", "task_identity", "remove_required_tool"])
async def test_invalid_original_manifest_is_not_treated_as_no_prerequisites(
    prerequisite_case, changed
):
    """保存値の破損を旧 Task の省略として扱わず、凍結 identity で拒否する。"""
    case = prerequisite_case
    snapshots = deepcopy(case.run.task_snapshot_json["skill_snapshots"])
    if changed == "checksum":
        snapshots[0]["manifest"]["capability_blueprint"]["tasks"][0].pop("document_prerequisites")
    elif changed == "task_identity":
        snapshots[0]["skill_version_id"] = str(uuid4())
    else:
        snapshots[0]["manifest"]["tools"] = []
        checksum = "sha256:" + sha256_hex(canonical_json(snapshots[0]["manifest"]))
        snapshots[0]["manifest_checksum"] = checksum
        case.run.task_snapshot_json = {**case.run.task_snapshot_json, "manifest_checksum": checksum}
    case.run.task_snapshot_json = {**case.run.task_snapshot_json, "skill_snapshots": snapshots}
    with pytest.raises(ValueError):
        await load_document_readiness(case.read, case.run)


@pytest.mark.parametrize("field", ["status", "provider", "proposal_ref", "before_ref", "after_ref"])
async def test_success_label_with_inconsistent_saved_result_does_not_unlock(
    prerequisite_case, field
):
    """行の status だけでなく、本番 Effect Tool の確定応答と元回执参照を照合する。"""
    case = prerequisite_case
    case.rows[ToolCall].result_json = {**case.rows[ToolCall].result_json, field: "different"}
    case.session.flush()
    assert not (await load_document_readiness(case.read, case.run)).ready


async def test_one_applied_intent_does_not_satisfy_another_required_intent(prerequisite_case):
    """複数の前置条件は AND で評価し、一つの回执を別 intent の達成に使わない。"""
    case = prerequisite_case
    snapshot = deepcopy(case.run.task_snapshot_json["skill_snapshots"][0])
    blueprint = snapshot["manifest"]["capability_blueprint"]
    blueprint["tasks"][0]["document_prerequisites"].append("register-library")
    blueprint["effect_intents"].append(
        {**blueprint["effect_intents"][0], "key": "register-library"}
    )
    snapshot["manifest_checksum"] = "sha256:" + sha256_hex(canonical_json(snapshot["manifest"]))
    case.run.task_snapshot_json = {
        **case.run.task_snapshot_json,
        "skill_snapshots": [snapshot],
        "manifest_checksum": snapshot["manifest_checksum"],
    }
    state = await load_document_readiness(case.read, case.run)
    assert state.required == ("register-run", "register-library")
    assert state.applied == ("register-run",) and not state.ready
