"""初回作成意図・旧 hash・DB 唯一競合後の再送境界を検証する。"""

from __future__ import annotations

import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.documents.snapshot import ALL_DOCUMENTS_SELECTION, DOCUMENT_READ_CAPABILITY
from projectmind.runs.creation_replay import validate_creation_replay
from projectmind.runs.creation_request import CREATION_REQUEST_FIELD, TaskRunIntent
from projectmind.runs.domain import IdempotencyConflictError, request_hash
from projectmind.runs.repository import RunRepository
from tests.runs.creation_fakes import creation_command, creation_intent, stored_creation


def test_database_models_can_import_creation_rules_in_a_fresh_process() -> None:
    """package の eager repository export による循環を import 順序で隠さない。"""

    subprocess.run(
        [
            sys.executable,
            "-c",
            "from projectmind.db.models import Run\n"
            "from projectmind.runs.creation_request import TaskRunIntent",
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )


def test_creation_intent_canonicalizes_sets_but_not_business_arrays() -> None:
    """選択集合の順序だけを同一視し、入力 array の順序を失わない。"""

    first, second = UUID(int=1), UUID(int=2)
    original = creation_intent(sources={"docs": f"documents:{second},{first}"})
    reordered = replace(original, sources={"docs": f"documents:{first},{second}"})
    assert original.to_json() == reordered.to_json()
    assert original.fingerprint() == reordered.fingerprint()
    assert TaskRunIntent.from_json(original.to_json()).fingerprint() == original.fingerprint()
    assert (
        replace(original, input_json={"target": "main", "positions": [1, 2]}).fingerprint()
        != original.fingerprint()
    )


@pytest.mark.parametrize(
    "token",
    ["document:nope", "documents:", "integration:bad", f"documents:{UUID(int=1)},{UUID(int=1)}"],
)
def test_creation_intent_rejects_malformed_selection(token: str) -> None:
    """不正な候補を欠落/default と解釈せず、再送照会より前に拒否する。"""

    with pytest.raises(ValueError):
        creation_intent(sources={"docs": token})


@pytest.mark.parametrize(
    "field", ["project_id", "skill_version_id", "actor_id", "task_key", "input_json", "sources"]
)
def test_every_client_identity_dimension_changes_the_fingerprint(field: str) -> None:
    """actor、精確版、入力と資源選択を hash から脱落させない。"""

    original = creation_intent()
    value = (
        uuid4()
        if field.endswith("_id")
        else "different-task"
        if field == "task_key"
        else {"target": True}
        if field == "input_json"
        else {"docs": ALL_DOCUMENTS_SELECTION}
    )
    changed = replace(original, **{field: value})
    assert original.fingerprint() != changed.fingerprint()


def test_intent_owns_its_input_and_serialized_copies() -> None:
    """呼出し元や保存用 dict の変更で、待機中の要求が変化しない。"""

    original = creation_intent()
    value = {"positions": [1, 2]}
    intent = replace(original, input_json=value)
    digest = intent.fingerprint()
    value["positions"].append(3)
    snapshot = intent.to_json()
    snapshot["input"]["positions"].append(4)
    assert intent.fingerprint() == digest


def test_resource_expansion_and_server_policy_do_not_reidentify_a_request() -> None:
    """全集や platform 上限が変わっても、既存要求へ新しい実行意図を割り当てない。"""

    command = creation_command(creation_intent(sources={"docs": ALL_DOCUMENTS_SELECTION}))
    changed = replace(
        command,
        selected_sources_json={"docs": {"document_snapshot": {"documents": [str(uuid4())]}}},
        limits_snapshot_json={"max_turns": 10},
        trace_id="another-trace",
    )
    assert request_hash(changed) == request_hash(command)
    assert command.limits_snapshot_json == {"max_turns": 20}


@pytest.mark.parametrize(
    "field", ["project_id", "task_id", "input_json", "permission_snapshot_json"]
)
def test_request_cannot_override_command_identity(field: str) -> None:
    """command と異なる自己申告 identity を fingerprint override として使わせない。"""

    command = creation_command(creation_intent())
    value = (
        uuid4()
        if field.endswith("_id")
        else {"actor_id": str(uuid4())}
        if field == "permission_snapshot_json"
        else {"target": 1}
    )
    with pytest.raises(ValueError, match="execution snapshot identity"):
        request_hash(replace(command, **{field: value}))


@pytest.mark.parametrize("change", ["version", "extra", "input", "actor", "hash", "shape"])
def test_corrupt_or_unknown_stored_intent_is_not_replayed(change: str) -> None:
    """DB snapshot の未知版や不整合は現在の資源で作り直さず衝突として閉じる。"""

    intent = creation_intent()
    row = stored_creation(creation_command(intent))
    if change == "version":
        row.task_snapshot_json[CREATION_REQUEST_FIELD]["request_version"] = "v2"
    elif change == "extra":
        row.task_snapshot_json[CREATION_REQUEST_FIELD]["override_hash"] = "untrusted"
    elif change == "input":
        row.input_json = {"target": "changed"}
    elif change == "actor":
        row.permission_snapshot_json["actor_id"] = str(uuid4())
    elif change == "hash":
        row.request_hash = "0" * 64
    else:
        row.selected_sources_json = []  # type: ignore[assignment]
    with pytest.raises(IdempotencyConflictError, match="cannot be safely replayed"):
        validate_creation_replay(row, intent)


def test_same_key_cannot_replay_another_actors_request() -> None:
    """Project に入れる別 actor でも作成要求の同一性を横取りできない。"""

    intent = creation_intent()
    row = stored_creation(creation_command(intent))
    with pytest.raises(IdempotencyConflictError, match="different request"):
        validate_creation_replay(row, replace(intent, actor_id=uuid4()))


@pytest.mark.parametrize("default", [False, True])
def test_legacy_binding_initialization_is_verified_without_live_resolution(default: bool) -> None:
    """旧 hash より後に追記した binding field を区別し、明示/default の選択を保持する。"""

    integration_id = uuid4()
    sources = {} if default else {"repo": f"integration:{integration_id}"}
    intent = creation_intent(sources=sources)
    command = replace(
        creation_command(intent, legacy=True),
        selected_sources_json={
            "repo": {
                "provider": "git",
                "capability": "repository.read/v1",
                "integration_id": str(integration_id),
                "candidate_key": f"integration:{integration_id}",
                "scope": {"paths": ["src/"]},
                "revision": "7",
                "source_binding_id": str(uuid4()) if default else None,
            }
        },
    )
    row = stored_creation(command)
    row.selected_sources_json["repo"].update(
        {
            "binding_id": str(uuid4()),
            "binding_checksum": "sha256:stored",
            "binding_capability": "repository.read/v1",
        }
    )
    before = deepcopy(row.selected_sources_json)
    validate_creation_replay(row, intent)
    assert row.selected_sources_json == before
    row.selected_sources_json["repo"]["scope"] = {"paths": ["private/"]}
    with pytest.raises(IdempotencyConflictError):
        validate_creation_replay(row, intent)


def test_legacy_document_intent_uses_original_candidate_not_current_members() -> None:
    """旧形式でも元の明示選択と hash が残れば、現在の Project を列挙せず照合する。"""

    intent = creation_intent(sources={"docs": ALL_DOCUMENTS_SELECTION})
    command = replace(
        creation_command(intent, legacy=True),
        selected_sources_json={
            "docs": {
                "provider": "project-documents",
                "capability": DOCUMENT_READ_CAPABILITY,
                "candidate_key": ALL_DOCUMENTS_SELECTION,
                "document_snapshot": {"documents": [str(uuid4())]},
            }
        },
    )
    validate_creation_replay(stored_creation(command), intent)


def test_legacy_missing_choice_fails_without_fabricating_history() -> None:
    """旧 Provider しか残っていない文書を、全集への同意として復元しない。"""

    intent = creation_intent(sources={"docs": ALL_DOCUMENTS_SELECTION})
    command = replace(
        creation_command(intent, legacy=True),
        selected_sources_json={
            "docs": {
                "provider": "project",
                "capability": DOCUMENT_READ_CAPABILITY,
            }
        },
    )
    with pytest.raises(IdempotencyConflictError):
        validate_creation_replay(stored_creation(command), intent)


@pytest.mark.asyncio
async def test_insert_conflict_returns_first_snapshot_without_adding_another_dispatch() -> None:
    """事前照会をすり抜けた同一要求も、唯一制約の勝者へ収斂させる。"""

    intent = creation_intent(sources={"docs": ALL_DOCUMENTS_SELECTION})
    original = creation_command(intent)
    row = stored_creation(original)
    session = MagicMock(spec=AsyncSession)
    inserted = MagicMock()
    inserted.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=inserted)
    found = MagicMock()
    found.one_or_none.return_value = row
    session.scalars = AsyncMock(return_value=found)
    changed = replace(original, selected_sources_json={"docs": {"members": [str(uuid4())]}})

    replay = await RunRepository(session).create_idempotent(changed)

    assert replay.run_id == row.id
    assert replay.idempotent_replay is True
    assert row.selected_sources_json == original.selected_sources_json
    session.add_all.assert_not_called()
    session.get.assert_not_called()
    query = session.scalars.call_args.args[0].compile().params
    assert query == {
        "project_id_1": row.project_id,
        "task_id_1": row.task_id,
        "idempotency_key_1": row.idempotency_key,
    }
