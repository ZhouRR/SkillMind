"""Python の原数値を実 API wire に保つ、前後端が共有できる合成 fixture。"""

from __future__ import annotations

import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from projectmind.api.auth_dependencies import authenticated_project_actor
from projectmind.api.routes.task_flow import router
from projectmind.auth.service import AuthenticatedActor
from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.skills.service import TaskFlowPreviewResult
from projectmind.skills.task_flow_preview import (
    TaskFlowPreviewSource,
    project_task_flow_preview,
)
from tests.skills.task_flow_fixtures import make_task_flow_source

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


def make_numeric_flow_source() -> TaskFlowPreviewSource:
    """Unsafe integer、浮動小数点、再帰の原宣言を固定 identity の両契約へ配置する。"""

    source = make_task_flow_source()
    interpretation_id = UUID("55555555-5555-4555-8555-555555555555")
    manifest = deepcopy(source.manifest)
    manifest["identity"]["interpretation_id"] = str(interpretation_id)
    blueprint = manifest["capability_blueprint"]
    blueprint["identity"]["interpretation_id"] = str(interpretation_id)
    draft = {
        "contract_version": "projectmind.task-contract-draft/v1",
        "type": "object",
        "fields": [
            {
                "key": "large_integer",
                "type": "integer",
                "required": True,
                "minimum": -(10**100),
                "maximum": 10**100,
                "enum": [9007199254740992, 9007199254740993, 10**100],
            },
            {
                "key": "mixed_number",
                "type": "number",
                "required": True,
                "enum": [9007199254740993, 9007199254740992.0, 10**100, 1e100],
            },
            {
                "key": "float_extremes",
                "type": "number",
                "required": True,
                "minimum": -sys.float_info.max,
                "maximum": sys.float_info.max,
                "enum": [-sys.float_info.max, -0.0, 5e-324, sys.float_info.max],
            },
            {
                "key": "nested",
                "type": "array",
                "required": False,
                "items": {
                    "type": "object",
                    "fields": [
                        {
                            "key": "exact",
                            "type": "number",
                            "required": True,
                            "minimum": 1.0,
                            "maximum": 10**200,
                            "enum": [9007199254740993, 1.0, 1e100, 10**200],
                        }
                    ],
                },
            },
        ],
    }
    blueprint["tasks"][1]["parameter_contract"] = deepcopy(draft)
    blueprint["tasks"][1]["result_contract"] = deepcopy(draft)
    return replace(
        source,
        project_id=UUID("11111111-1111-4111-8111-111111111111"),
        skill_id=UUID("22222222-2222-4222-8222-222222222222"),
        skill_version_id=UUID("33333333-3333-4333-8333-333333333333"),
        skill_source_id=UUID("44444444-4444-4444-8444-444444444444"),
        interpretation_id=interpretation_id,
        manifest=manifest,
        manifest_checksum="sha256:" + sha256_hex(canonical_json(manifest)),
    )


def numeric_flow_wire(source: TaskFlowPreviewSource | None = None) -> bytes:
    """実 projector と route/serializer を通す。認可だけ合成し、main lifespan は起動しない。"""

    original = source if source is not None else make_numeric_flow_source()
    preview = project_task_flow_preview(
        source=original, task_key="explain", contracts_dir=CONTRACTS
    )
    result = TaskFlowPreviewResult(preview=preview, readiness=None)
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.state.skill_service = _NumericFixtureService(original, result)

    async def read_actor() -> AuthenticatedActor:
        """wire 比較に認証成功だけを合成し、実 DB の認可を検証したとは主張しない。"""

        return AuthenticatedActor(
            user_id=UUID("66666666-6666-4666-8666-666666666666"),
            organization_id=UUID("77777777-7777-4777-8777-777777777777"),
            email="numeric@example.invalid",
            display_name="Numeric fixture",
            system_role="USER",
        )

    application.dependency_overrides[authenticated_project_actor] = read_actor
    with TestClient(application) as client:
        response = client.get(
            f"/api/v1/projects/{original.project_id}/skill-versions/"
            f"{original.skill_version_id}/tasks/explain/flow-preview"
        )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")
    return bytes(response.content)


class _NumericFixtureService:
    """serializer 試験で実 projector の結果だけを返す、外部接続なしの最小 service port。"""

    def __init__(self, source: TaskFlowPreviewSource, result: TaskFlowPreviewResult) -> None:
        """原 exact scope と immutable 投影を保持する。"""

        self.source = source
        self.result = result

    async def get_task_flow_preview(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        skill_version_id: UUID,
        task_key: str,
    ) -> TaskFlowPreviewResult:
        """別 Task への fallback をせず、呼出 scope と元 fixture の一致を検査する。"""

        assert organization_id == UUID("77777777-7777-4777-8777-777777777777")
        assert (project_id, skill_version_id, task_key) == (
            self.source.project_id,
            self.source.skill_version_id,
            "explain",
        )
        return self.result
