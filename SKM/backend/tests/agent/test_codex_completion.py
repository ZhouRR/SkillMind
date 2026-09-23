"""空白停止時の中断 RPC 障害を、bounded cleanup と同じ呼出しで回収する。"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from skillmind.agent import codex_completion
from skillmind.skills.interpreter_execution import MODEL_OUTPUT_WHITESPACE_LIMIT
from skillmind.skills.model_interpreter import ModelProviderError


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [False, True])
async def test_stall_closes_and_reaps_even_when_interrupt_fails_or_never_acknowledges(
    monkeypatch: pytest.MonkeyPatch, blocked: bool,
) -> None:
    """RPC 失敗/無応答を有限待機し、同じ子の回収と待機 thread の解放を確認する。"""

    closed = threading.Event()
    interrupt_returned = threading.Event()
    calls: list[str] = []

    class Process:
        """SDK close が kill 後の wait を省いた状態を表す合成 process。"""

        def poll(self):
            """回収前の子として報告する。"""
            return None

        def kill(self):
            """強制終了対象が元 handle 一つであることを記録する。"""
            calls.append("kill")

        def wait(self, timeout):
            """有界 wait による reap を記録する。"""
            assert timeout == 5
            calls.append("reap")

    class Client:
        """起動・turn と中断待機だけを提供する SDK seam。"""

        _proc = Process()

        def thread_start(self, params):
            """設定が維持された元 thread を返す。"""
            return SimpleNamespace(
                model="synthetic-model", reasoning_effort=SimpleNamespace(value="medium"),
                thread=SimpleNamespace(id="original-thread"),
            )

        def turn_start(self, thread_id, prompt, params):
            """同じ一回の turn を開始する。"""
            calls.append("start")
            return SimpleNamespace(turn=SimpleNamespace(id="original-turn"))

        def turn_interrupt(self, thread_id, turn_id):
            """元 turn の RPC を故障させ、close が待機を解放する。"""
            assert (thread_id, turn_id) == ("original-thread", "original-turn")
            calls.append("interrupt")
            try:
                if blocked:
                    assert closed.wait(2)
                raise RuntimeError("private diagnostic")
            finally:
                interrupt_returned.set()

        def close(self):
            """SDK reader の transport failure 相当で元 RPC 待機を解放する。"""
            calls.append("close")
            closed.set()

    async def start(client):
        """合成 transport の開始を完了させる。"""

    async def notifications(client, turn_id):
        """進行 callback が無い場合も検出可能な空白 delta を返す。"""
        yield {"method": "item/agentMessage/delta", "params": {
            "itemId": "message-1", "delta": " " * 4096,
        }}
        raise AssertionError("No further response should be consumed")

    monkeypatch.setattr(codex_completion, "create_codex_client", lambda _: Client())
    monkeypatch.setattr(codex_completion, "start_codex", start)
    monkeypatch.setattr(codex_completion, "codex_notifications", notifications)
    monkeypatch.setattr(codex_completion, "_INTERRUPT_TIMEOUT_SECONDS", .02)
    configuration = SimpleNamespace(
        model="synthetic-model", effort="medium", client_config=lambda: None,
    )
    async with asyncio.timeout(2):
        with pytest.raises(ModelProviderError) as caught:
            await codex_completion.CodexCompletionClient(configuration).complete(
                system_prompt="JSON", user_message="JSON", response_schema={"type": "object"},
                model="synthetic-model", parameters={},
            )
    assert caught.value.detail == MODEL_OUTPUT_WHITESPACE_LIMIT
    assert "private" not in str(caught.value)
    assert await asyncio.to_thread(interrupt_returned.wait, .5)
    assert calls == ["start", "interrupt", "close", "kill", "reap"]


@pytest.mark.parametrize("failure", ["catalog", "configuration"])
async def test_preparation_failure_is_a_returned_provider_error(monkeypatch, failure):
    """開始許可取得後の準備例外が UNKNOWN へ漏れず、model 開始なしで確定可能になる。"""
    from skillmind.agent.codex_catalog import CodexCatalogError

    def configure():
        """model RPC の前に発生する例外を再現する。"""
        if failure == "catalog":
            raise CodexCatalogError("codex:model_catalog_model_not_found")
        raise ValueError("private-runtime-configuration")

    def unexpected(config):
        """準備失敗後に SDK client を開始していないことを確認する。"""
        raise AssertionError("SDK must not be created")

    monkeypatch.setattr(codex_completion, "create_codex_client", unexpected)
    configuration = SimpleNamespace(model="gpt-6-sol", effort="high", client_config=configure)
    with pytest.raises(ModelProviderError) as caught:
        await codex_completion.CodexCompletionClient(configuration).complete(
            system_prompt="JSON", user_message="JSON", response_schema={"type": "object"},
            model="gpt-6-sol", parameters={},
        )
    assert caught.value.detail == (
        "codex:model_catalog_model_not_found" if failure == "catalog"
        else "codex:runtime_preparation_failed"
    )
    assert "private-runtime" not in str(caught.value)


async def test_catalog_preparation_failure_finishes_original_request_without_unknown():
    """実 Interpreter/control/台帳の局部回帰。準備失敗は一回の既知 FAILED として保存する。"""
    from skillmind.agent.codex_catalog import CodexCatalogError
    from skillmind.skills.interpreter_execution import InterpreterExecutionError
    from skillmind.skills.request_execution import RequestCallControl
    from tests.skills.interpretation_request_harness import RequestSession
    from tests.skills.test_interpretation_requests import INPUT, accept, command
    from tests.skills.test_model_interpreter import _interpreter, _request

    def configure():
        """CLI がモデルを見つけず、thread 開始前に失敗したことを再現する。"""
        raise CodexCatalogError("codex:model_catalog_model_not_found")

    session = RequestSession()
    request = await accept(session)
    ledger = session.ledger()
    owner = await ledger.claim(request.request_id)
    assert owner is not None
    client = codex_completion.CodexCompletionClient(SimpleNamespace(
        model=INPUT["model"], effort="high", client_config=configure,
    ))
    with pytest.raises(InterpreterExecutionError) as caught:
        await _interpreter(client).interpret(
            _request(), model=INPUT["model"], parameters={},
            control=RequestCallControl(ledger, owner),
        )
    assert caught.value.code.value == "provider_error"
    assert caught.value.detail == "codex:model_catalog_model_not_found"
    assert len(session.calls) == 1 and session.calls[0].returned_at is not None
    saved = await ledger.finish(owner, command(request))
    assert session.requests[0].status == "FAILED"
    assert session.requests[0].interpretation_id == saved.interpretation_id
    assert await ledger.claim(request.request_id) is None
