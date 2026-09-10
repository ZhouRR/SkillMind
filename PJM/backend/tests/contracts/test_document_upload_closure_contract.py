"""独立閉鎖の厳密五項目・明示要求・OpenAPI が元 upload 契約を変更しないことを守る。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from projectmind.api.main import create_app

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
FIELDS = {"upload_key", "project_id", "document_id", "closed_at", "publication_state"}


def _load(path: str) -> dict[str, Any]:
    """自己完結した契約と合成 example のみを読み、外部参照を取得しない。"""

    value = json.loads((CONTRACTS / path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("name", ["upload-closure", "upload-closure-request"])
def test_closure_examples_validate_against_standalone_contract(name: str) -> None:
    """Schema と example を独立版として登録し、古い upload response を流用しない。"""

    validator = Draft202012Validator(
        _load(f"documents/v1/{name}.schema.json"),
        format_checker=FormatChecker(),
    )
    validator.validate(_load(f"examples/document-{name}.v1.json"))


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_closure_receipt_requires_every_original_field(field: str) -> None:
    """欠落 field を現在一覧や既定値から補完しない。"""

    validator = Draft202012Validator(
        _load("documents/v1/upload-closure.schema.json"),
        format_checker=FormatChecker(),
    )
    example = _load("examples/document-upload-closure.v1.json")
    del example[field]
    assert not validator.is_valid(example)


@pytest.mark.parametrize(
    "field,value",
    [
        ("upload_key", "not-a-uuid"),
        ("project_id", "not-a-uuid"),
        ("document_id", "not-a-uuid"),
        *[
            (field, "00000000-0000-0000-0000-000000000000")
            for field in ("upload_key", "project_id", "document_id")
        ],
        ("closed_at", "2026-09-10T12:00:00"),
        ("closed_at", None),
        ("publication_state", "PUBLISHED"),
        ("publication_state", "CLEANED"),
        ("storage_key", "private"),
        ("quota_released", True),
    ],
)
def test_closure_receipt_rejects_corrupt_identity_and_unproven_cleanup(
    field: str,
    value: object,
) -> None:
    """閉鎖は公開だけを表し、清理・占用解放の状態や私有対象を追加しない。"""

    validator = Draft202012Validator(
        _load("documents/v1/upload-closure.schema.json"),
        format_checker=FormatChecker(),
    )
    example = _load("examples/document-upload-closure.v1.json")
    example[field] = value
    assert not validator.is_valid(example)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"confirmation": False},
        {"confirmation": "DELETE"},
        {"confirmation": "STOP_PUBLICATION", "delete": True},
    ],
)
def test_closure_request_never_defaults_confirmation(body: object) -> None:
    """別操作や曖昧な入力を元 key の不可逆閉鎖に変換しない。"""

    validator = Draft202012Validator(_load("documents/v1/upload-closure-request.schema.json"))
    assert not validator.is_valid(body)


def test_openapi_closure_declares_distinct_receipt_statuses_and_no_store_problems() -> None:
    """実 app に POST/GET・初回/重放 status・元 UUID・認可拒否を同時に宣言する。"""

    spec = create_app().openapi()
    schemas = spec["components"]["schemas"]
    response = schemas["DocumentUploadClosureResponse"]
    assert set(response["required"]) == set(response["properties"]) == FIELDS
    assert response["additionalProperties"] is False
    for field in ("upload_key", "project_id", "document_id"):
        assert response["properties"][field]["format"] == "uuid"
        assert response["properties"][field]["not"] == {
            "const": "00000000-0000-0000-0000-000000000000",
        }
    assert response["properties"]["publication_state"]["const"] == "CLOSED"
    request = schemas["DocumentUploadClosureRequest"]
    assert request["required"] == ["confirmation"] and request["additionalProperties"] is False
    assert request["properties"]["confirmation"]["const"] == "STOP_PUBLICATION"
    operations = spec["paths"][
        "/api/v1/projects/{project_id}/document-uploads/{upload_key}/closure"
    ]
    assert set(operations) == {"get", "post"}
    for method, statuses in (
        ("get", (401, 403, 404, 422, 503)),
        ("post", (401, 403, 404, 409, 422, 503)),
    ):
        operation = operations[method]
        key = next(value for value in operation["parameters"] if value["name"] == "upload_key")
        assert key["required"] is True and key["schema"]["format"] == "uuid"
        for status in statuses:
            problem = operation["responses"][str(status)]
            assert "application/problem+json" in problem["content"]
            assert "Cache-Control" in problem["headers"]
        for status in (200, 201) if method == "post" else (200,):
            success = operation["responses"][str(status)]
            assert success["content"]["application/json"]["schema"]["$ref"].endswith(
                "/DocumentUploadClosureResponse"
            )
            assert "Cache-Control" in success["headers"]
    assert "requestBody" not in operations["get"]
    assert operations["post"]["requestBody"]["required"] is True
    original = schemas["DocumentUploadResponse"]
    assert original["properties"]["state"]["enum"] == ["PENDING", "PUBLISHED"]
    assert set(original["required"]) == {
        "upload_key",
        "project_id",
        "state",
        "created_at",
        "document",
    }
