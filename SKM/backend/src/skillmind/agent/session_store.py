"""エンジン非依存の transcript 契約と PostgreSQL 保存を実装する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.db.models import AgentSessionEntry, AgentSessionTranscript


@dataclass(frozen=True, slots=True)
class TranscriptKey:
    """エンジンごとの会話を区別する database 保存用のキー。"""

    project_key: str
    session_id: str
    subpath: str = ""


@dataclass(frozen=True, slots=True)
class StoredTranscript:
    """SessionStore backend から取得した transcript と storage 更新時刻。"""

    entries: tuple[dict[str, Any], ...]
    updated_at: datetime


class SessionTranscriptBackend(Protocol):
    """SDK interface と PostgreSQL transaction を分離する storage port。"""

    async def append(self, key: TranscriptKey, entries: tuple[dict[str, Any], ...]) -> None:
        """Opaque entry を順序維持かつ UUID idempotent に追加する。"""

        ...

    async def load(self, key: TranscriptKey) -> StoredTranscript | None:
        """一つの transcript を sequence 順で返す。"""

        ...

    async def list_main(self, project_key: str) -> tuple[tuple[str, datetime], ...]:
        """Project key 配下の main transcript と更新時刻を返す。"""

        ...

    async def delete(self, key: TranscriptKey) -> None:
        """Main または指定 subpath の transcript を削除する。"""

        ...

    async def list_subpaths(self, project_key: str, session_id: str) -> tuple[str, ...]:
        """Session 配下の subagent transcript path を返す。"""

        ...


class PostgresSessionTranscriptBackend:
    """Row lock と追加式 table で transcript の順序と冪等性を保証する。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Transaction ごとの AsyncSession factory を保持する。"""

        self._session_factory = session_factory

    async def append(self, key: TranscriptKey, entries: tuple[dict[str, Any], ...]) -> None:
        """Key row を lock し、重複 UUID を除いて連続 sequence で追加する。"""

        async with self._session_factory() as session, session.begin():
            transcript = await self._get_or_create_locked(session, key)
            uuids = {entry_uuid for entry in entries if (entry_uuid := _entry_uuid(entry))}
            existing_uuids: set[str] = set()
            if uuids:
                statement = select(AgentSessionEntry.entry_uuid).where(
                    AgentSessionEntry.transcript_id == transcript.id,
                    AgentSessionEntry.entry_uuid.in_(uuids),
                )
                existing_uuids = {
                    entry_uuid
                    for entry_uuid in await session.scalars(statement)
                    if entry_uuid is not None
                }

            planned = _plan_new_entries(entries, existing_uuids, transcript.next_sequence)
            if not planned:
                return
            now = datetime.now(UTC)
            session.add_all(
                [
                    AgentSessionEntry(
                        id=uuid4(),
                        transcript_id=transcript.id,
                        sequence=sequence,
                        entry_uuid=_entry_uuid(entry),
                        entry_json=entry,
                        content_hash=_content_hash(entry),
                        created_at=now,
                    )
                    for sequence, entry in planned
                ]
            )
            transcript.next_sequence = planned[-1][0] + 1
            transcript.updated_at = now

    async def load(self, key: TranscriptKey) -> StoredTranscript | None:
        """Key が存在する場合だけ sequence 順の opaque JSON を返す。"""

        async with self._session_factory() as session:
            transcript = await self._find_transcript(session, key)
            if transcript is None:
                return None
            statement = (
                select(AgentSessionEntry.entry_json)
                .where(AgentSessionEntry.transcript_id == transcript.id)
                .order_by(AgentSessionEntry.sequence)
            )
            entries = tuple(dict(entry) for entry in await session.scalars(statement))
            return StoredTranscript(entries=entries, updated_at=transcript.updated_at)

    async def list_main(self, project_key: str) -> tuple[tuple[str, datetime], ...]:
        """Subpath を除外して main transcript のみを更新時刻降順で取得する。"""

        async with self._session_factory() as session:
            statement = (
                select(AgentSessionTranscript.session_id, AgentSessionTranscript.updated_at)
                .where(
                    AgentSessionTranscript.project_key == project_key,
                    AgentSessionTranscript.subpath == "",
                )
                .order_by(AgentSessionTranscript.updated_at.desc())
            )
            rows = (await session.execute(statement)).all()
            return tuple((session_id, updated_at) for session_id, updated_at in rows)

    async def delete(self, key: TranscriptKey) -> None:
        """Main key は Session 全体、subpath key は対象 transcript だけを削除する。"""

        conditions = [
            AgentSessionTranscript.project_key == key.project_key,
            AgentSessionTranscript.session_id == key.session_id,
        ]
        if key.subpath:
            conditions.append(AgentSessionTranscript.subpath == key.subpath)
        async with self._session_factory() as session, session.begin():
            await session.execute(delete(AgentSessionTranscript).where(*conditions))

    async def list_subpaths(self, project_key: str, session_id: str) -> tuple[str, ...]:
        """Main transcript を除く subpath を決定的な順序で取得する。"""

        async with self._session_factory() as session:
            statement = (
                select(AgentSessionTranscript.subpath)
                .where(
                    AgentSessionTranscript.project_key == project_key,
                    AgentSessionTranscript.session_id == session_id,
                    AgentSessionTranscript.subpath != "",
                )
                .order_by(AgentSessionTranscript.subpath)
            )
            return tuple(await session.scalars(statement))

    async def _get_or_create_locked(
        self, session: AsyncSession, key: TranscriptKey
    ) -> AgentSessionTranscript:
        """Unique key を作成後に row lock し、sequence 採番を直列化する。"""

        now = datetime.now(UTC)
        statement = (
            insert(AgentSessionTranscript)
            .values(
                id=uuid4(),
                project_key=key.project_key,
                session_id=key.session_id,
                subpath=key.subpath,
                next_sequence=1,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_agent_session_transcripts_key")
        )
        await session.execute(statement)
        transcript = await self._find_transcript(session, key, lock=True)
        if transcript is None:
            raise RuntimeError("Failed to create or lock agent session transcript")
        return transcript

    async def _find_transcript(
        self, session: AsyncSession, key: TranscriptKey, *, lock: bool = False
    ) -> AgentSessionTranscript | None:
        """正規化 key と一致する transcript row を取得する。"""

        statement = select(AgentSessionTranscript).where(
            AgentSessionTranscript.project_key == key.project_key,
            AgentSessionTranscript.session_id == key.session_id,
            AgentSessionTranscript.subpath == key.subpath,
        )
        if lock:
            statement = statement.with_for_update()
        return (await session.scalars(statement)).one_or_none()


def _entry_uuid(entry: Mapping[str, Any]) -> str | None:
    """有効な SDK UUID がある entry だけを idempotency 対象にする。"""

    value = entry.get("uuid")
    return value if isinstance(value, str) and value else None


def _plan_new_entries(
    entries: tuple[dict[str, Any], ...], existing_uuids: set[str], start_sequence: int
) -> tuple[tuple[int, dict[str, Any]], ...]:
    """既存・batch 内重複 UUID を除外して連続 sequence を割り当てる。"""

    planned: list[tuple[int, dict[str, Any]]] = []
    seen = set(existing_uuids)
    sequence = start_sequence
    for entry in entries:
        entry_uuid = _entry_uuid(entry)
        if entry_uuid is not None and entry_uuid in seen:
            continue
        planned.append((sequence, entry))
        sequence += 1
        if entry_uuid is not None:
            seen.add(entry_uuid)
    return tuple(planned)


def _content_hash(entry: Mapping[str, Any]) -> str:
    """JSON key 順序に依存しない監査用 SHA-256 hash を返す。"""

    return sha256_hex(canonical_json(entry))
