"""実 parser/adapter/compiler を通す API→Worker 交接と既存 task の無再解釈を確認する。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from skillmind.agent.domain import RunLimits
from skillmind.agent.task_brief import build_agent_task_brief, render_task_brief_prompt
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.core.settings import Settings
from skillmind.skills.direct_candidate import candidate_schema
from skillmind.skills.domain import InlineSkillFile
from skillmind.skills.interpreter import load_capability_catalog
from skillmind.skills.interpreter_execution import compute_execution_key
from skillmind.skills.model_interpreter import ModelCompletion, ModelSkillInterpreter
from skillmind.skills.runtime_profile import InterpreterRuntimeProfile
from skillmind.skills.service import _interpretation_request_input
from skillmind.skills.service_wiring import build_skill_service
from skillmind.skills.task_catalog import resolve_task_run_from_manifest

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
SYSTEM_SKILL = ROOT / "skills/skillmind-skill-interpreter"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["codex", "claude"])
async def test_empty_input_import_handoff_and_source_task_remain_usable(engine: str) -> None:
    """同期した別 instance の identity が一致し、モデル無しの保守側も原 task を読める。"""

    settings = Settings(_env_file=None, contracts_dir=CONTRACTS)
    sessions = MagicMock(side_effect=AssertionError("This regression does not access a database"))
    candidate = json.loads((CONTRACTS / "examples/skill-candidate.v2.json").read_text())
    calls: list[dict[str, Any]] = []

    class Completion:
        """外部モデルだけを合成応答に替え、実 adapter の入出力と compiler を通す。"""

        async def complete(self, **kwargs: Any) -> ModelCompletion:
            """呼出し identity を記録し、原候補の独立コピーを返す。"""

            calls.append(deepcopy(kwargs))
            return ModelCompletion(deepcopy(candidate), None, False)

    def instance():
        """各 process と同様、profile と adapter は独立に構築する。"""

        adapter = ModelSkillInterpreter(
            completion_client=Completion(), system_skill_root=SYSTEM_SKILL,
            response_schema=candidate_schema(CONTRACTS),
            runtime_profile=InterpreterRuntimeProfile(engine, "test-model", "medium", "1", "1"),
        )
        catalog = load_capability_catalog(CONTRACTS / "examples/skill-capability-catalog.v1.json")
        service = build_skill_service(
            settings, session_factory=sessions, file_storage=None, document_library_target=None,
            interpreter_components=(adapter, catalog, adapter.system_identity, "test-model"),
        )
        return service, adapter, catalog

    api, api_adapter, api_catalog = instance()
    worker, worker_adapter, worker_catalog = instance()
    files = (InlineSkillFile(
        path="SKILL.md",
        content="---\nname: handoff-review\ndescription: Explain the supplied instructions.\n---\n"
        "# Handoff review\nNo caller input or external resources are required.\n",
    ),)
    preview = api.preview_inline(files)
    source = SimpleNamespace(
        storage_uri="database://fixture", source_files=files,
        source_hash=preview.normalized_package["source"]["content_hash"],
    )
    payloads, requests = [], []
    for service, adapter, catalog in ((api, api_adapter, api_catalog), (worker, worker_adapter, worker_catalog)):
        package, analysis, request = await service._prepare_request(source, catalog, adapter.system_identity)
        assert request is not None
        requests.append(request)
        payloads.append(_interpretation_request_input(
            prepared=SimpleNamespace(package=package, analysis=analysis, request=request),
            catalog=catalog, identity=adapter.system_identity, model="test-model", parameters={},
            previous=None, adjustment=None, parent_id=None, nonce=None,
            runtime_profile=service._runtime_profile_snapshot(),
        ))
    assert canonical_json(payloads[0]) == canonical_json(payloads[1])
    assert compute_execution_key(requests[0], model="test-model", parameters={}) == (
        compute_execution_key(requests[1], model="test-model", parameters={})
    )
    assert not calls  # import と API 受理用の準備では completion を開始しない。
    response = await worker_adapter.interpret(requests[1], model="test-model", parameters={})
    compiled = worker._runner().run(
        requests[1], response, bind_identity=True,
        require_native_candidate=True, require_direct_candidate=True,
    )
    manifest = compiled["runtime_manifest_draft"]
    original = deepcopy(manifest)
    version_id = uuid4()
    checksum = "sha256:" + sha256_hex(canonical_json(manifest))
    resolved = resolve_task_run_from_manifest(
        skill_id=uuid4(), skill_version_id=version_id, skill_key=manifest["identity"]["skill_key"],
        version="0.1.0", manifest_checksum=checksum, manifest=manifest, task_key="execute",
    )
    assert resolved is not None
    maintenance = build_skill_service(
        settings, session_factory=sessions, file_storage=None, document_library_target=None,
    )
    assert maintenance._interpreter is None
    maintenance.preview_inline(files)
    maintenance._validate_task_input(resolved.input_schema, {})
    brief = build_agent_task_brief(
        run_id=uuid4(), project_id=uuid4(), manifest=manifest, selected_sources={}, tools=[],
        task_snapshot={
            "task_key": "execute", "capability": resolved.capability,
            "skill_version_id": str(version_id), "manifest_checksum": checksum,
            "output_schema_checksum": resolved.output_schema_checksum,
        },
        limits=RunLimits(max_turns=10, wall_timeout_seconds=60, max_output_bytes=100000),
    )
    assert brief.brief["source_documents"] == requests[0]["source"]["source_documents"]
    assert render_task_brief_prompt(brief.brief, input_json={}, output_schema=resolved.output_schema)
    assert manifest == original and len(calls) == 1
    sessions.assert_not_called()
