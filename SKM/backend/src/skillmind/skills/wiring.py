"""Interpreter 資産と model transport の組み立てを API/Worker で共有する。"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from skillmind.agent.claude import (
    CLAUDE_AGENT_SDK_VERSION,
    CLAUDE_CODE_CLI_VERSION,
    ClaudeRuntimeConfiguration,
)
from skillmind.agent.codex_completion import CodexCompletionClient
from skillmind.agent.codex_runtime import (
    CODEX_CLI_VERSION,
    CODEX_SDK_VERSION,
    CodexRuntimeConfiguration,
)
from skillmind.agent.interpreter_completion import ClaudeCompletionClient
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.core.logging import log_event
from skillmind.core.settings import Settings
from skillmind.effects.release import configured_execution_features
from skillmind.skills.interpreter import (
    CapabilityCatalogSnapshot,
    InterpreterSystemSkillIdentity,
    build_interpreter_generation_schema,
    load_capability_catalog,
)
from skillmind.skills.interpreter_execution import SkillInterpreter
from skillmind.skills.model_interpreter import ModelCompletionClient, ModelSkillInterpreter

from skillmind.skills.runtime_profile import InterpreterRuntimeProfile

logger = logging.getLogger(__name__)

# Credential は hash 対象にも含めない。接続先は平文で保存せず routing 設定の差だけを識別する。
_CLAUDE_PROFILE_KEYS = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
)


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
    止めず interpret を無効化し、理由を記録する。SDK/model は Run と同じ明示設定に従う。
    """

    contracts_dir = settings.contracts_dir.resolve()
    system_skill_root = contracts_dir.parent / "skills" / "skillmind-skill-interpreter"
    catalog_path = contracts_dir / "examples" / "skill-capability-catalog.v1.json"
    try:
        response_schema = build_interpreter_generation_schema(contracts_dir)
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
        completion: ModelCompletionClient
        model: str | None
        if settings.agent_sdk == "codex":
            codex_configuration = CodexRuntimeConfiguration(
                settings.codex_model, settings.codex_reasoning_effort, settings.codex_home,
            )
            completion = CodexCompletionClient(codex_configuration)
            model = codex_configuration.primary_model
            profile = InterpreterRuntimeProfile(
                engine="codex", model=model, reasoning_effort=codex_configuration.effort,
                sdk_version=CODEX_SDK_VERSION, cli_version=CODEX_CLI_VERSION,
            )
        else:
            configuration = ClaudeRuntimeConfiguration.from_environ(fallback=environment_fallback)
            completion = ClaudeCompletionClient(configuration)
            model = configuration.primary_model
            profile = InterpreterRuntimeProfile(
                engine="claude", model=model,
                reasoning_effort=configuration.environment.get("CLAUDE_CODE_EFFORT_LEVEL"),
                sdk_version=CLAUDE_AGENT_SDK_VERSION, cli_version=CLAUDE_CODE_CLI_VERSION,
                configuration_checksum="sha256:" + sha256_hex(canonical_json({
                    key: configuration.environment[key] for key in _CLAUDE_PROFILE_KEYS
                    if key in configuration.environment
                })),
            )
        interpreter = ModelSkillInterpreter(
            completion_client=completion,
            system_skill_root=system_skill_root,
            response_schema=response_schema,
            accept_prompt_json=settings.skill_interpreter_accept_prompt_json,
            runtime_profile=profile,
        )
        # Request producer と実 model adapter が、同じ実効設定付き identity を共有する。
        identity = interpreter.system_identity
    except (OSError, ValueError) as error:
        log_event(
            logger,
            logging.WARNING,
            "skill.interpreter.disabled",
            reason=type(error).__name__,
        )
        return None, None, None, None
    return interpreter, catalog, identity, model
