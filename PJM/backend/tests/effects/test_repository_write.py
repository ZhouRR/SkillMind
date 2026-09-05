"""repository.write/v1 提案の検証境界と apply 可否判定を確認する (計画 §20 R1)。

ここで閉じられなかった提案は承認画面に並び、承認されれば実際に repository へ落ちる。したがって
「承認すれば通るものだけが並ぶ」状態を保つのがこの層の責務であり、scope・branch・内容種別・
規模のいずれも提案段階で判定する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from projectmind.effects.catalog import EFFECT_CAPABILITIES, resolve_effect_capability
from projectmind.effects.domain import (
    ChangeProposalDraft,
    ChangeProposalValidationError,
    CreatePreauthorizationCommand,
    EffectRiskLevel,
)
from projectmind.effects.policy_repository import EffectPolicyRepository
from projectmind.effects.repository_write import (
    REPOSITORY_WRITE_CAPABILITY,
    repository_write_scope_from_payload,
    validate_repository_write_proposal,
)
from projectmind.integrations.domain import IntegrationStatus, IntegrationValidationError
from projectmind.integrations.repository import IntegrationRepository

_SCOPE = {"paths": ["src", "docs/spec.md"], "revisions": ["main"]}
_BASE = "9f2c1d4b8a6e5307c1b2d3e4f5a6b7c8d9e0f1a2"
# 既定 mode は direct (計画 §20 D6)。予約 namespace の規則を検証する test は branch を明示する。
_BRANCH_CONFIG = {
    "repository_uri": "https://git.example.test/p.git",
    "default_revision": "main",
    "write_mode": "branch",
}


def _draft(**overrides: Any) -> ChangeProposalDraft:
    """既定で有効な repository.write 提案 draft を作る。"""

    fields: dict[str, Any] = {
        "effect_intent_key": "apply-fix",
        "resource_key": "source_repository",
        "capability_version": REPOSITORY_WRITE_CAPABILITY,
        "operation": "commit",
        "target": {
            "locator": "projectmind/run-7a1c/fix-null-guard",
            "display": "projectmind/run-7a1c/fix-null-guard",
        },
        "changes": (
            {
                "path": "/files/src/handler.py",
                "action": "SET",
                "value": "def handle():\n    return 1\n",
            },
        ),
        "precondition": {"revision": _BASE},
        "summary": "Guard against a null payload",
        "evidence_refs": ("ev_source_1",),
        "risk_level": EffectRiskLevel.MEDIUM,
        "reversible": True,
        "rollback": {"description": "Delete the branch"},
        "verification": {"method": "READ_BACK", "paths": ["/files/src/handler.py"]},
        "continuation_mode": "RESUME",
        "checkpoint": {},
        "checkpoint_checksum": "sha256:" + "0" * 64,
        "idempotency_key": "cp-7a1c9d2e-repository",
        "request_fingerprint": "1" * 64,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    fields.update(overrides)
    return ChangeProposalDraft(**fields)


def _validate(draft: ChangeProposalDraft, **overrides: Any) -> dict[str, Any]:
    """branch mode を既定にした検証呼び出し (規則の焦点を mode からずらさない)。"""

    return validate_repository_write_proposal(
        draft,
        binding_scope=overrides.pop("binding_scope", _SCOPE),
        integration_config=overrides.pop("integration_config", _BRANCH_CONFIG),
    )


def test_valid_proposal_normalizes_into_a_provider_payload() -> None:
    """有効な提案は branch・base revision・逐 file 内容へ正規化される。"""

    payload = _validate(_draft())

    assert payload["target_branch"] == "projectmind/run-7a1c/fix-null-guard"
    assert payload["base_revision"] == _BASE
    assert payload["commit_message"] == "Guard against a null payload"
    assert payload["files"] == {"src/handler.py": "def handle():\n    return 1\n"}


def test_remove_is_accepted_without_content() -> None:
    """REMOVE は内容を持たない。持てば SET と区別できず replay 判定が壊れる。"""

    payload = _validate(
        _draft(
            changes=(
                {"path": "/files/src/legacy.py", "action": "REMOVE", "value": None},
            ),
            verification={"method": "READ_BACK", "paths": ["/files/src/legacy.py"]},
        )
    )

    assert payload["files"] == {"src/legacy.py": None}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"target": {"locator": "main", "display": "main"}},
            "projectmind/ namespace",
        ),
        (
            {"target": {"locator": "release/v1", "display": "release/v1"}},
            "projectmind/ namespace",
        ),
        (
            {
                "changes": (
                    {"path": "/files/etc/passwd", "action": "SET", "value": "x"},
                )
            },
            "frozen binding scope",
        ),
        (
            {
                "changes": (
                    {"path": "/files/src/../../etc/passwd", "action": "SET", "value": "x"},
                )
            },
            "path is invalid",
        ),
        (
            {
                "changes": (
                    {"path": "/files/src/app.py", "action": "APPEND", "value": "x"},
                )
            },
            "idempotent SET or REMOVE",
        ),
        (
            {
                "changes": (
                    {"path": "/files/src/app.py", "action": "SET", "value": 42},
                )
            },
            "UTF-8 text content",
        ),
        (
            {
                "changes": (
                    {"path": "/files/src/big.py", "action": "SET", "value": "x" * 1_048_577},
                )
            },
            "per-file limit",
        ),
        ({"precondition": {"revision": ""}}, "frozen base revision"),
    ],
)
def test_unsafe_or_out_of_scope_proposals_are_refused(
    overrides: dict[str, Any], message: str
) -> None:
    """既定 branch、scope 外 path、非冪等 action、binary/過大内容は提案段階で閉じる。"""

    with pytest.raises(ChangeProposalValidationError, match=message):
        _validate(_draft(**overrides))


def test_change_set_size_limit_is_enforced() -> None:
    """変更 file 数の上限を超える提案は拒否する (人手 review が成り立つ範囲に保つ)。"""

    changes = tuple(
        {"path": f"/files/src/module_{index}.py", "action": "SET", "value": "x"}
        for index in range(51)
    )

    with pytest.raises(ChangeProposalValidationError, match="size limit"):
        _validate(_draft(changes=changes))


def test_binding_without_paths_cannot_be_written() -> None:
    """path scope を持たない binding では、どの変更も許可しない。"""

    with pytest.raises(ChangeProposalValidationError, match="frozen binding scope"):
        _validate(_draft(), binding_scope={"paths": [], "revisions": []})


def test_scope_projection_lists_changed_paths() -> None:
    """監査用 scope 投影は変更 path をそのまま安定順で返す。"""

    assert repository_write_scope_from_payload(
        {"files": {"src/b.py": "x", "src/a.py": None}}
    ) == {"paths": ["src/a.py", "src/b.py"]}


def test_repository_write_is_never_preauthorizable() -> None:
    """代码変更は常に人手承認を要する。事前許可の対象に入らない (§20.2)。"""

    definition = resolve_effect_capability(REPOSITORY_WRITE_CAPABILITY)

    assert definition.preauthorizable is False
    assert definition.providers == frozenset({"git", "svn"})
    assert EFFECT_CAPABILITIES["issue.update/v1"].preauthorizable is True


def test_unregistered_capability_is_refused_before_apply() -> None:
    """Apply chain へ入れるのは登録済み capability だけ。"""

    with pytest.raises(ChangeProposalValidationError, match="not enabled for apply"):
        resolve_effect_capability("repository.delete/v1")


@pytest.mark.asyncio
async def test_preauthorization_policy_refuses_repository_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事前許可 policy を repository.write に対して作れない (計画 §20 R3)。

    「常に人手承認」を提案側だけで守っても、policy を作れてしまえば次の提案が自動承認される。
    policy 作成の段階で閉じるのが唯一の実効的な位置。
    """

    integration = SimpleNamespace(
        integration_id=uuid4(),
        status=IntegrationStatus.ACTIVE,
        provider="git",
        capabilities=("repository.read/v1", "repository.write/v1"),
        scope={"paths": ["src"], "revisions": ["main"]},
    )

    async def _get_integration(
        self: IntegrationRepository, *, project_id: object, integration_id: object
    ) -> object:
        """固定 Integration を返す repository patch。"""

        del self, project_id, integration_id
        return integration

    monkeypatch.setattr(IntegrationRepository, "get_integration", _get_integration)
    repository = EffectPolicyRepository(SimpleNamespace())  # type: ignore[arg-type]

    with pytest.raises(IntegrationValidationError, match="not eligible for preauthorization"):
        await repository.create(
            CreatePreauthorizationCommand(
                project_id=uuid4(),
                integration_id=integration.integration_id,
                capability_version=REPOSITORY_WRITE_CAPABILITY,
                operation="commit",
                scope={"paths": ["src"], "revisions": ["main"]},
                created_by=uuid4(),
                expires_at=None,
            )
        )


def test_direct_mode_only_accepts_the_default_branch() -> None:
    """direct (既定 mode) では既定 branch のみを提案でき、他 branch は提案段階で閉じる。"""

    config = {**_BRANCH_CONFIG, "write_mode": "direct"}

    payload = _validate(
        _draft(target={"locator": "main", "display": "main"}), integration_config=config
    )
    assert payload["target_branch"] == "main"

    with pytest.raises(ChangeProposalValidationError, match="default branch"):
        _validate(
            _draft(target={"locator": "release", "display": "release"}),
            integration_config=config,
        )


def test_branch_mode_still_requires_the_reserved_namespace() -> None:
    """既定 (branch) では従来どおり予約 namespace が必須。"""

    with pytest.raises(ChangeProposalValidationError, match="projectmind/ namespace"):
        _validate(_draft(target={"locator": "main", "display": "main"}))


def test_write_prefix_narrows_within_the_reserved_namespace() -> None:
    """Integration の収窄 prefix の外は提案段階で拒否する。"""

    config = {**_BRANCH_CONFIG, "write_branch_prefix": "projectmind/jaf/"}

    with pytest.raises(ChangeProposalValidationError, match="Integration write prefix"):
        _validate(_draft(), integration_config=config)

    payload = _validate(
        _draft(target={"locator": "projectmind/jaf/fix", "display": "projectmind/jaf/fix"}),
        integration_config=config,
    )
    assert payload["target_branch"] == "projectmind/jaf/fix"
