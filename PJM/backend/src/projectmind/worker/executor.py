"""Worker と AgentEngine の実行監督、heartbeat、終態処理を定義する。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol

from projectmind.agent.claude import CLAUDE_AGENT_SDK_VERSION, CLAUDE_CODE_CLI_VERSION
from projectmind.agent.domain import (
    AgentEngine,
    AgentEvent,
    AgentEventType,
    AgentSessionRef,
    ForkContext,
    ResumeContext,
    RunContext,
)
from projectmind.agent.result_validation import ResultValidationError, ResultValidator
from projectmind.core.hashing import canonical_json, sha256_hex
from projectmind.core.logging import log_event
from projectmind.effects.proposal import parse_change_proposal_request
from projectmind.runs.domain import (
    AgentSessionMetadata,
    ClaimedRun,
    LeaseValidationError,
    RunAttemptStatus,
    RunResultRecord,
    RunStatus,
    SessionContinuationMode,
)
from projectmind.runs.interaction import parse_interaction_request
from projectmind.runs.realtime import RunRealtimePublisher
from projectmind.runs.service import RunService

logger = logging.getLogger(__name__)


class RunExecutor(Protocol):
    """Lease 取得済み Run snapshot を実行する Worker-side port。"""

    async def execute(self, claimed_run: ClaimedRun) -> None:
        """Run を実行し、状態・Event・Result を永続化する。"""

        ...


class RunContextBuilder(Protocol):
    """ClaimedRun から security boundary 済み RunContext を構築する port。"""

    async def build(self, claimed_run: ClaimedRun, *, sequence_start: int) -> RunContext:
        """Workspace、User、Tool、Schema を解決した immutable context を返す。"""

        ...


class AgentRunExecutor:
    """Lease を維持しながら AgentEvent を永続化し、安全な終態へ閉じる。"""

    def __init__(
        self,
        *,
        run_service: RunService,
        context_builder: RunContextBuilder,
        engine: AgentEngine,
        result_validator: ResultValidator,
        lease_seconds: int,
        heartbeat_interval_seconds: float | None = None,
        realtime_publisher: RunRealtimePublisher | None = None,
    ) -> None:
        """実行依存と heartbeat 間隔を固定する。"""

        interval = heartbeat_interval_seconds or lease_seconds / 3
        if lease_seconds <= 0 or interval <= 0 or interval >= lease_seconds:
            raise ValueError("Heartbeat interval must be positive and shorter than the lease")
        self._run_service = run_service
        self._context_builder = context_builder
        self._engine = engine
        self._result_validator = result_validator
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_seconds = interval
        self._realtime_publisher = realtime_publisher

    async def execute(self, claimed_run: ClaimedRun) -> None:
        """PREPARING から heartbeat 付き Agent 実行を開始し、必ず終態化する。"""

        prepared = await self._run_service.prepare_execution(claimed_run)
        log_event(
            logger,
            logging.INFO,
            "run.execution.prepared",
            run_id=claimed_run.run_id,
            run_attempt_id=claimed_run.run_attempt_id,
            attempt_no=claimed_run.attempt_no,
            status=RunStatus.RUNNING.value,
        )
        if await self._run_service.is_cancellation_requested(claimed_run.run_id):
            await self._finalize_cancelled(claimed_run)
            return
        try:
            context = await self._context_builder.build(
                claimed_run,
                sequence_start=prepared.next_sequence,
            )
            _validate_context_identity(context, claimed_run, prepared.next_sequence)
            await self._run_service.freeze_agent_task_brief(
                claimed_run,
                brief=dict(context.task_brief),
                checksum=context.task_brief_checksum,
            )
        except Exception as builder_error:
            if await self._run_service.is_cancellation_requested(claimed_run.run_id):
                await self._finalize_cancelled(claimed_run)
                return
            await self._finalize_without_event(
                claimed_run,
                code="context_build_failed",
                error_type=type(builder_error).__name__,
            )
            return

        # Agent へ渡した指示の監査点。Brief 正文は Skill guidance と業務入力を含むため、
        # docs/11 §7.1 のとおり log には identity と checksum だけを残す。
        log_event(
            logger,
            logging.INFO,
            "run.execution.brief",
            run_id=claimed_run.run_id,
            run_attempt_id=claimed_run.run_attempt_id,
            task_brief_checksum=context.task_brief_checksum,
            execution_profile=_brief_execution_profile(context),
        )

        if await self._run_service.is_cancellation_requested(claimed_run.run_id):
            await self._finalize_cancelled(claimed_run)
            return

        done = asyncio.Event()
        session_ref: asyncio.Future[AgentSessionRef] = asyncio.get_running_loop().create_future()
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._consume_engine(claimed_run, context, done, session_ref))
                tasks.create_task(self._heartbeat(claimed_run, done))
                tasks.create_task(self._monitor_cancellation(claimed_run, done, session_ref))
        except ExceptionGroup as group:
            supervisor_error = _first_exception(group)
            if isinstance(supervisor_error, LeaseValidationError):
                raise supervisor_error from group
            if await self._run_service.is_cancellation_requested(claimed_run.run_id):
                await self._finalize_cancelled(claimed_run)
                return
            await self._finalize_without_event(
                claimed_run,
                code="execution_supervisor_failed",
                error_type=type(supervisor_error).__name__,
            )

    async def _consume_engine(
        self,
        claimed: ClaimedRun,
        context: RunContext,
        done: asyncio.Event,
        session_ref: asyncio.Future[AgentSessionRef],
    ) -> None:
        """Engine stream を一度だけ消費し、最初の terminal event で Run を閉じる。"""

        metadata = AgentSessionMetadata(
            cwd=str(context.workspace.cwd),
            engine="claude-agent-sdk",
            sdk_version=CLAUDE_AGENT_SDK_VERSION,
            cli_version=CLAUDE_CODE_CLI_VERSION,
            model=context.model,
            parent_session_id=claimed.parent_agent_session_id,
            continuation_mode=claimed.continuation_mode,
            checkpoint_checksum=claimed.checkpoint_checksum,
            engine_options_checksum=_engine_options_checksum(context),
        )
        last_sequence = context.sequence_start - 1
        last_session_id: str | None = None
        usage: dict[str, Any] = {}
        cost: dict[str, Any] = {}
        stream = aiter(self._execution_stream(claimed, context))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + context.limits.wall_timeout_seconds
        try:
            while True:
                # Wall timeout は event 待機だけに適用し、終態化 transaction を中断させない。
                remaining = deadline - loop.time()
                try:
                    if remaining <= 0:
                        raise TimeoutError
                    event = await asyncio.wait_for(anext(stream), timeout=remaining)
                except StopAsyncIteration:
                    if await self._run_service.is_cancellation_requested(claimed.run_id):
                        await self._finalize_cancelled(
                            claimed,
                            context=context,
                            metadata=metadata,
                            sequence=last_sequence + 1,
                            session_id=last_session_id,
                        )
                        return
                    break
                except TimeoutError:
                    await _close_stream(stream)
                    if await self._run_service.is_cancellation_requested(claimed.run_id):
                        await self._finalize_cancelled(
                            claimed,
                            context=context,
                            metadata=metadata,
                            sequence=last_sequence + 1,
                            session_id=last_session_id,
                        )
                        return
                    await self._finalize_stream_failure(
                        claimed,
                        context,
                        metadata=metadata,
                        sequence=last_sequence + 1,
                        session_id=last_session_id,
                        code="wall_timeout",
                    )
                    return
                if event.sequence <= last_sequence:
                    raise ValueError("Agent event sequence is not strictly increasing")
                event = _lineage_event(event, claimed.continuation_mode)
                last_sequence = event.sequence
                last_session_id = event.agent_session_id
                if not session_ref.done():
                    session_ref.set_result(
                        AgentSessionRef(
                            run_id=claimed.run_id,
                            run_attempt_id=claimed.run_attempt_id,
                            session_id=event.agent_session_id,
                        )
                    )
                    log_event(
                        logger,
                        logging.INFO,
                        "agent.session.started",
                        run_id=claimed.run_id,
                        run_attempt_id=claimed.run_attempt_id,
                        agent_session_id=event.agent_session_id,
                        attempt_no=claimed.attempt_no,
                    )
                _merge_usage(event, usage, cost)
                terminal_event = (
                    event.event_type is AgentEventType.RESULT_COMPLETED
                    or _terminal_mapping(event.event_type) is not None
                )
                cancellation_wins = (
                    terminal_event
                    and event.event_type is not AgentEventType.SESSION_INTERRUPTED
                    and await self._run_service.is_cancellation_requested(claimed.run_id)
                )
                if cancellation_wins:
                    await self._finalize_cancelled(
                        claimed,
                        context=context,
                        metadata=metadata,
                        sequence=event.sequence,
                        session_id=event.agent_session_id,
                    )
                    return
                if event.event_type is AgentEventType.TEXT_DELTA:
                    # 監査 DB を肥大化させず、切断時に失ってよい一時通知として配送する。
                    if self._realtime_publisher is not None:
                        await self._realtime_publisher.publish(event)
                    continue
                if event.event_type is AgentEventType.RESULT_COMPLETED:
                    await self._complete_result(
                        claimed,
                        context,
                        event,
                        metadata=metadata,
                        usage=usage,
                        cost=cost,
                    )
                    return
                if event.event_type is AgentEventType.INTERACTION_REQUESTED:
                    request_payload = event.payload.get("interaction_request")
                    if not isinstance(request_payload, Mapping):
                        await self._finalize_stream_failure(
                            claimed,
                            context,
                            metadata=metadata,
                            sequence=event.sequence,
                            session_id=event.agent_session_id,
                            code="interaction_request_invalid",
                        )
                        return
                    try:
                        interaction = parse_interaction_request(
                            request_payload,
                            now=event.occurred_at,
                        )
                    except ValueError:
                        await self._finalize_stream_failure(
                            claimed,
                            context,
                            metadata=metadata,
                            sequence=event.sequence,
                            session_id=event.agent_session_id,
                            code="interaction_request_invalid",
                        )
                        return
                    interaction_id = await self._run_service.suspend_for_interaction(
                        claimed,
                        event=event,
                        session_metadata=metadata,
                        interaction=interaction,
                    )
                    log_event(
                        logger,
                        logging.INFO,
                        "run.interaction.requested",
                        run_id=claimed.run_id,
                        run_attempt_id=claimed.run_attempt_id,
                        agent_session_id=event.agent_session_id,
                        interaction_id=interaction_id,
                        segment_no=claimed.segment_no,
                        status=(
                            RunStatus.WAITING_FOR_APPROVAL.value
                            if interaction.interaction_type.value == "EFFECT_APPROVAL"
                            else RunStatus.WAITING_FOR_INPUT.value
                        ),
                    )
                    return
                if event.event_type is AgentEventType.CHANGE_PROPOSED:
                    request_payload = event.payload.get("change_proposal_request")
                    if not isinstance(request_payload, Mapping):
                        await self._finalize_stream_failure(
                            claimed,
                            context,
                            metadata=metadata,
                            sequence=event.sequence,
                            session_id=event.agent_session_id,
                            code="change_proposal_invalid",
                        )
                        return
                    try:
                        proposal = parse_change_proposal_request(
                            request_payload,
                            now=event.occurred_at,
                        )
                    except ValueError:
                        await self._finalize_stream_failure(
                            claimed,
                            context,
                            metadata=metadata,
                            sequence=event.sequence,
                            session_id=event.agent_session_id,
                            code="change_proposal_invalid",
                        )
                        return
                    try:
                        proposal_id = await self._run_service.suspend_for_proposal(
                            claimed,
                            event=event,
                            session_metadata=metadata,
                            proposal=proposal,
                        )
                    except ValueError:
                        await self._finalize_stream_failure(
                            claimed,
                            context,
                            metadata=metadata,
                            sequence=event.sequence,
                            session_id=event.agent_session_id,
                            code="change_proposal_rejected",
                        )
                        return
                    log_event(
                        logger,
                        logging.INFO,
                        "run.change_proposed",
                        run_id=claimed.run_id,
                        run_attempt_id=claimed.run_attempt_id,
                        agent_session_id=event.agent_session_id,
                        proposal_id=proposal_id,
                        segment_no=claimed.segment_no,
                        status=RunStatus.WAITING_FOR_APPROVAL.value,
                    )
                    return
                terminal = _terminal_mapping(event.event_type)
                if terminal is not None:
                    target, attempt_status, code = terminal
                    await self._run_service.finalize_execution(
                        claimed,
                        target=target,
                        attempt_status=attempt_status,
                        event=event,
                        session_metadata=metadata,
                        result=None,
                        error_json=None
                        if target in {RunStatus.CANCELLED, RunStatus.WAITING_PERMISSION}
                        else {"code": code, "retryable": False},
                    )
                    log_event(
                        logger,
                        logging.INFO,
                        "run.execution.terminal",
                        run_id=claimed.run_id,
                        run_attempt_id=claimed.run_attempt_id,
                        agent_session_id=event.agent_session_id,
                        attempt_no=claimed.attempt_no,
                        status=target.value,
                        error_code=code if target is RunStatus.FAILED else None,
                    )
                    return
                await self._run_service.append_agent_event(
                    claimed,
                    event,
                    session_metadata=metadata,
                )

            await self._finalize_stream_failure(
                claimed,
                context,
                metadata=metadata,
                sequence=last_sequence + 1,
                session_id=last_session_id,
                code="terminal_event_missing",
            )
        finally:
            done.set()

    def _execution_stream(
        self, claimed: ClaimedRun, context: RunContext
    ) -> AsyncIterator[AgentEvent]:
        """Segment の continuation mode に対応する AgentEngine operation を選ぶ。"""

        if claimed.continuation_mode in {
            SessionContinuationMode.INITIAL,
            SessionContinuationMode.REPLACE,
        }:
            return self._engine.execute(context)
        if (
            claimed.parent_sdk_session_id is None
            or claimed.parent_run_attempt_id is None
        ):
            raise ValueError("Session continuation requires an audited parent session")
        parent = AgentSessionRef(
            run_id=claimed.run_id,
            run_attempt_id=claimed.parent_run_attempt_id,
            session_id=str(claimed.parent_sdk_session_id),
        )
        if claimed.continuation_mode is SessionContinuationMode.RESUME:
            return self._engine.resume(
                ResumeContext(run=context, session=parent, input_text=context.prompt)
            )
        if claimed.continuation_mode is SessionContinuationMode.FORK:
            return self._engine.fork(
                ForkContext(run=context, parent_session=parent, input_text=context.prompt)
            )
        raise ValueError(f"Unknown session continuation: {claimed.continuation_mode}")

    async def _complete_result(
        self,
        claimed: ClaimedRun,
        context: RunContext,
        event: AgentEvent,
        *,
        metadata: AgentSessionMetadata,
        usage: dict[str, Any],
        cost: dict[str, Any],
    ) -> None:
        """Structured output を再検証し、成功または検証失敗を原子的に確定する。"""

        schema_ref = context.task_snapshot.get("output_schema_checksum")
        if not isinstance(schema_ref, str):
            schema_ref = context.task_snapshot.get("output_schema")
        if not isinstance(schema_ref, str):
            schema_ref = "inline://run-result-schema"
        try:
            task_schema = context.task_snapshot.get("task_output_schema_json")
            task_schema_mapping = task_schema if isinstance(task_schema, dict) else None
            task_schema_ref = context.task_snapshot.get("task_output_schema_checksum")
            validated = await self._result_validator.validate(
                run_id=context.run_id,
                schema=context.result_schema,
                schema_ref=schema_ref,
                structured_output=event.payload.get("structured_output"),
                result_kind=(
                    str(context.task_snapshot.get("result_kind"))
                    if context.task_snapshot.get("result_kind") == "OUTCOME_ENVELOPE"
                    else "STRUCTURED_OUTPUT"
                ),
                task_schema=task_schema_mapping,
                task_schema_ref=task_schema_ref if isinstance(task_schema_ref, str) else None,
            )
        except ResultValidationError as error:
            failure = AgentEvent(
                run_id=event.run_id,
                run_attempt_id=event.run_attempt_id,
                agent_session_id=event.agent_session_id,
                sequence=event.sequence,
                occurred_at=datetime.now(UTC),
                event_type=AgentEventType.ENGINE_FAILED,
                payload={"reason": "result_validation_failed", "code": error.code},
            )
            await self._run_service.finalize_execution(
                claimed,
                target=RunStatus.FAILED,
                attempt_status=RunAttemptStatus.FAILED,
                event=failure,
                session_metadata=metadata,
                result=None,
                error_json={"code": error.code, "retryable": False},
            )
            log_event(
                logger,
                logging.WARNING,
                "run.result.rejected",
                run_id=claimed.run_id,
                run_attempt_id=claimed.run_attempt_id,
                agent_session_id=event.agent_session_id,
                attempt_no=claimed.attempt_no,
                status=RunStatus.FAILED.value,
                error_code=error.code,
            )
            return

        record = RunResultRecord(
            output_schema=schema_ref,
            result_kind=validated.result_kind,
            data=validated.data,
            evidence_refs=tuple(sorted(validated.evidence_refs)),
            artifact_refs=tuple(sorted(validated.artifact_refs)),
            change_proposal_refs=tuple(sorted(validated.change_proposal_refs)),
            optional_schema_identity=(
                {"schema_ref": validated.validation["task_schema_ref"]}
                if isinstance(validated.validation.get("task_schema_ref"), str)
                else {}
            ),
            summary=validated.summary,
            confidence=validated.confidence,
            needs_review=validated.needs_review,
            usage=dict(usage),
            cost=dict(cost),
            validation=validated.validation,
        )
        await self._run_service.finalize_execution(
            claimed,
            target=RunStatus.SUCCEEDED,
            attempt_status=RunAttemptStatus.SUCCEEDED,
            event=event,
            session_metadata=metadata,
            result=record,
            error_json=None,
        )
        log_event(
            logger,
            logging.INFO,
            "run.result.persisted",
            run_id=claimed.run_id,
            run_attempt_id=claimed.run_attempt_id,
            agent_session_id=event.agent_session_id,
            attempt_no=claimed.attempt_no,
            status=RunStatus.SUCCEEDED.value,
        )

    async def _finalize_stream_failure(
        self,
        claimed: ClaimedRun,
        context: RunContext,
        *,
        metadata: AgentSessionMetadata,
        sequence: int,
        session_id: str | None,
        code: str,
    ) -> None:
        """Terminal event の欠落や wall timeout を FAILED へ閉じ、可能なら Agent event を残す。"""

        event = None
        if session_id is not None:
            event = AgentEvent(
                run_id=context.run_id,
                run_attempt_id=context.run_attempt_id,
                agent_session_id=session_id,
                sequence=sequence,
                occurred_at=datetime.now(UTC),
                event_type=AgentEventType.ENGINE_FAILED,
                payload={"reason": code},
            )
        await self._run_service.finalize_execution(
            claimed,
            target=RunStatus.FAILED,
            attempt_status=RunAttemptStatus.FAILED,
            event=event,
            session_metadata=metadata if event is not None else None,
            result=None,
            error_json={"code": code, "retryable": False},
        )
        log_event(
            logger,
            logging.ERROR,
            "run.execution.stream_failed",
            run_id=claimed.run_id,
            run_attempt_id=claimed.run_attempt_id,
            agent_session_id=session_id,
            attempt_no=claimed.attempt_no,
            status=RunStatus.FAILED.value,
            error_code=code,
        )

    async def _finalize_without_event(
        self, claimed: ClaimedRun, *, code: str, error_type: str
    ) -> None:
        """Session 作成前の infrastructure failure を秘密なしで終態化する。"""

        await self._run_service.finalize_execution(
            claimed,
            target=RunStatus.FAILED,
            attempt_status=RunAttemptStatus.FAILED,
            event=None,
            session_metadata=None,
            result=None,
            error_json={"code": code, "error_type": error_type, "retryable": False},
        )
        log_event(
            logger,
            logging.ERROR,
            "run.execution.setup_failed",
            run_id=claimed.run_id,
            run_attempt_id=claimed.run_attempt_id,
            attempt_no=claimed.attempt_no,
            status=RunStatus.FAILED.value,
            error_code=code,
        )

    async def _finalize_cancelled(
        self,
        claimed: ClaimedRun,
        *,
        context: RunContext | None = None,
        metadata: AgentSessionMetadata | None = None,
        sequence: int | None = None,
        session_id: str | None = None,
    ) -> None:
        """取消 intent を terminal snapshot が最後になる CANCELLED transaction へ閉じる。"""

        event = None
        if context is not None and sequence is not None and session_id is not None:
            event = AgentEvent(
                run_id=context.run_id,
                run_attempt_id=context.run_attempt_id,
                agent_session_id=session_id,
                sequence=sequence,
                occurred_at=datetime.now(UTC),
                event_type=AgentEventType.SESSION_INTERRUPTED,
                payload={"reason": "user_interrupted"},
            )
        await self._run_service.finalize_execution(
            claimed,
            target=RunStatus.CANCELLED,
            attempt_status=RunAttemptStatus.CANCELLED,
            event=event,
            session_metadata=metadata if event is not None else None,
            result=None,
            error_json=None,
        )
        log_event(
            logger,
            logging.INFO,
            "run.execution.cancelled",
            run_id=claimed.run_id,
            run_attempt_id=claimed.run_attempt_id,
            agent_session_id=session_id,
            attempt_no=claimed.attempt_no,
            status=RunStatus.CANCELLED.value,
        )

    async def _monitor_cancellation(
        self,
        claimed: ClaimedRun,
        done: asyncio.Event,
        session_ref: asyncio.Future[AgentSessionRef],
    ) -> None:
        """Durable cancel intent を監視し、session 確立後に Engine interrupt を一度だけ呼ぶ。"""

        while not done.is_set():
            if await self._run_service.is_cancellation_requested(claimed.run_id):
                while not session_ref.done() and not done.is_set():
                    try:
                        await asyncio.wait_for(done.wait(), timeout=0.1)
                    except TimeoutError:
                        continue
                if session_ref.done() and not done.is_set():
                    # Adapter は timeout 時にも disconnect するため、
                    # 監視側の例外より stream 側の排空と終態化を優先する。
                    with suppress(Exception):
                        await self._engine.interrupt(session_ref.result())
                return
            try:
                await asyncio.wait_for(done.wait(), timeout=0.5)
            except TimeoutError:
                continue

    async def _heartbeat(self, claimed: ClaimedRun, done: asyncio.Event) -> None:
        """Execution 完了まで lease を延長し、失敗時は TaskGroup 全体を停止する。"""

        while True:
            try:
                await asyncio.wait_for(
                    done.wait(),
                    timeout=self._heartbeat_interval_seconds,
                )
                return
            except TimeoutError:
                try:
                    await self._run_service.heartbeat_run_attempt(
                        claimed.run_attempt_id,
                        lease_token=claimed.lease_token,
                        lease_seconds=self._lease_seconds,
                    )
                except LeaseValidationError:
                    # Interaction transaction が lease を先に解放した直後の heartbeat race は
                    # 正常終了として扱い、待機 Run を失敗へ再終態化しない。
                    if done.is_set():
                        return
                    raise


async def _close_stream(stream: AsyncIterator[AgentEvent]) -> None:
    """打ち切った engine stream を閉じ、CLI subprocess の解放を待つ。"""

    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        await asyncio.wait_for(aclose(), timeout=30)
    except Exception:
        # Subprocess 解放の失敗が wall timeout の終態化を妨げてはならない。
        return


def _validate_context_identity(
    context: RunContext, claimed: ClaimedRun, sequence_start: int
) -> None:
    """Builder が claim identity や repository 採番を変更していないことを確認する。"""

    if context.run_id != claimed.run_id or context.run_attempt_id != claimed.run_attempt_id:
        raise ValueError("RunContext identity does not match ClaimedRun")
    if context.sequence_start != sequence_start:
        raise ValueError("RunContext sequence start does not match repository")


def _brief_execution_profile(context: RunContext) -> str | None:
    """監査 log 用に Brief が凍結した ExecutionProfile を取り出す。"""

    execution = context.task_brief.get("execution")
    if isinstance(execution, Mapping):
        profile = execution.get("profile")
        if isinstance(profile, str):
            return profile
    return None


def _lineage_event(
    event: AgentEvent, continuation_mode: SessionContinuationMode
) -> AgentEvent:
    """新 Segment の最初の Session event を lineage 固有 event へ写す。"""

    if event.event_type is not AgentEventType.SESSION_STARTED:
        return event
    event_type = {
        SessionContinuationMode.INITIAL: AgentEventType.SESSION_STARTED,
        SessionContinuationMode.RESUME: AgentEventType.SESSION_RESUMED,
        SessionContinuationMode.FORK: AgentEventType.SESSION_FORKED,
        SessionContinuationMode.REPLACE: AgentEventType.SESSION_REPLACED,
    }[continuation_mode]
    return replace(event, event_type=event_type)


def _engine_options_checksum(context: RunContext) -> str:
    """Session 再現性に必要な engine option identity を秘密なしで固定する。"""

    payload = {
        "model": context.model,
        "tools": [tool.capability for tool in context.tools],
        "permission_snapshot": dict(context.permission_snapshot),
        "limits": {
            "max_turns": context.limits.max_turns,
            "wall_timeout_seconds": context.limits.wall_timeout_seconds,
            "max_output_bytes": context.limits.max_output_bytes,
            "max_budget_usd": context.limits.max_budget_usd,
        },
        "result_schema": context.task_snapshot.get("output_schema_checksum"),
    }
    return f"sha256:{sha256_hex(canonical_json(payload))}"


def _merge_usage(event: AgentEvent, usage: dict[str, Any], cost: dict[str, Any]) -> None:
    """Result 保存用に最新 usage と total cost を収集する。"""

    raw_usage = event.payload.get("usage")
    if isinstance(raw_usage, Mapping):
        usage.update(raw_usage)
    total_cost = event.payload.get("total_cost_usd")
    if isinstance(total_cost, int | float):
        cost["total_cost_usd"] = float(total_cost)


def _terminal_mapping(
    event_type: AgentEventType,
) -> tuple[RunStatus, RunAttemptStatus, str] | None:
    """Agent terminal event を Run/Attempt status と error code へ変換する。"""

    if event_type is AgentEventType.ENGINE_FAILED:
        return RunStatus.FAILED, RunAttemptStatus.FAILED, "agent_engine_failed"
    if event_type is AgentEventType.SESSION_INTERRUPTED:
        return RunStatus.CANCELLED, RunAttemptStatus.CANCELLED, "user_interrupted"
    if event_type is AgentEventType.SESSION_DEFERRED:
        return RunStatus.WAITING_PERMISSION, RunAttemptStatus.DEFERRED, "permission_deferred"
    return None


def _first_exception(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """Nested ExceptionGroup から診断用の最初の例外型を取得する。"""

    current: BaseException = group.exceptions[0]
    while isinstance(current, BaseExceptionGroup):
        current = current.exceptions[0]
    return current
