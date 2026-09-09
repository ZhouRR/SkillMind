"""Project の公開入力が旧版省略・型変換・全 null 更新を受け付けないことを固定する。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from projectmind.api.routes.projects import ProjectVersionRequest, UpdateProjectRequest
from projectmind.projects.domain import MAX_PROJECT_VERSION, UpdateProjectCommand


@pytest.mark.parametrize("version", [True, False, 1.0, "1", None, 0, -1, MAX_PROJECT_VERSION + 1])
@pytest.mark.parametrize("request_type", [ProjectVersionRequest, UpdateProjectRequest])
def test_json_version_requires_strict_integer_without_coercion(
    version: object,
    request_type: type[ProjectVersionRequest] | type[UpdateProjectRequest],
) -> None:
    """JSON Schema の数学的 integer 判定だけに依存せず、HTTP model の strict 型を検証する。"""

    body = {"expected_row_version": version}
    if request_type is UpdateProjectRequest:
        body["name"] = "Name"
    with pytest.raises(ValidationError):
        request_type.model_validate(body)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": "Name"},
        {"expected_row_version": 1},
        {
            "expected_row_version": 1,
            "name": None,
            "description": None,
            "settings": None,
            "retention_days": None,
        },
        {"expected_row_version": 1, "name": "Name", "key": "forbidden"},
    ],
)
def test_patch_requires_original_version_and_a_non_null_edit(body: object) -> None:
    """元版のない旧 client や全 null PATCH を、現版/空変更へ補完しない。"""

    with pytest.raises(ValidationError):
        UpdateProjectRequest.model_validate(body)


@pytest.mark.parametrize("fields", [{"description": ""}, {"settings": {}}, {"name": "Name"}])
def test_patch_keeps_valid_empty_values_and_ignores_other_null_fields(
    fields: dict[str, object],
) -> None:
    """空文字/空 object は実値であり、truthiness による見落としを防ぐ。"""

    result = UpdateProjectRequest.model_validate({"expected_row_version": 1, **fields})
    assert result.expected_row_version == 1


def test_internal_update_command_rejects_version_only() -> None:
    """HTTP を通らない caller も版だけの更新を作れない。"""

    with pytest.raises(ValueError):
        UpdateProjectCommand(expected_row_version=1)
