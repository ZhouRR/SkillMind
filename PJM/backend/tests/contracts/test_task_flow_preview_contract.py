"""Task Flow の読取契約・元宣言・公開 Schema の対応を検証する。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from projectmind.api.main import create_app
from projectmind.api.routes.task_flow import TaskFlowPreviewResponse, _preview_response
from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.skills.resource_binding import evaluate_blueprint_readiness
from projectmind.skills.service import TaskFlowPreviewResult
from projectmind.skills.task_flow_preview import project_task_flow_preview
from tests.skills.task_flow_fixtures import make_task_flow_source

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
SCHEMA = "tasks/flow-preview/v1.schema.json"
ENDPOINT = (
    "/api/v1/projects/{project_id}/skill-versions/{skill_version_id}/tasks/{task_key}/flow-preview"
)
FIELDS = {
    "preview_version",
    "identity",
    "status",
    "blueprint_checksum",
    "preview_checksum",
    "plan",
    "source_traces",
    "readiness",
}


def _load(name: str) -> dict[str, Any]:
    """自己完結した合成契約だけを読み、source/storage/設定にはアクセスしない。"""

    result = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    assert isinstance(result, dict)
    return result


def _validator() -> Draft202012Validator:
    """Standalone Schema の format も有効にして検証する。"""

    return Draft202012Validator(_load(SCHEMA), format_checker=FormatChecker())


def _example() -> dict[str, Any]:
    """実 projector で生成した AVAILABLE example を毎回独立に読む。"""

    return _load("examples/task-flow-preview.v1.json")


@pytest.mark.parametrize("name", ["task-flow-preview", "task-flow-preview-not-declared"])
def test_preview_examples_preserve_server_checksum_and_original_fields(name: str) -> None:
    """現在 readiness は原 preview 身分に含めず、原任意 field を null に補わない。"""

    body = _load(f"examples/{name}.v1.json")
    _validator().validate(body)
    parsed = TaskFlowPreviewResponse.model_validate_json(canonical_json(body))
    assert parsed.model_dump(mode="json", exclude_unset=True) == body
    semantic = {
        key: value
        for key, value in body.items()
        if key
        not in {
            "preview_checksum",
            "readiness",
        }
    }
    assert body["preview_checksum"] == f"sha256:{sha256_hex(canonical_json(semantic))}"


@pytest.mark.parametrize("declared", [True, False])
def test_examples_equal_the_current_production_projection(declared: bool) -> None:
    """Web が読む例を実 projector/API と照合し、古い手製 JSON の通過で代用しない。"""

    identities = [UUID(f"00000000-0000-4000-8000-{index:012d}") for index in range(1, 6)]
    with patch("tests.skills.task_flow_fixtures.uuid4", side_effect=identities):
        source = make_task_flow_source()
    readiness = None
    if declared:
        readiness = evaluate_blueprint_readiness(
            source.manifest["capability_blueprint"],
            candidates=(),
            registered_capabilities=frozenset(),
        )
    else:
        manifest = dict(source.manifest)
        del manifest["capability_blueprint"]
        source = replace(
            source,
            manifest=manifest,
            manifest_checksum=f"sha256:{sha256_hex(canonical_json(manifest))}",
        )
    response = _preview_response(
        TaskFlowPreviewResult(
            preview=project_task_flow_preview(
                source=source, task_key="explain", contracts_dir=CONTRACTS
            ),
            readiness=readiness,
        ),
        project_id=source.project_id,
        skill_version_id=source.skill_version_id,
        task_key="explain",
    )
    name = "task-flow-preview" if declared else "task-flow-preview-not-declared"
    assert response.model_dump(mode="json", exclude_unset=True) == _load(f"examples/{name}.v1.json")


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_preview_requires_every_public_field(field: str) -> None:
    """原版・状態・評価範囲の欠落を既定の成功に変換しない。"""

    body = _example()
    del body[field]
    assert not _validator().is_valid(body)


@pytest.mark.parametrize(
    "field,value",
    [
        ("preview_version", "projectmind.task-flow-preview/v2"),
        ("status", "RUNNING"),
        ("status", "NOT_DECLARED"),
        ("blueprint_checksum", None),
        ("preview_checksum", "sha256:invalid"),
        ("plan", None),
        ("source_traces", []),
        ("run_id", "00000000-0000-4000-8000-000000000001"),
        ("progress", 80),
        ("flow_version", "projectmind.task-flow/v1"),
        ("nodes", []),
        ("edges", []),
    ],
)
def test_preview_rejects_corruption_and_unproven_run_state(field: str, value: object) -> None:
    """Preview は未来の凍結計画や実行事実を保存する契約ではない。"""

    body = _example()
    body[field] = value
    assert not _validator().is_valid(body)


@pytest.mark.parametrize("field", ["project_id", "skill_id", "skill_version_id", "task_id"])
@pytest.mark.parametrize("value", ["not-a-uuid", None])
def test_preview_identity_is_never_reconstructed_from_a_title(field: str, value: object) -> None:
    """原 UUID が壊れている場合は同名や最新 Task へ付け替えない。"""

    body = _example()
    body["identity"][field] = value
    assert not _validator().is_valid(body)


@pytest.mark.parametrize("field", ["success_criteria", "resource_keys", "deliverables"])
def test_original_optional_collections_reject_explicit_null(field: str) -> None:
    """元 Blueprint の欠落と型損傷を公開 Schema でも区別する。"""

    body = _example()
    body["plan"]["task"]["value"][field] = None
    assert not _validator().is_valid(body)


@pytest.mark.parametrize(
    "field,value",
    [
        ("line", 0),
        ("line", True),
        ("line", "1"),
        ("verification", "SEMANTICALLY_VERIFIED"),
        ("verification", "BLOB_READ"),
        ("path", ""),
        ("target", "not-a-pointer"),
    ],
)
def test_source_trace_does_not_claim_unperformed_verification(field: str, value: object) -> None:
    """位置と読み取り範囲を保持し、意味や binary の取得証明へ格上げしない。"""

    body = _example()
    body["source_traces"][0][field] = value
    assert not _validator().is_valid(body)


def test_not_declared_is_not_an_empty_available_plan() -> None:
    """原 Blueprint 未宣言だけが null 計画であり、現在評価も補造しない。"""

    body = _load("examples/task-flow-preview-not-declared.v1.json")
    assert body["status"] == "NOT_DECLARED" and body["plan"] is None
    body["readiness"]["assessment"] = {"level": "RUNNABLE", "requirements": []}
    assert not _validator().is_valid(body)


def test_index_only_source_cannot_claim_a_verified_line() -> None:
    """Binary file 索引の存在は行内容や行範囲の読み取り証明にならない。"""

    body = _example()
    trace = body["source_traces"][0]
    trace["verification"] = "SOURCE_INDEX"
    trace["line"] = 999
    assert not _validator().is_valid(body)
    trace["line"] = None
    _validator().validate(body)


def test_preview_schema_matches_explicit_public_response_model() -> None:
    """JSON Schema を route と別の弱い契約へ逸脱させない。"""

    stored = _load(SCHEMA)
    del stored["$schema"]
    del stored["$id"]
    assert stored == TaskFlowPreviewResponse.model_json_schema(mode="serialization")


def test_task_preview_openapi_exposes_only_a_no_store_get_with_stable_problems() -> None:
    """公開読取・identity・Problem を一緒に宣言し、書込や Run API を新設しない。"""

    spec = create_app().openapi()
    operations = spec["paths"][ENDPOINT]
    assert set(operations) == {"get"}
    operation = operations["get"]
    assert "requestBody" not in operation
    assert "auth" in operation["tags"]
    assert {item["name"] for item in operation["parameters"]} >= {
        "project_id",
        "skill_version_id",
        "task_key",
    }
    for status in (200, 401, 403, 404, 409, 422, 503):
        response = operation["responses"][str(status)]
        assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
        media = "application/json" if status == 200 else "application/problem+json"
        assert media in response["content"]
    schema = spec["components"]["schemas"]["TaskFlowPreviewResponse"]
    assert set(schema["required"]) == set(schema["properties"]) == FIELDS
    assert schema["additionalProperties"] is False
