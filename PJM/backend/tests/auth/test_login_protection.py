"""Login の admission identity、失敗閉鎖と Redis 呼出し契約を検証する。"""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from projectmind.auth.login_protection import (
    LoginProtection,
    LoginProtectionPolicy,
    LoginProtectionUnavailableError,
    LoginRateLimitedError,
    source_identity,
)


def protection(redis: object) -> LoginProtection:
    """I/O 失敗を短く検証できる明示的 policy を用意する。"""

    return LoginProtection(cast(Redis, redis), LoginProtectionPolicy(5, 15, 100, 0.01))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("192.0.2.10", "192.0.2.10"),
        ("::ffff:192.0.2.10", "192.0.2.10"),
        ("2001:0db8:0:0::1", "2001:db8::1"),
        ("invalid-client", "unknown"),
        ("", "unknown"),
    ],
)
def test_source_spelling_cannot_split_a_known_ip(value: str, expected: str) -> None:
    """IPv4 mapped IPv6 と表記の差で同じ來源を別の quota にしない。"""

    assert source_identity(value) == expected


@pytest.mark.asyncio
async def test_admission_is_single_use_and_bound_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """別 service・再利用・遅延した body から password path へ進めない。"""

    redis = AsyncMock()
    redis.eval.return_value = [1, 0]
    first, second = protection(redis), protection(redis)
    receipt = await first.begin("192.0.2.10")
    with pytest.raises(LoginProtectionUnavailableError):
        second.consume(receipt)
    assert first.consume(receipt) == receipt.source_hash
    with pytest.raises(LoginProtectionUnavailableError):
        first.consume(receipt)
    expired = await first.begin("192.0.2.10")
    monkeypatch.setattr("projectmind.auth.login_protection.monotonic", lambda: expired.expires_at)
    with pytest.raises(LoginProtectionUnavailableError):
        first.consume(expired)
    assert redis.eval.await_count == 2


@pytest.mark.asyncio
async def test_account_and_pair_use_one_script_with_hashed_identity() -> None:
    """共通 hash tag の二 key を原子判定へ渡し、email/IP を保存名へ露出しない。"""

    redis = AsyncMock()
    redis.eval.return_value = [1, 0]
    guard = protection(redis)
    receipt = await guard.begin("192.0.2.10")
    await guard.check_account("reader@example.com", guard.consume(receipt))
    args = redis.eval.await_args.args
    assert args[1] == 2 and args[-2:] == ("15", "5")
    assert args[2].split("}")[0] == args[3].split("}")[0]
    assert "reader@example.com" not in repr(args[2:])
    assert "192.0.2.10" not in repr(args[2:])


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, [], [True, 0], [1, 1], [0, 0], [0, 301], ["1", "0"]])
async def test_invalid_store_reply_never_admits_login(result: object) -> None:
    """部分的・不明な reply を通過扱いにせず、公開の失敗へ閉じる。"""

    redis = AsyncMock()
    redis.eval.return_value = result
    with pytest.raises(LoginProtectionUnavailableError):
        await protection(redis).begin("192.0.2.10")


@pytest.mark.asyncio
async def test_store_outage_and_timeout_are_not_unlimited_login() -> None:
    """断線と遅い store を拒否し、application で EVAL を再試行しない。"""

    redis = AsyncMock()
    redis.eval.side_effect = RedisConnectionError("unavailable")
    with pytest.raises(LoginProtectionUnavailableError):
        await protection(redis).begin("192.0.2.10")
    redis.eval.assert_awaited_once()

    async def wait_forever(*args: object) -> None:
        """取消されるまで返さない Redis response を作る。"""

        await asyncio.Future()

    redis.eval.reset_mock()
    redis.eval.side_effect = wait_forever
    with pytest.raises(LoginProtectionUnavailableError):
        await protection(redis).begin("192.0.2.10")
    redis.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_limit_exposes_only_bounded_retry_after() -> None:
    """利用可能な待機秒数を返し、内部 dimension を例外へ含めない。"""

    redis = AsyncMock()
    redis.eval.return_value = [0, 120]
    with pytest.raises(LoginRateLimitedError) as caught:
        await protection(redis).begin("192.0.2.10")
    assert caught.value.retry_after_seconds == 120
    assert "192.0.2.10" not in str(caught.value)
