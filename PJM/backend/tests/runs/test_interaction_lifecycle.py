"""普通答復の原作者、lock 後状態と期限の順序を fake session で検証する。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.agent.domain import AgentEventType
from projectmind.db.models import (
    InteractionResponse,
    OutboxMessage,
    Run,
    RunSegment,
    UserInteraction,
)
from projectmind.runs.domain import (
    InteractionConflictError,
    InteractionExpiredError,
    InteractionNotFoundError,
    InteractionResponseInvalidError,
    RespondedInteraction,
    RunSegmentStatus,
    RunStatus,
    UserInteractionType,
)
from projectmind.runs.interaction import interaction_response_hash, parse_interaction_request
from projectmind.runs.repository import RunRepository
from tests.runs.execution_event_fakes import execution_event
from tests.runs.test_execution_finalization import FinalizationDatabase
from tests.runs.test_run_repository import create_queued_run, create_segment


class InteractionDatabase:
    """ORM の古い読取と lock 後の現在行を区別する局部 fake。実 lock/commit は扱わない。"""

    def __init__(self) -> None:
        """普通 REVIEW の待機状態と、追加行を記録する session を初期化する。"""

        self.run = create_queued_run()
        self.run.status = RunStatus.WAITING_FOR_INPUT.value
        self.segment = create_segment(self.run, status=RunSegmentStatus.WAITING)
        self.actor = uuid4()
        now = datetime.now(UTC)
        self.interaction = UserInteraction(
            id=uuid4(),
            run_id=self.run.id,
            run_segment_id=self.segment.id,
            agent_session_id=uuid4(),
            interaction_type=UserInteractionType.REVIEW.value,
            prompt_json={"prompt": "Review", "allow_multiple": False},
            options_json=[],
            required=False,
            expires_at=now + timedelta(hours=1),
            status="OPEN",
            version=1,
            continuation_mode="RESUME",
            checkpoint_json={"confirmed_facts": []},
            checkpoint_checksum="sha256:" + "a" * 64,
            change_proposal_id=None,
            created_at=now,
            updated_at=now,
        )
        self.rows: list[Any] = []
        self.statements: list[Select[Any]] = []
        self.on_run_lock: Callable[[], None] | None = None
        self.locked_missing = False
        self.observed: SimpleNamespace | None = None
        self.session = MagicMock(spec=AsyncSession)
        self.session.scalars = AsyncMock(side_effect=self.scalars)
        self.session.scalar = AsyncMock(return_value=20)
        self.session.add_all.side_effect = self.rows.extend
        self.repository = RunRepository(self.session)

    async def scalars(self, statement: Select[Any]) -> MagicMock:
        """Query の用途に応じて固定行を返し、populate_existing が無ければ旧 snapshot を返す。"""

        self.statements.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        locked = statement._for_update_arg is not None
        result = MagicMock()
        value: Any = None
        if entity is UserInteraction:
            if not locked:
                self.observed = SimpleNamespace(
                    **{
                        key: deepcopy(value)
                        for key, value in vars(self.interaction).items()
                        if key != "_sa_instance_state"
                    }
                )
                value = self.observed
            elif not self.locked_missing:
                value = (
                    self.interaction
                    if statement.get_execution_options().get("populate_existing")
                    else self.observed
                )
        elif entity is Run:
            if self.on_run_lock is not None:
                callback, self.on_run_lock = self.on_run_lock, None
                callback()
            value = self.run
        elif entity is RunSegment:
            value = (
                self.segment
                if locked
                else next((row for row in self.rows if isinstance(row, RunSegment)), None)
            )
        elif entity is InteractionResponse:
            value = next((row for row in self.rows if isinstance(row, InteractionResponse)), None)
        else:
            raise AssertionError(f"Unexpected entity {entity}")
        result.one_or_none.return_value = value
        result.one.return_value = value
        result.all.return_value = [] if value is None else [value]
        return result

    async def respond(self, **overrides: Any) -> RespondedInteraction:
        """同じ原 request を基準に repository を呼び、差分だけを scenario から上書きする。"""

        arguments = {
            "project_id": self.run.project_id,
            "run_id": self.run.id,
            "interaction_id": self.interaction.id,
            "actor_id": self.actor,
            "interaction_version": 1,
            "response_json": {"text": "  Keep the finding.  "},
            "idempotency_key": "response-original",
            "trace_id": "trace-test",
        }
        arguments.update(overrides)
        return await self.repository.respond_to_interaction(**arguments)

    async def expire(self) -> int:
        """期限走査を局部 fake の現在候補に実行する。"""

        return await self.repository.recover_expired_interactions(now=datetime.now(UTC), limit=20)


@pytest.mark.parametrize("status", list(RunStatus))
async def test_original_response_replay_preserves_hash_rows_and_current_run(
    status: RunStatus,
) -> None:
    """再送は期限・Run 終態後も元 ID を返し、旧 hash と本文や新 Segment を変更しない。"""

    database = InteractionDatabase()
    first = await database.respond()
    response = next(row for row in database.rows if isinstance(row, InteractionResponse))
    original_hash = response.request_hash
    assert original_hash == interaction_response_hash(
        interaction_id=database.interaction.id,
        interaction_version=1,
        response={"text": "  Keep the finding.  "},
    )
    database.run.status = status.value
    database.interaction.expires_at = datetime.now(UTC) - timedelta(hours=1)
    before = tuple(database.rows)
    replay = await database.respond()
    assert replay.idempotent_replay and replay.response_id == first.response_id
    assert replay.run_segment_id == first.run_segment_id and replay.run.status is status
    assert replay.run.row_version == database.run.row_version
    assert response.request_hash == original_hash and response.response_json["text"].startswith(
        "  "
    )
    assert tuple(database.rows) == before
    assert sum(isinstance(row, RunSegment) for row in before) == 1
    assert (
        sum(
            isinstance(row, OutboxMessage) and row.topic == "run.dispatch.requested/v1"
            for row in before
        )
        == 1
    )


@pytest.mark.parametrize(
    "changed", ["actor", "key", "version", "text", "stored-run", "stored-version"]
)
async def test_replay_rejects_different_actor_or_original_identity(changed: str) -> None:
    """同じ payload だけでは他者の答復を重放せず、衝突から新たな回答を作らない。"""

    database = InteractionDatabase()
    await database.respond()
    response = next(row for row in database.rows if isinstance(row, InteractionResponse))
    overrides: dict[str, Any] = {}
    if changed == "actor":
        overrides["actor_id"] = uuid4()
    elif changed == "key":
        overrides["idempotency_key"] = "different-key"
    elif changed == "version":
        overrides["interaction_version"] = 2
    elif changed == "text":
        overrides["response_json"] = {"text": "Changed"}
    elif changed == "stored-run":
        response.run_id = uuid4()
    else:
        response.interaction_version = 2
    before = tuple(database.rows)
    with pytest.raises(InteractionConflictError):
        await database.respond(**overrides)
    assert tuple(database.rows) == before


async def test_lock_after_other_response_refreshes_stale_open_snapshot() -> None:
    """lock 待機前の OPEN を再使用すると二つ目の回答が作られる境界を固定する。"""

    database = InteractionDatabase()

    def other_response_won() -> None:
        """別回答が先行した現在状態を lock の取得時点で返す。"""

        database.interaction.status = "RESPONDED"
        database.interaction.version = 2

    database.on_run_lock = other_response_won
    with pytest.raises(InteractionConflictError, match="no longer open"):
        await database.respond()
    assert database.rows == []
    lock_statements = [item for item in database.statements if item._for_update_arg is not None]
    assert [item.column_descriptions[0]["entity"] for item in lock_statements] == [
        Run,
        RunSegment,
        UserInteraction,
    ]
    final = lock_statements[-1]
    assert final.get_execution_options()["populate_existing"] is True
    sql = str(final)
    assert "user_interactions.run_id =" in sql and "user_interactions.run_segment_id =" in sql


async def test_legacy_duplicate_options_reject_first_answer_without_changing_rows() -> None:
    """旧曖昧な質問を破壊せず、新しい選択の意味を一意に確定できない回答だけを拒否する。"""

    database = InteractionDatabase()
    database.interaction.interaction_type = "CHOICE"
    database.interaction.options_json = [
        {"key": "same", "label": "First"}, {"key": "same", "label": "Second"},
    ]
    before = (database.run.status, database.run.row_version, database.interaction.version)
    with pytest.raises(InteractionResponseInvalidError, match="unique"):
        await database.respond(response_json={"selected_option_keys": ["same"]})
    assert (database.run.status, database.run.row_version, database.interaction.version) == before
    assert database.interaction.status == "OPEN" and database.rows == []


async def test_legacy_duplicate_options_preserve_original_response_replay() -> None:
    """旧質問の option key 制約で、既存の原作者・本文・hash の確認を後から拒否しない。"""

    database = InteractionDatabase()
    database.interaction.interaction_type = "CHOICE"
    database.interaction.options_json = [{"key": "same", "label": "First"}]
    answer = {"selected_option_keys": ["same"]}
    first = await database.respond(response_json=answer)
    database.interaction.options_json.append({"key": "same", "label": "Second"})
    response = next(row for row in database.rows if isinstance(row, InteractionResponse))
    original_hash = response.request_hash
    before = list(database.rows)
    replay = await database.respond(response_json=answer)
    assert replay.idempotent_replay and replay.response_id == first.response_id
    assert replay.run_segment_id == first.run_segment_id and database.rows == before
    assert response.request_hash == original_hash and response.response_json == answer


async def test_changed_locked_interaction_ownership_is_not_found() -> None:
    """Run/Segment に帰属する lock 対象が失われた場合は別行へ切り替えない。"""

    database = InteractionDatabase()
    database.locked_missing = True
    with pytest.raises(InteractionNotFoundError):
        await database.respond()
    assert database.rows == []


async def test_response_uses_expiry_after_waiting_for_run_lock() -> None:
    """観測時には有効な期限でも、lock 取得後の期限切れは回答ではなく timeout にする。"""

    database = InteractionDatabase()

    def expire_during_wait() -> None:
        """待機中に進んだ期限を現在行へ反映する。"""

        database.interaction.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    database.on_run_lock = expire_during_wait
    with pytest.raises(InteractionExpiredError):
        await database.respond()
    assert database.interaction.status == "EXPIRED"
    assert not any(isinstance(row, InteractionResponse) for row in database.rows)
    assert sum(isinstance(row, RunSegment) for row in database.rows) == 1


async def test_recovery_scan_time_does_not_replace_post_lock_clock() -> None:
    """候補走査に未来時刻が渡っても、現在の期限前に回答待ちを閉じない。"""

    database = InteractionDatabase()
    recovered = await database.repository.recover_expired_interactions(
        now=datetime.now(UTC) + timedelta(days=1), limit=20
    )
    assert recovered == 0 and database.interaction.status == "OPEN"
    assert database.rows == []


@pytest.mark.parametrize("expired", [False, True])
async def test_nonwaiting_original_segment_cannot_start_another_continuation(expired: bool) -> None:
    """Run の待機名だけでは完了済みの元 Segment を継続しない。"""

    database = InteractionDatabase()
    database.segment.status = RunSegmentStatus.COMPLETED.value
    if expired:
        database.interaction.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(InteractionExpiredError if expired else InteractionConflictError):
        await database.respond()
    assert database.rows == [] and database.run.status == RunStatus.WAITING_FOR_INPUT.value
    assert database.interaction.status == ("EXPIRED" if expired else "OPEN")


@pytest.mark.parametrize("response_first", [True, False])
async def test_response_and_expiry_orders_append_only_one_continuation(
    response_first: bool,
) -> None:
    """先後順を模擬し、通常答復と timeout が二つの Segment/dispatch を生まない。"""

    database = InteractionDatabase()
    if response_first:
        await database.respond()
        database.interaction.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await database.expire() == 0
    else:
        database.interaction.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await database.expire() == 1
        with pytest.raises(InteractionConflictError):
            await database.respond()
    assert sum(isinstance(row, RunSegment) for row in database.rows) == 1
    assert (
        sum(
            isinstance(row, OutboxMessage) and row.topic == "run.dispatch.requested/v1"
            for row in database.rows
        )
        == 1
    )
    assert sum(isinstance(row, InteractionResponse) for row in database.rows) == int(response_first)


@pytest.mark.parametrize(
    "status", [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.QUEUED]
)
@pytest.mark.parametrize("via_response", [False, True])
async def test_expired_old_interaction_never_reopens_finished_or_continued_run(
    status: RunStatus,
    via_response: bool,
) -> None:
    """API 発見と recovery の双方で、旧交互だけを閉じて既存 Run/Segment/event を保つ。"""

    database = InteractionDatabase()
    database.run.status = status.value
    database.interaction.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    before = (database.run.status, database.run.row_version, database.segment.status)
    if via_response:
        with pytest.raises(InteractionExpiredError):
            await database.respond()
    else:
        assert await database.expire() == 1
    assert database.interaction.status == "EXPIRED"
    assert (database.run.status, database.run.row_version, database.segment.status) == before
    assert database.rows == []


@pytest.mark.parametrize("expired", [False, True])
async def test_legacy_approval_is_read_only_for_response_and_recovery(expired: bool) -> None:
    """Proposal の無い旧承認待ちにも一般回答や期限回復で決定を補造しない。"""

    database = InteractionDatabase()
    database.interaction.interaction_type = "EFFECT_APPROVAL"
    database.run.status = RunStatus.WAITING_FOR_APPROVAL.value
    if expired:
        database.interaction.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(InteractionResponseInvalidError, match="approval endpoint"):
        await database.respond()
    assert await database.expire() == 0
    assert database.interaction.status == "OPEN" and database.rows == []
    candidates = database.statements[-4]
    assert "user_interactions.interaction_type IN" in str(candidates)


async def test_parser_bypass_cannot_create_ordinary_approval_wait() -> None:
    """直接構築された Draft も新規 Session/checkpoint/lease 変更より前に拒否する。"""

    database = FinalizationDatabase(cancelled=False)
    event = execution_event(database.claimed, AgentEventType.INTERACTION_REQUESTED)
    draft = replace(
        parse_interaction_request(event.payload["interaction_request"]),
        interaction_type=UserInteractionType.EFFECT_APPROVAL,
    )
    with pytest.raises(InteractionResponseInvalidError, match="approval endpoint"):
        await RunRepository(database.session).suspend_for_interaction(
            database.claimed,
            event=event,
            session_metadata=database.metadata,
            request=draft,
        )
    assert database.run.status == RunStatus.RUNNING.value
    assert database.attempt.lease_token_hash is not None
    database.session.add_all.assert_not_called()


async def test_parser_bypass_cannot_suspend_ambiguous_choice_options() -> None:
    """同じ key の直接 Draft も lease/Session/監査の変更前に拒否する。"""

    database = FinalizationDatabase(cancelled=False)
    event = execution_event(database.claimed, AgentEventType.INTERACTION_REQUESTED)
    draft = replace(
        parse_interaction_request(event.payload["interaction_request"]),
        interaction_type=UserInteractionType.CHOICE,
        options=({"key": "same", "label": "First"}, {"key": "same", "label": "Second"}),
    )
    before = (database.run.status, database.attempt.lease_token_hash, database.attempt.status)
    with pytest.raises(InteractionResponseInvalidError, match="unique"):
        await RunRepository(database.session).suspend_for_interaction(
            database.claimed, event=event, session_metadata=database.metadata, request=draft,
        )
    assert (
        database.run.status, database.attempt.lease_token_hash, database.attempt.status
    ) == before
    database.session.add_all.assert_not_called()


@pytest.mark.parametrize("version", [True, False, 1.0, "1", 0, -1, None])
async def test_internal_response_requires_strict_original_version(version: object) -> None:
    """内部呼出しも bool 等を版 1 と比較せず、読取前に拒否する。"""

    database = InteractionDatabase()
    with pytest.raises(InteractionResponseInvalidError, match="positive integer"):
        await database.respond(interaction_version=version)
    assert database.statements == [] and database.rows == []
