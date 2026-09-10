"""既存の SkillVersion 公開投影と四つの自己完結 Schema の同期を守る。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel

from projectmind.api.routes.skills import (
    ProjectSkillVersionListResponse,
    ProjectSkillVersionResponse,
    SkillVersionListResponse,
    SkillVersionResponse,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
CASES = (
    ("version/v1", "skill-version.v1", SkillVersionResponse),
    ("version/v1-list", "skill-version-list.v1", SkillVersionListResponse),
    ("project-enablement/v1", "project-skill-version.v1", ProjectSkillVersionResponse),
    (
        "project-enablement/v1-list",
        "project-skill-version-list.v1",
        ProjectSkillVersionListResponse,
    ),
)
SCHEMAS = tuple(case[0] for case in CASES)


def _load(path: str) -> dict[str, Any]:
    """登録済み合成契約だけを読み、環境設定や保存された Skill へ接続しない。"""
    value = json.loads((CONTRACTS / path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _validator(name: str) -> Draft202012Validator:
    """同じ公開 Schema と標準 format 検査で実投影を判定する。"""
    return Draft202012Validator(_load(f"skills/{name}.schema.json"), format_checker=FormatChecker())


def _response(name: str, description: str) -> BaseModel:
    """実 API response model で四種類の envelope を組み立てる。"""
    identity = UUID("00000000-0000-4000-8000-000000000001")
    now = datetime(2026, 9, 10, tzinfo=UTC)
    version = SkillVersionResponse(
        skill_id=identity,
        skill_version_id=identity,
        skill_source_id=identity,
        interpretation_id=identity,
        organization_id=identity,
        skill_key="synthetic-review",
        name="Synthetic review",
        description=description,
        version="1.0.0",
        status="DRAFT",
        manifest_checksum="sha256:" + "a" * 64,
        manifest={"manifest_version": "projectmind/v1alpha1"},
        gate_passed=False,
        gate_findings=[],
        interpretation_diff={},
        created_at=now,
        published_by=None,
        published_at=None,
    )
    enabled = ProjectSkillVersionResponse(
        project_id=identity,
        organization_id=identity,
        skill_version=version,
        enabled_by=identity,
        enabled_at=now,
        disabled_at=None,
    )
    responses: dict[str, BaseModel] = {
        "version/v1": version,
        "version/v1-list": SkillVersionListResponse(skill_versions=[version]),
        "project-enablement/v1": enabled,
        "project-enablement/v1-list": ProjectSkillVersionListResponse(skill_versions=[enabled]),
    }
    return responses[name]


def _version(body: dict[str, Any], name: str) -> dict[str, Any]:
    """Envelope を保持したまま、反例を適用する実 version object を選ぶ。"""
    current = body["skill_versions"][0] if name.endswith("-list") else body
    value = current["skill_version"] if name.startswith("project-enablement/") else current
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("name", SCHEMAS)
def test_all_version_definitions_match_the_public_response_fields(name: str) -> None:
    """一箇所だけの追加や nested version の required 漏れを検出する。"""
    schema = _load(f"skills/{name}.schema.json")
    detail = _load("skills/version/v1.schema.json")
    canonical = {
        key: value for key, value in detail.items() if key not in {"$schema", "$id", "title"}
    }
    definition = canonical if name == "version/v1" else schema["$defs"]["version"]
    assert definition == canonical
    model = SkillVersionResponse.model_json_schema()
    assert set(definition["properties"]) == set(model["properties"])
    assert set(definition["required"]) == set(model["required"])
    assert definition["properties"]["description"] == {"type": "string"}
    assert definition["additionalProperties"] is False


@pytest.mark.parametrize("name", SCHEMAS)
@pytest.mark.parametrize("description", ["", "Synthetic source description.", "説明の原文。"])
def test_actual_response_serialization_matches_all_four_schemas(
    name: str, description: str
) -> None:
    """空文字を含む既存公開 description を、実 model serialization のまま受け入れる。"""
    body = _response(name, description).model_dump(mode="json")
    _validator(name).validate(body)
    assert _version(body, name)["description"] == description


@pytest.mark.parametrize("name", SCHEMAS)
@pytest.mark.parametrize("value", [None, False, 0, [], {}])
def test_description_does_not_accept_non_string_values(name: str, value: Any) -> None:
    """省略値や型補正で既存必須文字列を成功に変換しない。"""
    body = _response(name, "Original description").model_dump(mode="json")
    _version(body, name)["description"] = value
    assert not _validator(name).is_valid(body)


@pytest.mark.parametrize("name", SCHEMAS)
def test_description_remains_required(name: str) -> None:
    """四つの envelope で description 欠落を同じように拒否する。"""
    body = _response(name, "Original description").model_dump(mode="json")
    del _version(body, name)["description"]
    assert not _validator(name).is_valid(body)


@pytest.mark.parametrize("name", SCHEMAS)
@pytest.mark.parametrize("location", ["envelope", "version"])
def test_unknown_fields_are_still_rejected(name: str, location: str) -> None:
    """公開済み一項目の同期を、allowlist 全体の無効化で代替しない。"""
    body = _response(name, "Original description").model_dump(mode="json")
    target = body if location == "envelope" else _version(body, name)
    target["unexpected_private_field"] = "synthetic"
    assert not _validator(name).is_valid(body)


@pytest.mark.parametrize("name,example,response_model", CASES)
def test_registered_examples_match_schema_and_actual_response_model(
    name: str, example: str, response_model: type[BaseModel]
) -> None:
    """既存四 example が公開必須項目を含み、API model の補完なしで読めることを守る。"""
    body = _load(f"examples/{example}.json")
    _validator(name).validate(body)
    assert response_model.model_validate(body).model_dump(mode="json") == body
