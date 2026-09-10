"""Redmine controlled-effect Provider の CAS、replay、read-back 境界を検証する。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from skillmind.effects.domain import ClaimedEffectExecution
from skillmind.effects.redmine import (
    EffectProviderStaleError,
    EffectProviderTransportError,
    EffectProviderVerificationError,
    RedmineIssueSnapshot,
    RedmineIssueUpdateProvider,
    UrllibRedmineTransport,
)


def _execution() -> ClaimedEffectExecution:
    """status field 一件を更新する lease 取得済み EffectExecution を返す。"""

    return ClaimedEffectExecution(
        effect_execution_id=uuid4(),
        proposal_id=uuid4(),
        proposal_ref="cp_test001",
        approval_id=uuid4(),
        run_id=uuid4(),
        run_segment_id=uuid4(),
        run_attempt_id=uuid4(),
        agent_session_id=uuid4(),
        project_id=uuid4(),
        integration_id=uuid4(),
        binding_id=uuid4(),
        capability_version="issue.update/v1",
        operation="set status",
        target={"locator": "42", "display": "Issue 42"},
        changes=({"path": "/fields/status_id", "action": "SET", "value": 3},),
        precondition={"revision": "rev-1"},
        verification={"method": "READ_BACK", "paths": ["/fields/status_id"]},
        idempotency_key="effect:test:0001",
        request_fingerprint="f" * 64,
        provider="redmine",
        integration_revision=1,
        integration_scope={"issue_ids": ["42"], "field_keys": ["status_id"]},
        integration_config={"base_url": "https://redmine.example.test"},
        secret_reference_id=uuid4(),
        lease_token="lease-token",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        attempt_no=1,
    )


class MemoryRedmineTransport:
    """呼び出し順と snapshot を固定する in-memory Redmine adapter。"""

    def __init__(self, snapshots: list[RedmineIssueSnapshot]) -> None:
        """read ごとに返す snapshot を保持する。"""

        self.snapshots = list(snapshots)
        self.calls: list[str] = []
        self.protocol_error: EffectProviderTransportError | None = None
        self.update_replayed = False

    async def ensure_issue_update_protocol(self, *, base_url: str, api_key: str) -> None:
        """Adapter discovery を記録し、指定時だけ fail closed にする。"""

        assert base_url.startswith("https://")
        assert api_key == "deployment-secret"
        self.calls.append("discover")
        if self.protocol_error is not None:
            raise self.protocol_error

    async def read_issue(
        self,
        *,
        base_url: str,
        api_key: str,
        issue_id: str,
        field_keys: tuple[str, ...],
    ) -> RedmineIssueSnapshot:
        """指定順の snapshot を返す。"""

        assert base_url.startswith("https://")
        assert api_key == "deployment-secret"
        assert issue_id == "42"
        assert field_keys == ("status_id",)
        self.calls.append("read")
        return self.snapshots.pop(0)

    async def update_issue(
        self,
        *,
        base_url: str,
        api_key: str,
        issue_id: str,
        expected_revision: str,
        fields: Mapping[str, Any],
        idempotency_key: str,
    ) -> bool:
        """CAS input を照合し、adapter の replay 結果を返す。"""

        assert base_url.startswith("https://")
        assert api_key == "deployment-secret"
        assert issue_id == "42"
        assert expected_revision == "rev-1"
        assert fields == {"status_id": 3}
        assert idempotency_key == "effect:test:0001"
        self.calls.append("update")
        return self.update_replayed


@pytest.mark.asyncio
async def test_effect_protocol_is_required_before_any_issue_access() -> None:
    """CAS adapter discovery が失敗した場合は read/write のどちらも開始しない。"""

    transport = MemoryRedmineTransport([])
    transport.protocol_error = EffectProviderTransportError(
        "provider_effect_protocol_unavailable", retryable=False
    )

    with pytest.raises(EffectProviderTransportError) as captured:
        await RedmineIssueUpdateProvider(transport).apply(
            _execution(), credential="deployment-secret"
        )

    assert captured.value.code == "provider_effect_protocol_unavailable"
    assert captured.value.retryable is False
    assert transport.calls == ["discover"]


@pytest.mark.asyncio
async def test_matching_desired_state_is_an_idempotent_replay() -> None:
    """Revision が進んでいても desired state が一致すれば write を再送しない。"""

    transport = MemoryRedmineTransport(
        [RedmineIssueSnapshot("42", "rev-2", {"status_id": 3})]
    )

    result = await RedmineIssueUpdateProvider(transport).apply(
        _execution(), credential="deployment-secret"
    )

    assert result.replayed is True
    assert result.verification["replayed"] is True
    assert transport.calls == ["discover", "read"]


@pytest.mark.asyncio
async def test_revision_change_with_another_state_is_stale() -> None:
    """Pre-read revision が変化し desired state と異なる場合は CAS 前に stale とする。"""

    transport = MemoryRedmineTransport(
        [RedmineIssueSnapshot("42", "rev-2", {"status_id": 2})]
    )

    with pytest.raises(EffectProviderStaleError):
        await RedmineIssueUpdateProvider(transport).apply(
            _execution(), credential="deployment-secret"
        )

    assert transport.calls == ["discover", "read"]


@pytest.mark.asyncio
async def test_apply_uses_adapter_then_verifies_with_read_back() -> None:
    """一致する revision から CAS apply を行い、その後の read-back 一致だけを成功とする。"""

    transport = MemoryRedmineTransport(
        [
            RedmineIssueSnapshot("42", "rev-1", {"status_id": 1}),
            RedmineIssueSnapshot("42", "rev-2", {"status_id": 3}),
        ]
    )

    result = await RedmineIssueUpdateProvider(transport).apply(
        _execution(), credential="deployment-secret"
    )

    assert result.replayed is False
    assert result.before.content["revision"] == "rev-1"
    assert result.after.content["revision"] == "rev-2"
    assert transport.calls == ["discover", "read", "update", "read"]


@pytest.mark.asyncio
async def test_read_back_mismatch_is_not_reported_as_applied() -> None:
    """Provider 応答が成功でも read-back が不一致なら verification failure とする。"""

    transport = MemoryRedmineTransport(
        [
            RedmineIssueSnapshot("42", "rev-1", {"status_id": 1}),
            RedmineIssueSnapshot("42", "rev-2", {"status_id": 2}),
        ]
    )

    with pytest.raises(EffectProviderVerificationError):
        await RedmineIssueUpdateProvider(transport).apply(
            _execution(), credential="deployment-secret"
        )

    assert transport.calls == ["discover", "read", "update", "read"]


@pytest.mark.asyncio
async def test_http_transport_uses_versioned_adapter_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP transport が stock issue PUT ではなく discovery 後の versioned endpoint を使う。"""

    transport = UrllibRedmineTransport()
    requests: list[dict[str, Any]] = []

    def request_json(**kwargs: Any) -> dict[str, Any]:
        """Network を行わず request 引数に応じた contract response を返す。"""

        requests.append(dict(kwargs))
        if str(kwargs["url"]).endswith("skillmind-effect-provider.json"):
            return {
                "protocol": "skillmind.redmine-effect/v1",
                "provider": "redmine",
                "capabilities": ["issue.update/v1"],
                "atomic_precondition": "revision",
                "idempotency": "key",
            }
        return {"outcome": "APPLIED"}

    monkeypatch.setattr(transport, "_request_json", request_json)
    await transport.ensure_issue_update_protocol(
        base_url="https://redmine.example.test", api_key="deployment-secret"
    )
    replayed = await transport.update_issue(
        base_url="https://redmine.example.test",
        api_key="deployment-secret",
        issue_id="42",
        expected_revision="rev-1",
        fields={"status_id": 3},
        idempotency_key="effect:test:0001",
    )

    assert replayed is False
    assert requests[0]["method"] == "GET"
    assert requests[1]["url"].endswith(
        "/skillmind/effects/issue.update/v1/issues/42.json"
    )
    assert requests[1]["body"] == {
        "capability_version": "issue.update/v1",
        "expected_revision": "rev-1",
        "idempotency_key": "effect:test:0001",
        "issue": {"status_id": 3},
    }
