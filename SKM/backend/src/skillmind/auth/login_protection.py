"""Redis の有期限状態で、来源・account・組合の login admission を制御する。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from ipaddress import IPv6Address, ip_address
from time import monotonic
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from skillmind.core.hashing import sha256_hex

# GET と SET/PX を一つの EVAL に閉じ、途中停止で無期限の counter を残さない。
# Lua は失敗時に rollback しないため、各 SET 自体にも必ず TTL を付ける。
_RATE_SCRIPT = """
local window = 60000
local base = 60000
local maximum = 300000
local quiet = 900000
local lifetime = quiet + maximum + window
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local states = {}
local retry = 0

-- 外部破損や版の違いを空状態として扱うと、保護を消してしまう。
local function integer(value)
    return type(value) == 'number' and value >= 0 and value < 9007199254740991
        and value == math.floor(value)
end

for i, key in ipairs(KEYS) do
    local limit = tonumber(ARGV[i])
    if not integer(limit) or limit < 1 or limit > 10000 then
        return redis.error_reply('invalid login limit policy')
    end
    local raw = redis.call('GET', key)
    local state = nil
    if raw then
        local ok, decoded = pcall(cjson.decode, raw)
        if not ok or type(decoded) ~= 'table' or decoded.version ~= 1
            or not integer(decoded.start) or not integer(decoded.count)
            or not integer(decoded.strikes) or decoded.strikes > 4
            or not integer(decoded.blocked) or not integer(decoded.last)
            or decoded.blocked > decoded.last + maximum
            or redis.call('PTTL', key) < 0 then
            return redis.error_reply('invalid login limit state')
        end
        state = decoded
        if state.blocked > now then
            retry = math.max(retry, state.blocked - now)
        end
    end
    if not state or now - state.last >= quiet then
        state = {version=1, start=now, count=0, strikes=0, blocked=0, last=now}
    end
    states[i] = state
end

-- 拒絶中の連打で期限を延ばさず、他の dimension の状態も作成しない。
if retry > 0 then
    return {0, math.ceil(retry / 1000)}
end

local encoded = {}
for i, state in ipairs(states) do
    if now - state.start >= window then
        state.start = now
        state.count = 0
    end
    state.count = state.count + 1
    state.last = now
    state.blocked = 0
    if state.count > tonumber(ARGV[i]) then
        state.strikes = math.min(state.strikes + 1, 4)
        local delay = math.min(base * (2 ^ (state.strikes - 1)), maximum)
        state.blocked = now + delay
        retry = math.max(retry, delay)
    end
    -- 全 candidate の encoding を先に済ませ、型エラーによる片側更新を避ける。
    encoded[i] = cjson.encode(state)
end
for i, key in ipairs(KEYS) do
    redis.call('SET', key, encoded[i], 'PX', lifetime)
end
return {retry == 0 and 1 or 0, math.ceil(retry / 1000)}
"""


class LoginProtectionUnavailableError(RuntimeError):
    """短期防護を確認できず、password 検証へ進めないことを示す。"""


class LoginRateLimitedError(RuntimeError):
    """公開してよい待機秒数だけを持ち、拒否された dimension を隠す。"""

    def __init__(self, retry_after_seconds: int) -> None:
        """bounded な Retry-After を保持し、email/IP や Redis 値を含めない。"""

        super().__init__("Too many login attempts")
        self.retry_after_seconds = max(1, min(retry_after_seconds, 300))


@dataclass(frozen=True, slots=True)
class LoginProtectionPolicy:
    """一分の admission 数と、短期 store 応答の待機上限を定義する。"""

    pair_attempts: int
    account_attempts: int
    source_requests: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        """Settings 以外の内部構築でも無制限や非有限 timeout を拒絶する。"""

        if (
            any(
                type(value) is not int or not 1 <= value <= 10000
                for value in (self.pair_attempts, self.account_attempts, self.source_requests)
            )
            or not 0 < self.timeout_seconds <= 10
        ):
            raise ValueError("Invalid login protection policy")


@dataclass(slots=True)
class LoginAdmission:
    """同一 service 内で一回だけ消費する来源確認結果。公開 token ではない。"""

    source_hash: str = field(repr=False)
    owner: object = field(repr=False)
    expires_at: float = field(repr=False)
    consumed: bool = False


def source_identity(client_address: str) -> str:
    """可信 ASGI client の IP 表記差を正規化し、不明来源は共通の桶へ閉じる。"""

    try:
        address = ip_address(client_address)
    except ValueError:
        return "unknown"
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


class LoginProtection:
    """DB や password を参照せず、全 API instance で同じ admission を強制する。"""

    def __init__(self, redis: Redis, policy: LoginProtectionPolicy) -> None:
        """期限付き共有 store と、一回用 receipt の発行主体を保持する。"""

        self._redis = redis
        self.policy = policy
        self._owner = object()

    async def begin(self, client_address: str) -> LoginAdmission:
        """Body/Origin/challenge より前に来源 request を計上する。"""

        source_hash = sha256_hex(source_identity(client_address))
        await self._check(
            (f"skillmind:auth:limit:v2:source:{source_hash}",), (self.policy.source_requests,)
        )
        return LoginAdmission(source_hash, self._owner, monotonic() + 30)

    def consume(self, admission: LoginAdmission) -> str:
        """別 service、期限超過、二重利用の receipt を拒否し、来源 hash を返す。"""

        if (
            admission.owner is not self._owner
            or admission.consumed
            or monotonic() >= admission.expires_at
        ):
            raise LoginProtectionUnavailableError("Login admission is unavailable")
        admission.consumed = True
        return admission.source_hash

    async def check_account(self, normalized_email: str, source_hash: str) -> None:
        """Account と組合を同じ Redis transaction で数え、来源変更の回避を防ぐ。"""

        account = sha256_hex(normalized_email)
        # 二 key は同じ hash tag を使う。公開 API に cluster 構成を約束するものではない。
        prefix = f"skillmind:auth:limit:v2:{{{account}}}"
        await self._check(
            (f"{prefix}:account", f"{prefix}:pair:{source_hash}"),
            (self.policy.account_attempts, self.policy.pair_attempts),
        )

    async def _check(self, keys: tuple[str, ...], limits: tuple[int, ...]) -> None:
        """原子判断が不明なら再試行せず拒否し、password path を fail closed にする。"""

        try:
            async with asyncio.timeout(self.policy.timeout_seconds):
                # redis-py 5 の共用 mixin は sync/async の union 型を返すが、この client は async。
                result = await cast(
                    Awaitable[object],
                    self._redis.eval(
                        _RATE_SCRIPT,
                        len(keys),
                        *keys,
                        *(str(limit) for limit in limits),
                    ),
                )
        except (RedisError, TimeoutError) as error:
            raise LoginProtectionUnavailableError("Login protection is unavailable") from error
        if (
            not isinstance(result, list)
            or len(result) != 2
            or any(type(value) is not int for value in result)
            or result[0] not in (0, 1)
            or not 0 <= result[1] <= 300
            or (result[0] == 1) != (result[1] == 0)
        ):
            raise LoginProtectionUnavailableError("Login protection is unavailable")
        if result[0] == 0:
            raise LoginRateLimitedError(result[1])
