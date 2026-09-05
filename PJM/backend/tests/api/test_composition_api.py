"""Module(SkillComposition)API の契約と権限境界を検証する。"""

from __future__ import annotations

from uuid import UUID, uuid4

from fakes import FakeAuthService, FakeCompositionService
from fastapi.testclient import TestClient

from projectmind.auth.service import AuthenticatedActor


def test_list_modules_returns_project_scoped_records(client: TestClient) -> None:
    """成員は Project で有効な module と束縛 Skill の投影を取得できる。"""

    client.app.state.composition_service = FakeCompositionService()
    project_id = uuid4()
    response = client.get(f"/api/v1/projects/{project_id}/modules")

    assert response.status_code == 200
    modules = response.json()["modules"]
    assert len(modules) == 1
    assert modules[0]["project_id"] == str(project_id)
    assert modules[0]["skills"][0]["skill_name"] == "Skill 0"
    # 内部組合 field(behavior_prompt/config)は公開しない。
    assert "behavior_prompt" not in modules[0]


def test_create_module_records_bindings_and_returns_created(client: TestClient) -> None:
    """ADMIN の作成が 201 で束縛列を保持する。"""

    fake = FakeCompositionService()
    client.app.state.composition_service = fake
    version_id = str(uuid4())
    response = client.post(
        f"/api/v1/projects/{uuid4()}/modules",
        json={"name": "品质分析", "description": "模块说明", "skill_version_ids": [version_id]},
    )

    assert response.status_code == 201
    assert response.json()["name"] == "品质分析"
    assert fake.created == [("品质分析", [UUID(version_id)])]


def test_create_module_requires_at_least_one_binding(client: TestClient) -> None:
    """束縛なしの作成は request validation で 422 に落ちる。"""

    client.app.state.composition_service = FakeCompositionService()
    response = client.post(
        f"/api/v1/projects/{uuid4()}/modules",
        json={"name": "空模块", "skill_version_ids": []},
    )

    assert response.status_code == 422


def test_create_module_rejects_unpublished_skill(client: TestClient) -> None:
    """PUBLISHED でない SkillVersion への束縛は安定 code の 422 になる。"""

    client.app.state.composition_service = FakeCompositionService(invalid_skill=True)
    response = client.post(
        f"/api/v1/projects/{uuid4()}/modules",
        json={"name": "越权模块", "skill_version_ids": [str(uuid4())]},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "module_rejected"


def test_update_missing_module_folds_to_not_found(client: TestClient) -> None:
    """不存在/越権 module の更新は 404 の安定 Problem へ畳む。"""

    client.app.state.composition_service = FakeCompositionService(not_found=True)
    response = client.put(
        f"/api/v1/projects/{uuid4()}/modules/{uuid4()}",
        json={"name": "更名", "skill_version_ids": [str(uuid4())]},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "module_not_found"


def test_delete_module_returns_no_content(client: TestClient) -> None:
    """削除は 204 を返し、対象 ID を service へ渡す。"""

    fake = FakeCompositionService()
    client.app.state.composition_service = fake
    module_id = uuid4()
    response = client.delete(f"/api/v1/projects/{uuid4()}/modules/{module_id}")

    assert response.status_code == 204
    assert fake.deleted == [module_id]


def test_user_module_mutation_returns_administrator_required(client: TestClient) -> None:
    """USER は module を閲覧できるが、作成は 403 で拒否される(docs/04 権限表)。"""

    auth = FakeAuthService()
    auth.actor = AuthenticatedActor(
        user_id=auth.actor.user_id,
        organization_id=auth.actor.organization_id,
        email=auth.actor.email,
        display_name=auth.actor.display_name,
        system_role="USER",
    )
    client.app.state.auth_service = auth
    client.app.state.composition_service = FakeCompositionService()
    project_id = uuid4()

    listing = client.get(f"/api/v1/projects/{project_id}/modules")
    assert listing.status_code == 200

    mutation = client.post(
        f"/api/v1/projects/{project_id}/modules",
        headers={"Origin": "http://testserver", "X-CSRF-Token": auth.csrf_token},
        json={"name": "denied", "skill_version_ids": [str(uuid4())]},
    )
    assert mutation.status_code == 403
    assert mutation.json()["code"] == "administrator_required"
