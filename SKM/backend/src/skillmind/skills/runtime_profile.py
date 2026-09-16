"""Interpreter の実効設定と生成依存を、資格情報なしで identity に束縛する。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from skillmind.core.hashing import canonical_json, sha256_hex

if TYPE_CHECKING:
    from skillmind.skills.interpreter import InterpreterSystemSkillIdentity

# 候補投影・編訳の意味を変える場合は更新する。保存済み候補や Run は再計算しない。
INTERPRETER_PIPELINE_VERSION = "skillmind.interpreter-pipeline/v2.1"


@dataclass(frozen=True, slots=True)
class InterpreterRuntimeProfile:
    """配備で実際に使う SDK/model 設定。endpoint、path、credential は保持しない。"""

    engine: Literal["codex", "claude"]
    model: str | None
    reasoning_effort: str | None
    sdk_version: str
    cli_version: str
    configuration_checksum: str | None = None

    def __post_init__(self) -> None:
        """固定 field のみを公開可能な設定 snapshot として受理する。"""

        if self.engine not in {"codex", "claude"}:
            raise ValueError("Unsupported Interpreter engine")
        for value in (self.model, self.reasoning_effort, self.sdk_version, self.cli_version):
            if value is not None and (
                not isinstance(value, str) or not value or not value.isprintable()
                or len(value) > 256
            ):
                raise ValueError("Invalid Interpreter runtime profile")
        if not self.sdk_version or not self.cli_version:
            raise ValueError("Interpreter build identity is required")
        if self.configuration_checksum is not None and (
            not isinstance(self.configuration_checksum, str)
            or re.fullmatch(r"sha256:[a-f0-9]{64}", self.configuration_checksum) is None
        ):
            raise ValueError("Invalid Interpreter configuration checksum")

    def snapshot(
        self, *, write_operations: Mapping[str, Any], accept_prompt_json: bool
    ) -> dict[str, Any]:
        """生成に影響する操作 catalog と fallback も含め、不変比較用の値を返す。"""

        return {
            "profile_version": "skillmind.interpreter-runtime-profile/v1",
            "pipeline_version": INTERPRETER_PIPELINE_VERSION,
            **asdict(self),
            "write_operations_checksum": "sha256:" + sha256_hex(canonical_json(write_operations)),
            "accept_prompt_json": accept_prompt_json,
        }


def bind_runtime_identity(
    identity: InterpreterSystemSkillIdentity,
    *,
    profile: Mapping[str, Any],
    system_prompt: str,
) -> InterpreterSystemSkillIdentity:
    """原 source/Schema identity に実 prompt と実効設定を追加し、元 identity は変更しない。"""

    checksum = "sha256:" + sha256_hex(canonical_json({
        "base_prompt_checksum": identity.prompt_checksum,
        "system_prompt_checksum": "sha256:" + sha256_hex(system_prompt),
        "runtime_profile": dict(profile),
    }))
    return replace(identity, prompt_checksum=checksum)


def validate_interpreter_parameters(parameters: Mapping[str, Any] | None) -> None:
    """両 SDK が使わない per-call parameter を、値や key を表示せず明示拒否する。"""

    if parameters is not None and (not isinstance(parameters, Mapping) or parameters):
        raise ValueError("Per-call Skill interpreter parameters are not supported")
