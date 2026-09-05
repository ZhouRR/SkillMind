"""Skill 解析・保存・draft・publish API の契約を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fakes import FakeArqPool, FakeRunService, FakeSkillService
from fastapi.testclient import TestClient

from projectmind.runs.domain import CreatedRun, RunStatus, derive_task_id
from projectmind.skills import InlineSkillFile, UploadSkillFile


def test_parse_skill_returns_assisted_draft_without_authorizing_tools(
    client: TestClient,
) -> None:
    """Inline Skill source がモデル呼び出しなしで assisted draft へ正規化される。"""

    response = client.post(
        "/api/v1/skills/parse",
        json={
            "files": [
                {
                    "path": "SKILL.md",
                    "content": (
                        "---\n"
                        "name: API Skill\n"
                        "allowed-tools: [Read]\n"
                        "---\n"
                        "# API Skill\n\n"
                        "Follow [rules](references/rules.md).\n"
                    ),
                },
                {"path": "references/rules.md", "content": "# Rules\n\nUse evidence.\n"},
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["normalized_package"]["metadata"]["name"] == "API Skill"
    assert payload["runtime_manifest_draft"]["compatibility"]["level"] == "assisted"
    assert payload["runtime_manifest_draft"]["tools"] == []
    assert payload["runtime_manifest_draft"]["extensions"]["declared_tools"] == ["Read"]


def test_parse_skill_rejects_unsafe_inline_path(client: TestClient) -> None:
    """Browser 由来の path が source root 外へ出る入力を拒否する。"""

    response = client.post(
        "/api/v1/skills/parse",
        json={"files": [{"path": "../SKILL.md", "content": "# Escape\n"}]},
    )

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "invalid_file_path"


def test_save_skill_import_returns_persistent_identity(client: TestClient) -> None:
    """Organization への import が source と interpretation の不変 ID を返す。"""

    fake = FakeSkillService()
    client.app.state.skill_service = fake
    organization_id = client.app.state.auth_service.actor.organization_id
    response = client.post(
        "/api/v1/skill-imports",
        json={"files": [{"path": "SKILL.md", "content": "# API Skill\n"}]},
    )

    assert response.status_code == 201
    assert response.json()["organization_id"] == str(organization_id)
    assert response.json()["interpretation_status"] == "PREVIEW_READY"
    assert response.json()["compatibility_level"] == "assisted"
    assert response.json()["preview"]["normalized_package"]["metadata"]["name"] == "API Skill"
    assert fake.received_files == (InlineSkillFile(path="SKILL.md", content="# API Skill\n"),)


def test_upload_skill_import_accepts_multipart_directory(client: TestClient) -> None:
    """Multipart upload が相対 path 付き binary asset を Organization へ保存する。"""

    fake = FakeSkillService()
    client.app.state.skill_service = fake
    organization_id = client.app.state.auth_service.actor.organization_id
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    response = client.post(
        "/api/v1/skill-imports/upload",
        files=[
            ("files", ("SKILL.md", b"---\nname: API Skill\n---\n# API Skill\n", "text/markdown")),
            ("files", ("assets/logo.png", png, "image/png")),
        ],
    )

    assert response.status_code == 201
    assert response.json()["organization_id"] == str(organization_id)
    # filename が相対 path として渡り、binary asset は原文のまま service へ届く。
    assert fake.received_uploads == (
        UploadSkillFile(
            path="SKILL.md",
            data=b"---\nname: API Skill\n---\n# API Skill\n",
            content_type="text/markdown",
        ),
        UploadSkillFile(path="assets/logo.png", data=png, content_type="image/png"),
    )


def test_upload_skill_import_returns_503_when_storage_unavailable(client: TestClient) -> None:
    """Object storage 未配線なら upload は 503 の安定 Problem を返す。"""

    client.app.state.skill_service = FakeSkillService(storage_unavailable=True)
    response = client.post(
        "/api/v1/skill-imports/upload",
        files=[("files", ("SKILL.md", b"# API Skill\n", "text/markdown"))],
    )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "skill_storage_unavailable"


def test_get_skill_interpretation_returns_saved_preview(client: TestClient) -> None:
    """Interpretation ID から保存済み assisted preview を再取得できる。"""

    fake = FakeSkillService()
    client.app.state.skill_service = fake
    response = client.get(f"/api/v1/skill-interpretations/{fake.stored.interpretation_id}")

    assert response.status_code == 200
    assert response.json()["skill_source_id"] == str(fake.stored.skill_source_id)
    assert response.json()["interpreter_version"] == "deterministic-parser/1.0.0"


def test_get_skill_interpretation_returns_problem_when_missing(
    client: TestClient,
) -> None:
    """未知 interpretation が共通 404 Problem Details を返す。"""

    client.app.state.skill_service = FakeSkillService(missing=True)
    response = client.get(f"/api/v1/skill-interpretations/{uuid4()}")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "skill_interpretation_not_found"


def test_assisted_skill_draft_requires_warning_acceptance(client: TestClient) -> None:
    """Assisted Draft は作成できるが warning 未受理の publish は 409 を返す。"""

    service = FakeSkillService()
    client.app.state.skill_service = service
    draft = client.post(
        f"/api/v1/skill-interpretations/{service.stored.interpretation_id}/draft"
    )

    assert draft.status_code == 201
    payload = draft.json()
    assert payload["status"] == "DRAFT"
    assert payload["gate_passed"] is True
    assert payload["gate_findings"][0]["code"] == "assisted_review_required"

    publish = client.post(
        f"/api/v1/skill-versions/{payload['skill_version_id']}/publish",
        json={"accepted_warnings": []},
    )
    assert publish.status_code == 409
    assert publish.json()["code"] == "skill_publish_gate_failed"


def test_interpret_skill_source_queues_worker_job(client: TestClient) -> None:
    """保存済み source の interpret が Worker job を投入し、queued 受理を返す。"""

    service = FakeSkillService()
    client.app.state.skill_service = service
    pool = FakeArqPool()
    client.app.state.arq_pool = pool
    source_id = service.stored.skill_source_id
    response = client.post(
        f"/api/v1/skill-sources/{source_id}/interpret"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["execution_key"].startswith("sha256:")
    assert payload["execution"] is None
    # Model への egress を持つ Worker が実行するよう、interpret job が投入される。
    assert pool.jobs[0][0] == "interpret_skill_source_job"


def test_interpret_skill_source_returns_stored_when_reused(client: TestClient) -> None:
    """再利用/unsafe 相当は job を投入せず、確定 execution を同封して返す。"""

    service = FakeSkillService(launch_stored=True)
    client.app.state.skill_service = service
    pool = FakeArqPool()
    client.app.state.arq_pool = pool
    source_id = service.stored.skill_source_id
    response = client.post(
        f"/api/v1/skill-sources/{source_id}/interpret"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "stored"
    assert payload["execution"]["status"] == "PREVIEW_READY"
    assert pool.jobs == []


def test_interpret_skill_source_can_explicitly_force_regeneration(client: TestClient) -> None:
    """明示 query だけが既存 identity の再利用を迂回する job を投入する。"""

    service = FakeSkillService(launch_stored=True)
    client.app.state.skill_service = service
    pool = FakeArqPool()
    client.app.state.arq_pool = pool
    source_id = service.stored.skill_source_id

    response = client.post(
        f"/api/v1/skill-sources/{source_id}/interpret"
        "?force_regenerate=true"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["execution_key"] == "sha256:" + ("f" * 64)
    assert pool.jobs[0][1]["force_regenerate"] is True
    assert pool.jobs[0][1]["regeneration_nonce"] == "forced"


def test_interpret_skill_source_returns_404_when_source_missing(client: TestClient) -> None:
    """未登録 source の interpret を安定した 404 へ変換する。"""

    client.app.state.skill_service = FakeSkillService(source_missing=True)
    client.app.state.arq_pool = FakeArqPool()
    response = client.post(
        f"/api/v1/skill-sources/{uuid4()}/interpret"
    )

    assert response.status_code == 404
    assert response.json()["code"] == "skill_source_not_found"


def test_interpret_skill_source_returns_503_when_unconfigured(client: TestClient) -> None:
    """Interpreter 未配線環境では interpret が 503 を返す。"""

    client.app.state.skill_service = FakeSkillService(interpreter_unavailable=True)
    client.app.state.arq_pool = FakeArqPool()
    response = client.post(
        f"/api/v1/skill-sources/{uuid4()}/interpret"
    )

    assert response.status_code == 503
    assert response.json()["code"] == "skill_interpreter_unavailable"


def test_interpret_skill_source_returns_409_when_source_integrity_fails(
    client: TestClient,
) -> None:
    """保存済み source の再構築失敗を generic 500 にせず 409 Problem へ変換する。"""

    service = FakeSkillService(source_integrity_failed=True)
    client.app.state.skill_service = service
    response = client.post(f"/api/v1/skill-sources/{service.stored.skill_source_id}/interpret")

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "skill_source_integrity_failed"


def test_adjust_interpretation_queues_worker_job(client: TestClient) -> None:
    """調整指示が adjust Worker job を投入し、queued 受理を返す。"""

    service = FakeSkillService()
    client.app.state.skill_service = service
    pool = FakeArqPool()
    client.app.state.arq_pool = pool
    parent_id = service.stored.interpretation_id
    response = client.post(
        f"/api/v1/skill-interpretations/{parent_id}/adjust",
        json={"instruction": "Focus on one file."},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "queued"
    assert service.adjustment_instruction == "Focus on one file."
    assert pool.jobs[0][0] == "adjust_skill_interpretation_job"
    # 親と instruction は job kwargs へ引き渡され、Worker が同じ service 経路で再実行する。
    assert pool.jobs[0][1]["interpretation_id"] == str(parent_id)
    assert pool.jobs[0][1]["instruction"] == "Focus on one file."


def test_adjust_interpretation_returns_409_when_parent_not_ready(client: TestClient) -> None:
    """PREVIEW_READY でない親への調整を 409 へ変換する。"""

    client.app.state.skill_service = FakeSkillService(not_ready=True)
    client.app.state.arq_pool = FakeArqPool()
    response = client.post(
        f"/api/v1/skill-interpretations/{uuid4()}/adjust",
        json={"instruction": "x"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "skill_interpretation_not_ready"


def test_interpret_stream_rejects_malformed_execution_key(client: TestClient) -> None:
    """Channel 名へ自由文字列を通さないため、不正な execution key を 422 で弾く。"""

    client.app.state.skill_service = FakeSkillService()
    response = client.get("/api/v1/skill-interpretations/stream/not-a-key")

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_execution_key"


def test_get_interpretation_execution_returns_detail(client: TestClient) -> None:
    """Model interpretation 実行 detail を diff 付きで返す。"""

    service = FakeSkillService()
    client.app.state.skill_service = service
    interpretation_id = service.stored.interpretation_id
    response = client.get(
        f"/api/v1/skill-interpretations/{interpretation_id}/execution"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "PREVIEW_READY"
    assert payload["diff"]["has_changes"] is False


def test_get_interpretation_execution_returns_404_when_missing(client: TestClient) -> None:
    """未登録 interpretation の実行取得を安定した 404 へ変換する。"""

    client.app.state.skill_service = FakeSkillService(missing=True)
    response = client.get(
        f"/api/v1/skill-interpretations/{uuid4()}/execution"
    )

    assert response.status_code == 404
    assert response.json()["code"] == "skill_interpretation_not_found"


def test_list_organization_skill_versions_without_project(client: TestClient) -> None:
    """Project 未選択でも ADMIN が Organization Skill library を取得できる。"""

    client.app.state.skill_service = FakeSkillService()

    response = client.get("/api/v1/skill-versions")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["skill_versions"]) == 1
    assert payload["skill_versions"][0]["organization_id"] == str(
        client.app.state.auth_service.actor.organization_id
    )


def test_deprecate_skill_version_uses_organization_lifecycle(client: TestClient) -> None:
    """Version 廃止は Project path を要求せず DEPRECATED を返す。"""

    client.app.state.skill_service = FakeSkillService()
    skill_version_id = uuid4()

    response = client.post(f"/api/v1/skill-versions/{skill_version_id}/deprecate")

    assert response.status_code == 200
    assert response.json()["skill_version_id"] == str(skill_version_id)
    assert response.json()["status"] == "DEPRECATED"


def test_delete_skill_version_removes_only_unreferenced_versions(client: TestClient) -> None:
    """廃止版の削除は受理し、監査参照が残る版は 409 で拒否する。

    Library は廃止しても行が消えないため版が増え続ける。片付ける経路は必要だが、Run snapshot
    が指す frozen Manifest を消すと過去の実行の根拠が説明できなくなる。両者の境界を固定する。
    """

    skills = FakeSkillService()
    blocked_version_id = uuid4()
    skills.blocked_delete_version_id = blocked_version_id
    client.app.state.skill_service = skills
    deletable_version_id = uuid4()

    deleted = client.delete(f"/api/v1/skill-versions/{deletable_version_id}")
    blocked = client.delete(f"/api/v1/skill-versions/{blocked_version_id}")

    assert deleted.status_code == 204
    assert skills.deleted_version_ids == [deletable_version_id]
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "skill_version_delete_blocked"


def test_skill_version_response_exposes_source_description(client: TestClient) -> None:
    """一覧が skill_key だけでなく SKILL.md 由来の説明文も返す。"""

    client.app.state.skill_service = FakeSkillService()

    response = client.get("/api/v1/skill-versions")

    assert response.status_code == 200
    assert response.json()["skill_versions"][0]["description"] != ""


def test_project_skill_version_enable_list_and_disable(client: TestClient) -> None:
    """ADMIN が Project の精確版を有効化し、一覧確認後に監査行を残して停用できる。"""

    client.app.state.skill_service = FakeSkillService()
    project_id = uuid4()
    skill_version_id = uuid4()

    enabled = client.put(
        f"/api/v1/projects/{project_id}/skill-versions/{skill_version_id}"
    )
    listed = client.get(
        f"/api/v1/projects/{project_id}/skill-versions?include_disabled=true"
    )
    disabled = client.delete(
        f"/api/v1/projects/{project_id}/skill-versions/{skill_version_id}"
    )

    assert enabled.status_code == 200
    assert enabled.json()["project_id"] == str(project_id)
    assert enabled.json()["skill_version"]["skill_version_id"] == str(skill_version_id)
    assert listed.status_code == 200
    assert len(listed.json()["skill_versions"]) == 1
    assert disabled.status_code == 200
    assert disabled.json()["disabled_at"] is not None


def test_list_project_tasks_projects_published_task_catalog(client: TestClient) -> None:
    """Project member に PUBLISHED SkillVersion 由来の task descriptor を返す。"""

    client.app.state.skill_service = FakeSkillService()
    client.app.state.run_service = FakeRunService()
    project_id = uuid4()
    response = client.get(f"/api/v1/projects/{project_id}/tasks")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["tasks"]) == 1
    task = payload["tasks"][0]
    assert task["skill_key"] == "repository-review"
    assert task["task_key"] == "review-change"
    assert task["capability"] == "repository.review"
    assert task["version"] == "1.0.0"
    assert task["input_schema"] == {"type": "object", "additionalProperties": False}
    assert task["input_schema_checksum"] == "sha256:" + ("b" * 64)
    assert task["default_view"] == "repository-review-report"
    # 資源要求は蓝图が唯一の宣言元。公開 response では readiness/blueprint 経由で見る。
    assert "data_source_requirements" not in task
    assert task["tool_requirements"][0]["capability"] == "repository.read/v1"
def test_published_task_exposes_the_deterministic_run_join_key(client: TestClient) -> None:
    """task descriptor が Run 側と同じ task_id を返すことを確認する。

    画面は「この task の上次执行」をこの値で突き合わせる。frontend 側で uuid5 を再実装すると、
    導出が変わったときに実行はできるのに履歴が紐づかない静かな不整合になる。
    """

    client.app.state.skill_service = FakeSkillService()
    client.app.state.run_service = FakeRunService()

    response = client.get(f"/api/v1/projects/{uuid4()}/tasks")

    assert response.status_code == 200
    task = response.json()["tasks"][0]
    assert UUID(task["task_id"]) == derive_task_id(
        skill_version_id=UUID(task["skill_version_id"]), task_key=task["task_key"]
    )
def test_task_catalog_carries_the_last_run_per_task(client: TestClient) -> None:
    """catalog が task ごとの最新 Run を同梱することを確認する。

    画面が Run 履歴の先頭 N 件を引いて突き合わせる形だと、N 件より古い task が「未実行」と
    表示される——欠落ではなく誤った値になるため、合流は server 側で行う。
    """

    run_service = FakeRunService()
    client.app.state.skill_service = FakeSkillService()
    client.app.state.run_service = run_service
    project_id = uuid4()
    # catalog の task と同じ task_id を持つ Run を一件作らせる。
    catalog = client.get(f"/api/v1/projects/{project_id}/tasks").json()["tasks"][0]
    run_service.created_run = CreatedRun(
        run_id=uuid4(),
        project_id=project_id,
        task_id=UUID(catalog["task_id"]),
        status=RunStatus.SUCCEEDED,
        row_version=4,
        created_at=datetime(2026, 7, 25, 9, 0, tzinfo=UTC),
        idempotent_replay=False,
    )

    response = client.get(f"/api/v1/projects/{project_id}/tasks")

    assert response.status_code == 200
    last_run = response.json()["tasks"][0]["last_run"]
    assert last_run["status"] == "SUCCEEDED"
    assert last_run["run_id"] == str(run_service.created_run.run_id)


def test_task_catalog_reports_null_for_a_task_that_never_ran(client: TestClient) -> None:
    """未実行の task は null を返す。「取得できなかった」と区別できる形にする。"""

    client.app.state.skill_service = FakeSkillService()
    client.app.state.run_service = FakeRunService()

    response = client.get(f"/api/v1/projects/{uuid4()}/tasks")

    assert response.status_code == 200
    assert response.json()["tasks"][0]["last_run"] is None
