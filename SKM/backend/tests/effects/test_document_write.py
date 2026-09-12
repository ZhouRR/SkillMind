"""汎用提案入口から原 Artifact 保存要求までの契約を検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from skillmind.artifacts.repository import ArtifactRepository
from skillmind.documents.library import DocumentLibraryTarget
from skillmind.effects.document_command import load_document_effect_command
from skillmind.effects.document_write import validate_document_write_proposal
from skillmind.effects.domain import ChangeProposalValidationError
from skillmind.effects.proposal import parse_change_proposal_request
from skillmind.effects.release import ExecutionFeatures
from tests.documents.test_document_library_binding import target
from tests.storage.test_object_effect import fixture as object_fixture


@pytest.mark.parametrize(
    "deferred,database", [(False, False), (True, False), (False, True), (True, True)]
)
def test_existing_switches_cannot_enable_document_effects(deferred, database):
    """旧 switch を単独/同時に開いても文書庫保存の権限へ流用しない。"""

    assert not ExecutionFeatures(deferred, database).effect_enabled("document.write/v1", "CREATE")


def test_internal_document_switch_is_create_only_and_does_not_open_other_writers():
    """文書庫の内部门禁は原 CREATE と提案だけを有効化する。"""

    features = ExecutionFeatures(document_writes=True)
    assert features.write_capabilities == frozenset({"document.write/v1"})
    assert features.effect_enabled("document.write/v1", "CREATE")
    assert not features.effect_enabled("document.write/v1", "UPDATE")
    assert features.capability_enabled("change.propose/v1")
    assert not features.capability_enabled("subagent.dispatch/v1")


def request():
    """公開 change.propose の checkpoint/証拠形を保ち、文書保存の承認内容を構築する。"""
    path = Path(__file__).resolve().parents[3] / "contracts/examples/change-propose-request.v1.json"
    value = json.loads(path.read_text())
    value.update(
        effect_intent_key="save_document",
        resource_key="review_outputs",
        capability_version="document.write/v1",
        operation="CREATE",
        target={"locator": "results/review/source.md", "display": "Review source"},
        changes=[
            {
                "path": "/document",
                "action": "SET",
                "value": {
                    "artifact_ref": "art_fixture",
                    "content_hash": "sha256:" + "a" * 64,
                    "size_bytes": 10,
                    "mime_type": "text/markdown",
                },
            }
        ],
        precondition={"revision": "absent"},
        verification={"method": "READ_BACK", "paths": ["/document"]},
    )
    return value


async def test_public_proposal_describes_original_artifact_without_model_body_or_storage_address(
    monkeypatch,
):
    """path/hash/size/MIME と原 Artifact を批准し、物理 key は Project binding から導出する。"""
    source, original, arguments = object_fixture()
    project_id = original.project_id
    library = DocumentLibraryTarget(source.namespace, original.bucket)
    scope = library.scope(project_id)
    value = request()
    value["changes"][0]["value"].update(
        content_hash=original.content_checksum, size_bytes=len(original.content)
    )
    payload = validate_document_write_proposal(
        parse_change_proposal_request(value), binding_scope=scope
    )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "contracts/tools/document.write/v1/request.schema.json"
        ).read_text()
    )
    monkeypatch.setattr(
        ArtifactRepository, "get_content", AsyncMock(return_value=arguments["artifact"])
    )
    command = await load_document_effect_command(
        MagicMock(), effect_id=original.effect_id, project_id=project_id,
        run_id=original.run_id, payload=payload, target=library,
    )
    # 物理 key は Effect ID 確定後の最終 payload だけに入る。公開 Schema はその形を保持する。
    Draft202012Validator(schema).validate({**payload, "object_key": command.object_key})
    assert command == original
    assert payload == {
        "path": "results/review/source.md",
        **value["changes"][0]["value"],
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"body": "new text"},
        {"bucket": "another"},
        {"object_key": "somewhere"},
        {"artifact_ref": "arbitrary-file"},
        {"size_bytes": True},
        {"size_bytes": 0},
        {"size_bytes": 1_048_577},
        {"content_hash": "a" * 64},
        {"mime_type": "text/html"},
    ],
)
def test_model_body_and_inconsistent_artifact_description_are_rejected(changes):
    """保存する byte を提案本文で差し替えたり、size/MIME を無制限にしたりできない。"""
    value = request()
    value["changes"][0]["value"].update(changes)
    with pytest.raises(ChangeProposalValidationError):
        validate_document_write_proposal(
            parse_change_proposal_request(value), binding_scope=target().scope(uuid4())
        )


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.md",
        "results/../source.md",
        "results//source.md",
        "results/source.md ",
        "results/./source.md",
        "a/",
    ],
)
def test_path_is_not_normalized_into_a_different_approved_target(path):
    """曖昧な path を別の場所へ正規化せず、原提案を拒否する。"""
    value = request()
    value["target"]["locator"] = path
    with pytest.raises(ChangeProposalValidationError):
        validate_document_write_proposal(
            parse_change_proposal_request(value), binding_scope=target().scope(uuid4())
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": "UPDATE"},
        {"precondition": {"revision": "any"}},
        {"verification": {"method": "READ_BACK", "paths": ["/other"]}},
    ],
)
def test_unconditional_or_overwrite_proposal_cannot_enter_document_create(changes):
    """原不在と一つの文書回读だけを許し、汎用 write への退避を拒否する。"""
    value = deepcopy(request())
    value.update(changes)
    with pytest.raises(ChangeProposalValidationError):
        validate_document_write_proposal(
            parse_change_proposal_request(value), binding_scope=target().scope(uuid4())
        )
