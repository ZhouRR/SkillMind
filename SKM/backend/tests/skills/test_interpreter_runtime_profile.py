"""実効設定・prompt・操作 catalog の実行 identity と無効 parameter 拒否を検証する。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

import pytest

from skillmind.skills.interpreter_execution import compute_execution_key
from skillmind.skills.runtime_profile import (
    InterpreterRuntimeProfile,
    bind_runtime_identity,
    validate_interpreter_parameters,
)

if TYPE_CHECKING:
    from skillmind.skills.interpreter import InterpreterSystemSkillIdentity


@dataclass(frozen=True)
class IdentityFixture:
    """hash 合成の単体検証用 dataclass。SDK/DB を起動する identity loader は使用しない。"""

    source_hash: str = "sha256:" + "a" * 64
    prompt_checksum: str = "sha256:" + "b" * 64


def profile() -> InterpreterRuntimeProfile:
    """特定実モデルの存在を仮定しない設定 fixture を返す。"""

    return InterpreterRuntimeProfile(
        engine="codex", model="test-model", reasoning_effort="medium",
        sdk_version="1.0.0", cli_version="1.0.0",
    )


def identity_checksum(
    value: InterpreterRuntimeProfile, *, prompt: str = "source rules",
    operations: dict[str, Any] | None = None, fallback: bool = False,
) -> str:
    """本番 helper を通して、実行 key に入る prompt identity を計算する。"""

    original = cast("InterpreterSystemSkillIdentity", IdentityFixture())
    snapshot = value.snapshot(
        write_operations=operations or {"database.write/v1": ["INSERT"]},
        accept_prompt_json=fallback,
    )
    bound = bind_runtime_identity(original, profile=snapshot, system_prompt=prompt)
    assert original.prompt_checksum == "sha256:" + "b" * 64
    assert bound.source_hash == original.source_hash
    return bound.prompt_checksum


def test_same_effective_configuration_has_same_identity() -> None:
    """同一構成は安定し、metadata の変更で原 source を改変しない。"""

    assert identity_checksum(profile()) == identity_checksum(profile())


@pytest.mark.parametrize("change", [
    {"reasoning_effort": "high"}, {"model": "another-model"},
    {"sdk_version": "1.0.1"}, {"cli_version": "1.0.1"},
    {"configuration_checksum": "sha256:" + "c" * 64}, {"engine": "claude"},
])
def test_effective_configuration_changes_execution_identity(change: dict[str, Any]) -> None:
    """実際の設定差が frozen request と execution key に伝わる。"""

    old = identity_checksum(profile())
    new = identity_checksum(replace(profile(), **change))
    assert old != new
    assert compute_execution_key({"prompt_checksum": old}, model="test-model", parameters={}) != (
        compute_execution_key({"prompt_checksum": new}, model="test-model", parameters={})
    )


@pytest.mark.parametrize("change", [
    {"prompt": "updated source rules"}, {"fallback": True},
    {"operations": {"database.write/v1": ["INSERT", "UPDATE"]}},
])
def test_prompt_operations_and_fallback_are_part_of_identity(change: dict[str, Any]) -> None:
    """同じモデルでも生成依存が変わった場合は原結果を誤って再利用しない。"""

    assert identity_checksum(profile()) != identity_checksum(profile(), **change)


def test_snapshot_does_not_hold_mutable_caller_objects() -> None:
    """返した dict の変更を次の設定 snapshot へ逆流させない。"""

    configured = profile()
    snapshot = configured.snapshot(write_operations={}, accept_prompt_json=False)
    snapshot["model"] = "changed"
    assert configured.snapshot(write_operations={}, accept_prompt_json=False)["model"] == "test-model"


@pytest.mark.parametrize("value", [{"temperature": 0}, {"seed": 1}, {"private-key": "private-value"}, []])
def test_unused_parameters_are_rejected_without_echo(value: Any) -> None:
    """使わない parameter を捨てず、提供値をエラーへ含めずに拒否する。"""

    with pytest.raises(ValueError) as caught:
        validate_interpreter_parameters(value)
    assert "private" not in str(caught.value)


def test_absent_or_empty_parameters_are_supported() -> None:
    """既存の無指定呼出しは維持する。"""

    validate_interpreter_parameters(None)
    validate_interpreter_parameters({})


@pytest.mark.parametrize("change", [{"engine": "unknown"}, {"model": "bad\nvalue"},
                                   {"sdk_version": ""}, {"configuration_checksum": 2},
                                   {"configuration_checksum": "not-a-checksum"}])
def test_runtime_profile_rejects_malformed_public_metadata(change: dict[str, Any]) -> None:
    """設定値を診断へ複写せず、固定形式以外を受理しない。"""

    with pytest.raises(ValueError):
        replace(profile(), **change)
