"""専用 Redis process で Lua の原子性、TTL と多次元 login 制限を検証する。"""

from __future__ import annotations

import asyncio
import json

import pytest
from redis.asyncio import Redis

from skillmind.auth.login_protection import (
    LoginProtection,
    LoginProtectionPolicy,
    LoginProtectionUnavailableError,
    LoginRateLimitedError,
)


def guard(redis: Redis, *, pair: int = 5, account: int = 15, source: int = 100) -> LoginProtection:
    """実 EVAL を各 service instance から同じ専用 Redis へ送る。"""

    return LoginProtection(redis, LoginProtectionPolicy(pair, account, source, 2))


async def attempt(
    guard: LoginProtection, *, email: str = "reader@example.com", ip: str = "192.0.2.10"
) -> None:
    """來源を一度数えた receipt で account/組合の password 前 gate を検査する。"""

    receipt = await guard.begin(ip)
    await guard.check_account(email, guard.consume(receipt))


@pytest.mark.asyncio
async def test_concurrent_instances_admit_only_the_pair_allowance(isolated_redis: Redis) -> None:
    """32 個の instance が競合しても同じ組合へ五回だけ admission を発行する。"""

    results = await asyncio.gather(
        *(attempt(guard(isolated_redis, account=100)) for _ in range(32)),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 5
    assert all(result is None or isinstance(result, LoginRateLimitedError) for result in results)
    keys = await isolated_redis.keys("skillmind:auth:limit:v2:*")
    assert len(keys) == 3
    assert all(await asyncio.gather(*(isolated_redis.pttl(key) for key in keys)))
    for key in keys:
        assert await isolated_redis.pttl(key) > 0
        assert "reader@example.com" not in key and "192.0.2.10" not in key


@pytest.mark.asyncio
async def test_account_quota_survives_source_changes(isolated_redis: Redis) -> None:
    """來源を変えても一つの account への合算上限を越えられない。"""

    limiter = guard(isolated_redis, account=3)
    for last in range(3):
        await attempt(limiter, ip=f"192.0.2.{last}")
    with pytest.raises(LoginRateLimitedError):
        await attempt(limiter, ip="192.0.2.99")


@pytest.mark.asyncio
async def test_source_quota_survives_email_changes(isolated_redis: Redis) -> None:
    """Account を変える password spraying も同じ來源の quota で止まる。"""

    limiter = guard(isolated_redis, source=2)
    await attempt(limiter, email="first@example.com")
    await attempt(limiter, email="second@example.com")
    with pytest.raises(LoginRateLimitedError):
        await attempt(limiter, email="third@example.com")
    assert len(await isolated_redis.keys("*:account")) == 2


@pytest.mark.asyncio
async def test_blocked_requests_do_not_extend_state_or_ttl(isolated_redis: Redis) -> None:
    """拒絶中の連打は count・blocked 時点・TTL を延ばさず、永久 lock にしない。"""

    limiter = guard(isolated_redis, source=1)
    await limiter.begin("192.0.2.10")
    with pytest.raises(LoginRateLimitedError):
        await limiter.begin("192.0.2.10")
    (key,) = await isolated_redis.keys("*")
    before, ttl = await isolated_redis.get(key), await isolated_redis.pttl(key)
    for _ in range(12):
        with pytest.raises(LoginRateLimitedError):
            await limiter.begin("192.0.2.10")
    assert await isolated_redis.get(key) == before
    assert 0 < await isolated_redis.pttl(key) <= ttl


@pytest.mark.asyncio
async def test_progressive_delay_is_capped_and_decays_after_quiet(isolated_redis: Redis) -> None:
    """実 Redis 時刻と保存状態で 60/120/240/300 秒、静默後の減衰を検証する。"""

    limiter = guard(isolated_redis, source=1)
    for expected in (60, 120, 240, 300, 300):
        await limiter.begin("192.0.2.10")
        with pytest.raises(LoginRateLimitedError) as caught:
            await limiter.begin("192.0.2.10")
        assert caught.value.retry_after_seconds == expected
        (key,) = await isolated_redis.keys("*")
        state = json.loads(await isolated_redis.get(key))
        # 待機時間そのものを sleep せず、専用 state の時点を過去へ移す。
        for name in ("start", "last", "blocked"):
            state[name] = max(0, state[name] - (expected + 1) * 1000)
        await isolated_redis.set(key, json.dumps(state), px=1260000)
    state["last"] -= 901000
    state["start"] -= 901000
    state["blocked"] = 0
    await isolated_redis.set(key, json.dumps(state), px=1260000)
    await limiter.begin("192.0.2.10")
    assert json.loads(await isolated_redis.get(key))["strikes"] == 0


@pytest.mark.asyncio
async def test_actual_key_expiry_reopens_the_quota(isolated_redis: Redis) -> None:
    """実 TTL 満了で短期防護が復旧し、古い blocked 値を永久保存しない。"""

    limiter = guard(isolated_redis, source=1)
    await limiter.begin("192.0.2.10")
    with pytest.raises(LoginRateLimitedError):
        await limiter.begin("192.0.2.10")
    (key,) = await isolated_redis.keys("*")
    await isolated_redis.pexpire(key, 30)
    async with asyncio.timeout(2):
        while await isolated_redis.exists(key):
            await asyncio.sleep(0.01)
    await limiter.begin("192.0.2.10")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["wrong-type", "invalid-json", "missing-ttl", "wrong-version"]
)
async def test_corrupted_store_is_not_an_empty_allowance(
    isolated_redis: Redis, corruption: str
) -> None:
    """壊れた共有状態を削除・初期化して通過させず、password を拒否する。"""

    limiter = guard(isolated_redis)
    await limiter.begin("192.0.2.10")
    (key,) = await isolated_redis.keys("*")
    if corruption == "wrong-type":
        await isolated_redis.delete(key)
        await isolated_redis.lpush(key, "invalid")
    elif corruption == "invalid-json":
        await isolated_redis.set(key, "invalid", px=1000)
    elif corruption == "missing-ttl":
        await isolated_redis.persist(key)
    else:
        state = json.loads(await isolated_redis.get(key))
        state["version"] = 99
        await isolated_redis.set(key, json.dumps(state), px=1000)
    with pytest.raises(LoginProtectionUnavailableError):
        await limiter.begin("192.0.2.10")
