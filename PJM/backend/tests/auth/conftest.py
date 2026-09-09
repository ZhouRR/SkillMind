"""Login の実 Lua 回帰だけに専用 Redis process を提供する。"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError


@pytest.fixture
async def isolated_redis(tmp_path: Path) -> AsyncIterator[Redis]:
    """TCP と永続化を無効にした所有 process だけを起動し、必ず終了させる。"""

    explicit = os.environ.get("PROJECTMIND_TEST_REDIS_SERVER")
    binary = explicit or shutil.which("redis-server")
    if not binary:
        pytest.skip("A Redis server binary is required for isolated Lua verification")
    if not Path(binary).is_absolute() or not Path(binary).is_file():
        pytest.fail("The selected Redis server must be an existing absolute file")
    socket = tmp_path / "redis.sock"
    # 短い独立 socket を使い、既存 PROJECTMIND_REDIS_URL の DB には一切触れない。
    process = subprocess.Popen(
        [
            binary,
            "--port",
            "0",
            "--unixsocket",
            str(socket),
            "--unixsocketperm",
            "700",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(tmp_path),
            "--loglevel",
            "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    redis = Redis(
        unix_socket_path=str(socket),
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        async with asyncio.timeout(5):
            while True:
                if process.poll() is not None:
                    pytest.fail("The isolated Redis server exited before readiness")
                try:
                    if await redis.ping():
                        break
                except RedisConnectionError:
                    pass
                await asyncio.sleep(0.02)
        yield redis
    finally:
        await redis.aclose()
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
        if process.stdout is not None:
            process.stdout.close()
