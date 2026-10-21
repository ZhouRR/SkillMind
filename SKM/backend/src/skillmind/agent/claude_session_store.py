"""Claude SDK の SessionStore を共有 transcript backend へ接続する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, cast

from claude_agent_sdk.types import (
    SessionKey,
    SessionListSubkeysKey,
    SessionStore,
    SessionStoreEntry,
    SessionStoreListEntry,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.session_store import (
    PostgresSessionTranscriptBackend,
    SessionTranscriptBackend,
    TranscriptKey,
)


class PostgresSessionStore(SessionStore):
    """SDK SessionStore protocol を PostgreSQL backend へ接続する adapter。"""

    def __init__(self, backend: SessionTranscriptBackend) -> None:
        """Skillmind が管理する transcript backend を保持する。"""

        self._backend = backend

    @classmethod
    def from_session_factory(
        cls, session_factory: async_sessionmaker[AsyncSession]
    ) -> PostgresSessionStore:
        """Application の transaction factory から production adapter を構築する。"""

        return cls(PostgresSessionTranscriptBackend(session_factory))

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        """SDK entry を JSON-safe copy に正規化して追加する。"""

        normalized_key = _normalize_key(key)
        normalized_entries = tuple(_normalize_entry(entry) for entry in entries)
        if normalized_entries:
            await self._backend.append(normalized_key, normalized_entries)

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        """保存済み entry を SDK が materialize できる形で無損失に返す。"""

        stored = await self._backend.load(_normalize_key(key))
        if stored is None:
            return None
        return [cast(SessionStoreEntry, dict(entry)) for entry in stored.entries]

    async def list_sessions(self, project_key: str) -> list[SessionStoreListEntry]:
        """Main transcript だけを SDK の epoch millisecond 形式で列挙する。"""

        _validate_component("project_key", project_key, maximum=200)
        sessions = await self._backend.list_main(project_key)
        return [
            {
                "session_id": session_id,
                "mtime": int(updated_at.timestamp() * 1000),
            }
            for session_id, updated_at in sessions
        ]

    async def delete(self, key: SessionKey) -> None:
        """Main key の場合は backend 側で全 subpath も cascade delete する。"""

        await self._backend.delete(_normalize_key(key))

    async def list_subkeys(self, key: SessionListSubkeysKey) -> list[str]:
        """Resume 時に materialize する subagent transcript path を返す。"""

        project_key = _validate_component("project_key", key["project_key"], maximum=200)
        session_id = _validate_component("session_id", key["session_id"], maximum=128)
        return list(await self._backend.list_subpaths(project_key, session_id))


def _normalize_key(key: Mapping[str, Any]) -> TranscriptKey:
    """SDK key の長さと main/subpath 表現を database 用に検証する。"""

    project_key = _validate_component("project_key", key.get("project_key"), maximum=200)
    session_id = _validate_component("session_id", key.get("session_id"), maximum=128)
    raw_subpath = key.get("subpath")
    if raw_subpath == "":
        raise ValueError("Session subpath must be omitted instead of empty")
    subpath = ""
    if raw_subpath is not None:
        subpath = _validate_subpath(raw_subpath)
    return TranscriptKey(project_key=project_key, session_id=session_id, subpath=subpath)


def _validate_component(name: str, value: Any, *, maximum: int) -> str:
    """Storage key component を空文字、制御文字、過長値から保護する。"""

    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ValueError(f"Invalid SessionStore {name}")
    return value


def _validate_subpath(value: Any) -> str:
    """Resume materialize 時に root 外へ出られない portable subpath だけを受け付ける。"""

    subpath = _validate_component("subpath", value, maximum=1024)
    path = PurePosixPath(subpath)
    if path.is_absolute() or "\\" in subpath or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Invalid SessionStore subpath")
    return subpath


def _normalize_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Opaque entry が JSON-safe object で type を持つことだけを検証する。"""

    if not isinstance(entry.get("type"), str) or not entry["type"]:
        raise ValueError("SessionStore entry requires a non-empty type")
    entry_uuid = entry.get("uuid")
    if entry_uuid is not None and (
        not isinstance(entry_uuid, str) or not entry_uuid or len(entry_uuid) > 128
    ):
        raise ValueError("SessionStore entry uuid must be a non-empty string")
    try:
        # JSON round-trip で caller の mutable object から切り離し、NaN などを拒否する。
        return cast(
            dict[str, Any],
            json.loads(json.dumps(dict(entry), ensure_ascii=False, allow_nan=False)),
        )
    except (TypeError, ValueError) as error:
        raise ValueError("SessionStore entry must be JSON-safe") from error
