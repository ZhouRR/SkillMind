"""Repository に保存した Schema、example、OpenAPI の同期を検証する。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import FormatChecker
from jsonschema.validators import validator_for

from skillmind.api.main import create_app
from skillmind.api.problems import PROBLEM_DETAILS_SCHEMA, problem_openapi_response

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"

EXAMPLES = {
    "examples/task-flow-preview.v1.json": "tasks/flow-preview/v1.schema.json",
    "examples/task-flow-preview-not-declared.v1.json": "tasks/flow-preview/v1.schema.json",
    "examples/agent-task-brief.v1.json": "agent-task-brief/v1.schema.json",
    "examples/capability-blueprint.v1.json": "capability-blueprint/v1.schema.json",
    "examples/create-project-request.v1.json": "projects/v1/create-request.schema.json",
    "examples/document.v1.json": "documents/v1/document.schema.json",
    "examples/document-upload-pending.v1.json": "documents/v1/upload.schema.json",
    "examples/document-upload-published.v1.json": "documents/v1/upload.schema.json",
    "examples/document-upload-closure.v1.json": "documents/v1/upload-closure.schema.json",
    "examples/document-upload-closure-request.v1.json": (
        "documents/v1/upload-closure-request.schema.json"
    ),
    "examples/document-read-request.v1.json": "tools/document.read/v1/request.schema.json",
    "examples/document-read-response.v1.json": "tools/document.read/v1/response.schema.json",
    "examples/login-context.v1.json": "auth/v1/login-context.schema.json",
    "examples/login-request.v1.json": "auth/v1/login-request.schema.json",
    "examples/login-rate-limited.v1.json": "errors/problem/v1.schema.json",
    "examples/login-protection-unavailable.v1.json": "errors/problem/v1.schema.json",
    "examples/auth-session.v1.json": "auth/v1/session.schema.json",
    "examples/user-account.v1.json": "users/v1/account.schema.json",
    "examples/user-list.v1.json": "users/v1/list.schema.json",
    "examples/user-security-event.v1.json": "users/v1/security-event.schema.json",
    "examples/user-security-events.v1.json": "users/v1/security-events.schema.json",
    "examples/user-mutation.v1.json": "users/v1/mutation.schema.json",
    "examples/user-create-request.v1.json": "users/v1/create-request.schema.json",
    "examples/user-update-request.v1.json": "users/v1/update-request.schema.json",
    "examples/user-password-request.v1.json": "users/v1/password-request.schema.json",
    "examples/user-version-request.v1.json": "users/v1/version-request.schema.json",
    "examples/agent-result-event.v1.json": "events/run-event/v1.schema.json",
    "examples/skill-interpret-delta-event.v1.json": ("events/skill-interpret-event/v1.schema.json"),
    "examples/cancel-run-response.v1.json": "runs/cancel/v1/response.schema.json",
    "examples/cancel-requested-run-event.v1.json": "events/run-event/v1.schema.json",
    "examples/change-propose-request.v1.json": ("tools/change.propose/v1/request.schema.json"),
    "examples/evidence.v1.json": "evidence/v1.schema.json",
    "examples/generated-task-manifest.v1alpha1.json": ("runtime-manifest/v1alpha1.schema.json"),
    "examples/generic-native-manifest.v1alpha1.json": ("runtime-manifest/v1alpha1.schema.json"),
    "examples/issue-read-request.v1.json": "tools/issue.read/v1/request.schema.json",
    "examples/issue-read-response.v1.json": "tools/issue.read/v1/response.schema.json",
    "examples/issue-update-request.v1.json": "tools/issue.update/v1/request.schema.json",
    "examples/issue-update-response.v1.json": "tools/issue.update/v1/response.schema.json",
    "examples/interaction-request.v1.json": ("tools/interaction.request/v1/request.schema.json"),
    "examples/interaction-response-request.v1.json": (
        "runs/interaction-response/v1/request.schema.json"
    ),
    "examples/interaction-response.v1.json": ("runs/interaction-response/v1/response.schema.json"),
    "examples/interaction-response-replay.v1.json": (
        "runs/interaction-response/v1/response.schema.json"
    ),
    "examples/subagent-dispatch-request.v1.json": (
        "tools/subagent.dispatch/v1/request.schema.json"
    ),
    "examples/subagent-dispatch-response.v1.json": (
        "tools/subagent.dispatch/v1/response.schema.json"
    ),
    "examples/outcome-envelope.v1.json": "outcomes/envelope/v1.schema.json",
    "examples/project.v1.json": "projects/v1/project.schema.json",
    "examples/project-list.v1.json": "projects/v1/list.schema.json",
    "examples/project-member.v1.json": "projects/v1/member.schema.json",
    "examples/project-member-list.v1.json": "projects/v1/member-list.schema.json",
    "examples/project-version-request.v1.json": "projects/v1/version-request.schema.json",
    "examples/update-project-request.v1.json": "projects/v1/update-request.schema.json",
    "examples/redmine-effect-discovery.v1.json": (
        "providers/redmine-effect/v1/discovery.schema.json"
    ),
    "examples/redmine-effect-apply-request.v1.json": (
        "providers/redmine-effect/v1/apply-request.schema.json"
    ),
    "examples/redmine-effect-apply-response.v1.json": (
        "providers/redmine-effect/v1/apply-response.schema.json"
    ),
    "examples/repository-read-request.v1.json": "tools/repository.read/v1/request.schema.json",
    "examples/repository-read-response.v1.json": "tools/repository.read/v1/response.schema.json",
    "examples/repository-write-request.v1.json": "tools/repository.write/v1/request.schema.json",
    "examples/repository-write-response.v1.json": "tools/repository.write/v1/response.schema.json",
    "examples/run-event.v1.json": "events/run-event/v1.schema.json",
    "examples/run-detail.v1.json": "runs/detail/v1.schema.json",
    "examples/run-detail-documents.v1.json": "runs/detail/v1.schema.json",
    "examples/run-history.v1.json": "runs/history/v1.schema.json",
    "examples/text-delta-run-event.v1.json": "events/run-event/v1.schema.json",
    "examples/create-task-run-request.v1.json": "runs/task-create/v1/request.schema.json",
    "examples/create-task-run-documents-single.v1.json": (
        "runs/task-create/v1/request.schema.json"
    ),
    "examples/create-task-run-documents-set.v1.json": ("runs/task-create/v1/request.schema.json"),
    "examples/create-task-run-documents-all.v1.json": ("runs/task-create/v1/request.schema.json"),
    "examples/create-run-response.v1.json": "runs/create/v1/response.schema.json",
    "examples/create-evaluation-request.v1.json": "evaluations/v1/create-request.schema.json",
    "examples/evaluation.v1.json": "evaluations/v1/evaluation.schema.json",
    "examples/evaluation-history.v1.json": "evaluations/v1/history.schema.json",
    "examples/skill-version.v1.json": "skills/version/v1.schema.json",
    "examples/skill-version-list.v1.json": "skills/version/v1-list.schema.json",
    "examples/project-skill-version.v1.json": "skills/project-enablement/v1.schema.json",
    "examples/project-skill-version-list.v1.json": (
        "skills/project-enablement/v1-list.schema.json"
    ),
    "examples/skill-interpreter-diagnostic.v1.json": "skills/interpreter/v1/diagnostic.schema.json",
    "examples/skill-static-analysis.v1.json": "skills/interpreter/v1/static-analysis.schema.json",
    "examples/skill-capability-catalog.v1.json": (
        "skills/interpreter/v1/capability-catalog.schema.json"
    ),
    "examples/skill-interpretation-report.v1.json": (
        "skills/interpreter/v1/interpretation-report.schema.json"
    ),
    "examples/skill-interpreter-request.v1.json": "skills/interpreter/v1/request.schema.json",
    "examples/skill-interpreter-response.v1.json": "skills/interpreter/v1/response.schema.json",
    "examples/task-contract-draft.v1.json": "task-contract-draft/v1.schema.json",
    "examples/task-schedule.v1.json": "task-schedule/v1.schema.json",
    "examples/task-schedule-activity.v1.json": "task-schedule/v1.schema.json",
    "examples/task-schedule-activity-empty.v1.json": "task-schedule/v1.schema.json",
    "examples/task-schedule-activity-legacy.v1.json": "task-schedule/v1.schema.json",
    "examples/task-schedule-page.v1.json": "task-schedule/v1.schema.json",
    "examples/task-schedule-status-request.v1.json": "task-schedule/v1.schema.json",
    "examples/workspace-read-request.v1.json": "tools/workspace.read/v1/request.schema.json",
    "examples/workspace-read-response.v1.json": "tools/workspace.read/v1/response.schema.json",
    "examples/workspace-search-request.v1.json": "tools/workspace.search/v1/request.schema.json",
    "examples/workspace-search-response.v1.json": "tools/workspace.search/v1/response.schema.json",
    "examples/workspace-write-request.v1.json": "tools/workspace.write/v1/request.schema.json",
    "examples/workspace-write-response.v1.json": "tools/workspace.write/v1/response.schema.json",
    "examples/workspace-write-request.v2.json": "tools/workspace.write/v2/request.schema.json",
    "examples/workspace-write-response.v2.json": "tools/workspace.write/v2/response.schema.json",
    "examples/workspace-write-draft-response.v2.json": (
        "tools/workspace.write/v2/response.schema.json"
    ),
    "examples/artifact-list.v1.json": "artifacts/v1/list.schema.json",
    "examples/run-detail-artifacts.v1.json": "runs/detail/v1.schema.json",
    "examples/evaluation-submission-request.v1.json": (
        "evaluations/v1/submission-request.schema.json"
    ),
    "examples/evaluation-submission.v1.json": "evaluations/v1/submission.schema.json",
    "examples/evaluation-page.v1.json": "evaluations/v1/page.schema.json",
}


def load(path: Path) -> dict[str, Any]:
    """Test 対象 JSON を object として読み込む。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_all_schemas_are_valid_draft_2020_12() -> None:
    """全 contract Schema が有効な JSON Schema であることを確認する。"""

    schemas = sorted(CONTRACTS.rglob("*.schema.json"))
    assert schemas
    for path in schemas:
        schema = load(path)
        validator_for(schema).check_schema(schema)


def test_runtime_outcome_schema_matches_public_contract() -> None:
    """Worker が使用する包絡 Schema と公開 contract の本文差分を拒否する。"""

    from skillmind.agent.outcome import OUTCOME_ENVELOPE_SCHEMA

    assert load(CONTRACTS / "outcomes" / "envelope" / "v1.schema.json") == (OUTCOME_ENVELOPE_SCHEMA)


def test_runtime_interaction_schema_matches_public_contract() -> None:
    """Worker が defer 前に使う Interaction Schema と公開 Tool contract を同期する。"""

    from skillmind.runs.interaction import INTERACTION_REQUEST_SCHEMA

    assert load(CONTRACTS / "tools/interaction.request/v1/request.schema.json") == (
        INTERACTION_REQUEST_SCHEMA
    )


def test_runtime_change_proposal_schema_matches_public_contract() -> None:
    """Worker が停止前に使う ChangeProposal Schema と Tool contract を同期する。"""

    from skillmind.effects.proposal import CHANGE_PROPOSE_REQUEST_SCHEMA

    assert load(CONTRACTS / "tools/change.propose/v1/request.schema.json") == (
        CHANGE_PROPOSE_REQUEST_SCHEMA
    )


def test_examples_match_contracts() -> None:
    """代表 example が対応する versioned Schema に適合することを確認する。"""

    for example_name, schema_name in EXAMPLES.items():
        schema = load(CONTRACTS / schema_name)
        validator_for(schema)(schema, format_checker=FormatChecker()).validate(
            json.loads((CONTRACTS / example_name).read_text(encoding="utf-8"))
        )


def test_every_example_on_disk_is_registered() -> None:
    """contracts/examples の全 file と EXAMPLES 表の完全一致を強制する。

    表に無い example は誰にも検証されないまま通るため(AGENTS「同期点」)、
    登録漏れと実体の無い登録の両方向をここで落とす。
    """

    on_disk = {f"examples/{path.name}" for path in sorted((CONTRACTS / "examples").glob("*.json"))}
    assert on_disk == set(EXAMPLES)


def test_exported_openapi_is_current() -> None:
    """保存済み OpenAPI が現在の FastAPI application と一致することを確認する。"""

    exported = load(CONTRACTS / "openapi" / "skillmind-api.v1.json")
    assert exported == create_app().openapi()


def test_problem_openapi_schema_matches_the_public_contract() -> None:
    """配置 path に依存しない frozen Schema と正本の drift を拒否する。"""

    expected = load(CONTRACTS / "errors/problem/v1.schema.json")
    assert expected == PROBLEM_DETAILS_SCHEMA
    first = problem_openapi_response("first")
    second = problem_openapi_response("second")
    first["content"]["application/problem+json"]["schema"]["required"].clear()
    assert second["content"]["application/problem+json"]["schema"] == expected
    assert expected == PROBLEM_DETAILS_SCHEMA


def test_login_openapi_describes_problem_body_and_headers() -> None:
    """JSON の成功応答と Problem の拒否を誤った同一 media type にしない。"""

    paths = create_app().openapi()["paths"]
    expected = load(CONTRACTS / "errors/problem/v1.schema.json")
    for path, method in (("login", "post"), ("login-context", "get")):
        responses = paths[f"/api/v1/auth/{path}"][method]["responses"]
        assert "application/json" in responses["200"]["content"]
        for status in ("429", "503"):
            response = responses[status]
            assert set(response["content"]) == {"application/problem+json"}
            assert response["content"]["application/problem+json"]["schema"] == expected
            assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
            assert response["headers"]["X-Request-ID"]["required"] is True
        assert responses["429"]["headers"]["Retry-After"] == {
            "required": True,
            "schema": {"type": "integer", "minimum": 1, "maximum": 300},
        }
        assert "Retry-After" not in responses["503"]["headers"]


def test_authentication_errors_do_not_advertise_framework_validation_json() -> None:
    """実 auth handler の 401/403/422 が全て既存 Problem に収斂することを宣言する。"""

    paths = create_app().openapi()["paths"]
    expected = load(CONTRACTS / "errors/problem/v1.schema.json")
    for path, method, statuses in (
        ("login", "post", ("401", "403", "422")),
        ("session", "get", ("401",)),
        ("logout", "post", ("401", "403", "422")),
    ):
        for status in statuses:
            response = paths[f"/api/v1/auth/{path}"][method]["responses"][status]
            assert response["content"] == {"application/problem+json": {"schema": expected}}
            assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
