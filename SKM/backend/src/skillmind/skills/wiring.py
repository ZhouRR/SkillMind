"""Interpreter 資産と model transport の組み立てを API/Worker で共有する。"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from skillmind.agent.claude import ClaudeRuntimeConfiguration
from skillmind.agent.interpreter_completion import ClaudeCompletionClient
from skillmind.core.logging import log_event
from skillmind.core.settings import Settings
from skillmind.effects.release import configured_execution_features
from skillmind.skills.interpreter import (
    CapabilityCatalogSnapshot,
    InterpreterSystemSkillIdentity,
    build_interpreter_generation_schema,
    load_capability_catalog,
    load_interpreter_system_skill,
)
from skillmind.skills.interpreter_execution import SkillInterpreter
from skillmind.skills.model_interpreter import ModelSkillInterpreter

logger = logging.getLogger(__name__)


def build_skill_interpreter(
    settings: Settings,
    *,
    environment_fallback: Mapping[str, str | None] | None = None,
) -> tuple[
    SkillInterpreter | None,
    CapabilityCatalogSnapshot | None,
    InterpreterSystemSkillIdentity | None,
    str | None,
]:
    """Interpreter 資産と資格情報が揃う環境でだけ model interpreter を配線する。

    System Skill、capability catalog、response Schema のいずれかが欠ける環境では起動を
    止めず interpret を無効化し、理由を記録する。model は Run と同じ ANTHROPIC 設定に従う。
    """

    contracts_dir = settings.contracts_dir.resolve()
    system_skill_root = contracts_dir.parent / "skills" / "skillmind-skill-interpreter"
    catalog_path = contracts_dir / "examples" / "skill-capability-catalog.v1.json"
    try:
        response_schema = build_interpreter_generation_schema(contracts_dir)
        identity = load_interpreter_system_skill(
            system_skill_root,
            generation_schema=response_schema,
        )
        catalog = load_capability_catalog(catalog_path)
        features = configured_execution_features(settings)
        # 配備で使えない能力を Interpreter に提示せず、絞込後の checksum を凍結する。
        catalog = CapabilityCatalogSnapshot.build(
            catalog_version=catalog.catalog_version,
            capabilities=tuple(
                entry for entry in catalog.capabilities
                if features.capability_enabled(entry.capability)
            ),
        )
        configuration = ClaudeRuntimeConfiguration.from_environ(fallback=environment_fallback)
    except (OSError, ValueError) as error:
        log_event(
            logger,
            logging.WARNING,
            "skill.interpreter.disabled",
            reason=type(error).__name__,
        )
        return None, None, None, None
    interpreter = ModelSkillInterpreter(
        completion_client=ClaudeCompletionClient(configuration),
        system_skill_root=system_skill_root,
        response_schema=response_schema,
        accept_prompt_json=settings.skill_interpreter_accept_prompt_json,
    )
    return interpreter, catalog, identity, configuration.primary_model
