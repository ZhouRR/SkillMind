"""原 upload の公開五項目と既存文書九項目の契約・状態整合を検証する。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from projectmind.api.main import create_app

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def _load(path: str) -> dict[str, Any]:
    """リポジトリの合成契約だけを読み、外部 reference を解決しない。"""

    value = json.loads((CONTRACTS / path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.fixture
def validator() -> Draft202012Validator:
    """CLI と同じ checker を使い、Schema の timezone 制約を単独で検証する。"""

    return Draft202012Validator(
        _load("documents/v1/upload.schema.json"), format_checker=FormatChecker(),
    )


@pytest.mark.parametrize("state", ["pending", "published"])
def test_original_upload_examples_are_valid(
    validator: Draft202012Validator, state: str,
) -> None:
    """未確定と公開済みを別 example とし、どちらも明示五項目を要求する。"""

    example = _load(f"examples/document-upload-{state}.v1.json")
    validator.validate(example)
    assert set(example) == {"upload_key", "project_id", "state", "created_at", "document"}


@pytest.mark.parametrize("field", ["upload_key", "project_id", "state", "created_at", "document"])
def test_upload_receipt_never_defaults_missing_fields(
    validator: Draft202012Validator, field: str,
) -> None:
    """null と欠落を区別し、旧応答や途中の投影を成功として受理しない。"""

    example = _load("examples/document-upload-pending.v1.json")
    del example[field]
    assert not validator.is_valid(example)


@pytest.mark.parametrize("field,value", [
    ("upload_key", "00000000-0000-0000-0000-000000000000"),
    ("upload_key", "not-a-uuid"), ("project_id", "not-a-uuid"),
    ("state", "UNKNOWN"), ("created_at", "2026-09-10T12:00:00"),
    ("document", {}), ("private_key", "never-public"),
])
def test_upload_receipt_rejects_malformed_or_private_fields(
    validator: Draft202012Validator, field: str, value: object,
) -> None:
    """不正な識別子・日時・追加属性を欠損と同様に拒否する。"""

    example = _load("examples/document-upload-pending.v1.json")
    example[field] = value
    assert not validator.is_valid(example)


@pytest.mark.parametrize("state", ["pending", "published"])
def test_upload_state_and_document_nullability_cannot_disagree(
    validator: Draft202012Validator, state: str,
) -> None:
    """PENDING に文書を捏造せず、PUBLISHED を metadata 無しで受理しない。"""

    example = _load(f"examples/document-upload-{state}.v1.json")
    example["state"] = "PUBLISHED" if state == "pending" else "PENDING"
    assert not validator.is_valid(example)


def test_embedded_document_contract_matches_original_nine_fields() -> None:
    """self-contained Schema の複製部分が既存の公開文書と分岐しないよう固定する。"""

    document = _load("documents/v1/document.schema.json")
    document.pop("$schema")
    document.pop("$id")
    assert _load("documents/v1/upload.schema.json")["$defs"]["document"] == document


@pytest.mark.parametrize("field,value", [
    ("storage_key", "private"), ("size", True), ("size", -1),
    ("checksum", "sha256:invalid"), ("created_at", "2026-09-10T12:00:00"),
])
def test_published_receipt_keeps_strict_document_projection(
    validator: Draft202012Validator, field: str, value: object,
) -> None:
    """元 metadata でも保存 key と不正な基本値を公開成功に変換しない。"""

    example = _load("examples/document-upload-published.v1.json")
    example["document"][field] = value
    assert not validator.is_valid(example)


def test_openapi_upload_receipt_declares_same_state_nullability() -> None:
    """実 app の response Schema も未確定と公開済みの null 制約を宣言する。"""

    schemas = create_app().openapi()["components"]["schemas"]
    schema = schemas["DocumentUploadResponse"]
    assert set(schema["required"]) == {
        "upload_key", "project_id", "state", "created_at", "document",
    }
    assert schema["additionalProperties"] is False
    assert schema["if"]["properties"]["state"] == {"const": "PENDING"}
    assert schema["then"]["properties"]["document"] == {"type": "null"}
    assert schema["else"]["properties"]["document"] == {"type": "object"}
    assert schema["properties"]["upload_key"]["not"] == {
        "const": "00000000-0000-0000-0000-000000000000",
    }
