"""隔離した実 Git で、原 commit の只読核対と機密を含まない観測保存を確認する。"""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from skillmind.agent.repository_client import GitCommandRepositoryClient, GitWriteSession
from skillmind.core.hashing import canonical_json
from skillmind.effects.git_receipt import (
    GitCommitCommand,
    GitCommitReader,
    git_effect_commit_message,
)
from skillmind.effects.reconciliation_domain import (
    EffectReconciliationInput,
    EffectReconciliationReference,
    EffectReconciliationTarget,
)
from skillmind.effects.reconciliation_requests import reconciliation_receipt_json
from skillmind.effects.reconciliation_service import EffectReconciliationService
from tests.effects.test_repository_effect import (
    _execution,
    _provider,
    _remote_repository,
    requires_git,
)


@requires_git
@pytest.mark.parametrize("state", ["found", "missing", "conflict"])
async def test_original_commit_lookup_never_calls_push(tmp_path, monkeypatch, state):
    """未観測や他 Effect でも再送せず、正常なら元 checksum に属する回执だけを返す。"""
    uri, base = _remote_repository(tmp_path)
    execution = _execution(
        uri=uri,
        base_revision=base,
        changes=({"path": "/files/src/計画.json", "action": "SET", "value": "{}\n"},),
    )
    if state != "missing":
        await _provider().apply(execution, credential=None)
    if state == "conflict":
        execution = replace(execution, effect_execution_id=uuid4())
    command = GitCommitCommand(
        execution.effect_execution_id,
        execution.project_id, execution.run_id,
        execution.integration_id,
        execution.target["locator"],
        base,
        git_effect_commit_message(
            display=execution.target["display"],
            proposal_ref=execution.proposal_ref,
            effect_id=execution.effect_execution_id,
            fingerprint=execution.request_fingerprint,
        ),
        (("src/計画.json", "{}\n"),),
    )
    target = EffectReconciliationTarget(
        execution.proposal_id,
        execution.binding_id,
        "sha256:" + "a" * 64,
        "git",
        "git-branch-commit/v2",
        command,
        canonical_json(execution.integration_config),
        uuid4(),
    )
    service = EffectReconciliationService(
        MagicMock(),
        secret_resolver=MagicMock(),
        database_reader=AsyncMock(),
        document_reader=None,
        document_library_target=None,
        git_reader=GitCommitReader(GitCommandRepositoryClient(command_timeout_seconds=30)),
    )
    service._load = AsyncMock(return_value=EffectReconciliationInput(target, "fixture:secret"))
    push = AsyncMock(side_effect=AssertionError("Read-only lookup cannot push"))
    monkeypatch.setattr(GitWriteSession, "commit_and_push", push)
    ref = EffectReconciliationReference(
        uuid4(),
        uuid4(),
        uuid4(),
        execution.project_id,
        execution.run_id,
        execution.effect_execution_id,
    )
    result = await service.observe(ref)
    assert result.kind == "GIT_COMMIT"
    assert (
        result.status
        == {"found": "CONFIRMED", "missing": "NOT_OBSERVED", "conflict": "CONFLICT"}[state]
    )
    saved = reconciliation_receipt_json(result, target)
    if state == "found":
        assert saved["effect_id"] == str(execution.effect_execution_id)
        assert saved["request_checksum"] == command.request_checksum
        assert "secret" not in canonical_json(saved)
    else:
        assert saved is None
    push.assert_not_called()
