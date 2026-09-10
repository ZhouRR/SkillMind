"""新旧 Tool と Artifact API/Result の公開契約を実 OpenAPI と合わせて検証する。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from projectmind.api.main import create_app

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _load(relative: str) -> Any:
    """Schema と JSON array example を外部参照なしで読む。"""

    return json.loads((CONTRACTS / relative).read_text(encoding="utf-8"))


def test_workspace_write_versions_preserve_request_but_separate_receipts() -> None:
    """同じ入力でも v1 の成功を Artifact 保存と扱わず、v2 に明示回执を要求する。"""

    first = _load("tools/workspace.write/v1/request.schema.json")
    second = _load("tools/workspace.write/v2/request.schema.json")
    assert {key: value for key, value in first.items() if key not in {"$id", "title"}} == {
        key: value for key, value in second.items() if key not in {"$id", "title"}
    }
    v1 = Draft202012Validator(_load("tools/workspace.write/v1/response.schema.json"))
    v2 = Draft202012Validator(_load("tools/workspace.write/v2/response.schema.json"))
    old = _load("examples/workspace-write-response.v1.json")
    output = _load("examples/workspace-write-response.v2.json")
    draft = _load("examples/workspace-write-draft-response.v2.json")
    v1.validate(old)
    assert not v2.is_valid(old)
    for payload in (output, draft):
        v2.validate(payload)
        assert not v1.is_valid(payload)
    output["artifact_refs"] = []
    draft["artifact_refs"] = ["art_forged"]
    assert not v2.is_valid(output) and not v2.is_valid(draft)


def test_artifact_list_shape_and_download_headers_are_explicit() -> None:
    """一覧は上限付き十項目、内容は完全200添付であり署名 URL/JSON や range ではない。"""

    app = create_app().openapi()
    paths = app["paths"]
    prefix = "/api/v1/projects/{project_id}/runs/{run_id}/artifacts"
    operation = paths[prefix]["get"]
    api_schema = {**operation["responses"]["200"]["content"]["application/json"]["schema"],
                  "components": app["components"]}
    schemas = (_load("artifacts/v1/list.schema.json"), api_schema)
    example = _load("examples/artifact-list.v1.json")
    for schema in schemas:
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        validator.validate(example)
        validator.validate([])
        assert not validator.is_valid(example * 101)
        for field in example[0]:
            candidate = deepcopy(example)
            del candidate[0][field]
            assert not validator.is_valid(candidate)
        candidate = deepcopy(example)
        candidate[0]["content"] = "private"
        assert not validator.is_valid(candidate)
    download = paths[prefix + "/{artifact_ref}/content"]["get"]["responses"]
    assert set(download["200"]["content"]) == {"text/plain"}
    assert "206" not in download and "302" not in download and "304" not in download
    assert download["200"]["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    assert download["200"]["headers"]["X-Content-Type-Options"]["schema"]["const"] == "nosniff"
    assert "Content-Disposition" in download["200"]["headers"]
    for code in ("401", "403", "404", "409", "422", "503"):
        assert set(download[code]["content"]) == {"application/problem+json"}
        headers = download[code]["headers"]
        assert headers["Cache-Control"]["schema"]["const"] == "no-store"
        assert ("X-Content-Type-Options" in headers) == (code in {"409", "503"})


@pytest.mark.parametrize("field,value", [
    ("artifact_ref", "https://example.invalid/file"), ("artifact_ref", "art_" + "a" * 61),
    ("size_bytes", -1), ("size_bytes", 1_048_577), ("mime_type", "text/html"),
    ("path", "workspace/draft.txt"), ("checksum", "sha256:" + "F" * 64),
    ("created_at", "2026-09-10T00:00:00"),
])
def test_metadata_rejects_invalid_public_values(field: str, value: object) -> None:
    """上限・定位・MIME・checksum・時刻を Schema/実 OpenAPI で一致させる。"""

    openapi = create_app().openapi()
    payload = _load("examples/artifact-list.v1.json")
    payload[0][field] = value
    for schema in (
        _load("artifacts/v1/list.schema.json"),
        {"type": "array", "items": {"$ref": "#/components/schemas/ArtifactMetadataResponse"},
         "components": openapi["components"]},
    ):
        assert not Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(payload)


@pytest.mark.parametrize("kind", ["OUTCOME_ENVELOPE", "STRUCTURED_OUTPUT"])
@pytest.mark.parametrize("version", ["v1", "v2"])
def test_result_scope_version_requires_exact_artifact_guarantees(kind: str, version: str) -> None:
    """v1 を後付けの通過へ格上げせず、v2 は全成功 flag と内容核対を必須にする。"""

    openapi = create_app().openapi()
    payload = _load("examples/run-detail-artifacts.v1.json")
    payload["result"]["result_kind"] = kind
    validation = payload["result"]["validation"]
    checks = validation["reference_checks"]
    checks["effects"] = "PLATFORM_RECORD_MATCH" if kind == "OUTCOME_ENVELOPE" else "NOT_APPLICABLE"
    checks["version"] = f"projectmind.result-reference-checks/{version}"
    checks["artifacts"] = "NOT_VERIFIED" if version == "v1" else "RUN_OWNERSHIP_AND_CONTENT"
    validation["artifact_refs_valid"] = version == "v2"
    validators = (
        Draft202012Validator(_load("runs/detail/v1.schema.json")),
        Draft202012Validator({"$ref": "#/components/schemas/RunDetailResponse",
                              "components": openapi["components"]}),
    )
    for validator in validators:
        validator.validate(payload)
    validation["artifact_refs_valid"] = version != "v2"
    assert all(not validator.is_valid(payload) for validator in validators)
    del validation["artifact_refs_valid"]
    assert all(validator.is_valid(payload) == (version == "v1") for validator in validators)
