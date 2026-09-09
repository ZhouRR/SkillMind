"""JSON Schema 自体と代表 example の互換性を検証する。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import FormatChecker
from jsonschema.validators import validator_for

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"

EXAMPLE_CONTRACTS = {
    "examples/agent-task-brief.v1.json": "agent-task-brief/v1.schema.json",
    "examples/capability-blueprint.v1.json": "capability-blueprint/v1.schema.json",
    "examples/create-project-request.v1.json": "projects/v1/create-request.schema.json",
    "examples/document.v1.json": "documents/v1/document.schema.json",
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
    "examples/skill-interpret-delta-event.v1.json": (
        "events/skill-interpret-event/v1.schema.json"
    ),
    "examples/cancel-run-response.v1.json": "runs/cancel/v1/response.schema.json",
    "examples/cancel-requested-run-event.v1.json": "events/run-event/v1.schema.json",
    "examples/change-propose-request.v1.json": (
        "tools/change.propose/v1/request.schema.json"
    ),
    "examples/evidence.v1.json": "evidence/v1.schema.json",
    "examples/generated-task-manifest.v1alpha1.json": "runtime-manifest/v1alpha1.schema.json",
    "examples/generic-native-manifest.v1alpha1.json": "runtime-manifest/v1alpha1.schema.json",
    "examples/issue-read-request.v1.json": "tools/issue.read/v1/request.schema.json",
    "examples/issue-read-response.v1.json": "tools/issue.read/v1/response.schema.json",
    "examples/issue-update-request.v1.json": "tools/issue.update/v1/request.schema.json",
    "examples/issue-update-response.v1.json": "tools/issue.update/v1/response.schema.json",
    "examples/interaction-request.v1.json": (
        "tools/interaction.request/v1/request.schema.json"
    ),
    "examples/interaction-response-request.v1.json": (
        "runs/interaction-response/v1/request.schema.json"
    ),
    "examples/interaction-response.v1.json": (
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
    "examples/create-task-run-documents-set.v1.json": (
        "runs/task-create/v1/request.schema.json"
    ),
    "examples/create-task-run-documents-all.v1.json": (
        "runs/task-create/v1/request.schema.json"
    ),
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
    "examples/skill-capability-catalog.v1.json": "skills/interpreter/v1/capability-catalog.schema.json",
    "examples/skill-interpretation-report.v1.json": "skills/interpreter/v1/interpretation-report.schema.json",
    "examples/skill-interpreter-request.v1.json": "skills/interpreter/v1/request.schema.json",
    "examples/skill-interpreter-response.v1.json": "skills/interpreter/v1/response.schema.json",
    "examples/task-contract-draft.v1.json": "task-contract-draft/v1.schema.json",
    "examples/workspace-read-request.v1.json": "tools/workspace.read/v1/request.schema.json",
    "examples/workspace-read-response.v1.json": "tools/workspace.read/v1/response.schema.json",
    "examples/workspace-search-request.v1.json": "tools/workspace.search/v1/request.schema.json",
    "examples/workspace-search-response.v1.json": "tools/workspace.search/v1/response.schema.json",
    "examples/workspace-write-request.v1.json": "tools/workspace.write/v1/request.schema.json",
    "examples/workspace-write-response.v1.json": "tools/workspace.write/v1/response.schema.json",
}


def load_json(path: Path) -> dict[str, Any]:
    """Contract file を JSON object として安全に読み込む。"""

    with path.open(encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return value


def validate() -> None:
    """全 Schema の妥当性と example の適合性を検証する。"""

    schemas = sorted(CONTRACTS.rglob("*.schema.json"))
    if not schemas:
        raise RuntimeError("No contract schemas found")

    # 先に Schema 自体を検査し、不正 Schema による誤った example 判定を防ぐ。
    for path in schemas:
        schema = load_json(path)
        validator_for(schema).check_schema(schema)

    # 表に無い example は誰にも検証されないまま通るため、登録漏れを fail closed にする。
    on_disk = {f"examples/{path.name}" for path in sorted((CONTRACTS / "examples").glob("*.json"))}
    unregistered = on_disk - set(EXAMPLE_CONTRACTS)
    if unregistered:
        raise RuntimeError(f"Examples missing from EXAMPLE_CONTRACTS: {sorted(unregistered)}")

    format_checker = FormatChecker()
    for example_name, schema_name in EXAMPLE_CONTRACTS.items():
        schema = load_json(CONTRACTS / schema_name)
        example = load_json(CONTRACTS / example_name)
        validator = validator_for(schema)(schema, format_checker=format_checker)
        validator.validate(example)

    print(f"Validated {len(schemas)} schemas and {len(EXAMPLE_CONTRACTS)} examples.")


if __name__ == "__main__":
    validate()
