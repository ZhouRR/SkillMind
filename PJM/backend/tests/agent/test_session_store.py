"""PostgreSQL SessionStore adapter の SDK conformance と追加規則を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from claude_agent_sdk.types import SessionKey

from projectmind.agent.session_store import (
    PostgresSessionStore,
    StoredTranscript,
    TranscriptKey,
    _content_hash,
    _plan_new_entries,
)


class MemoryTranscriptBackend:
    """SDK adapter conformance を database 接続なしで検証する in-memory backend。"""

    def __init__(self) -> None:
        """空の transcript と決定的な storage clock を用意する。"""

        self.entries: dict[TranscriptKey, list[dict[str, Any]]] = {}
        self.updated_at: dict[TranscriptKey, datetime] = {}
        self.clock = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

    async def append(self, key: TranscriptKey, entries: tuple[dict[str, Any], ...]) -> None:
        """Production と同じ UUID dedup と連続順序で entry を追加する。"""

        current = self.entries.setdefault(key, [])
        existing = {
            value for entry in current if isinstance((value := entry.get("uuid")), str) and value
        }
        planned = _plan_new_entries(entries, existing, len(current) + 1)
        current.extend(entry for _, entry in planned)
        if planned:
            self.clock += timedelta(milliseconds=1)
            self.updated_at[key] = self.clock

    async def load(self, key: TranscriptKey) -> StoredTranscript | None:
        """存在しない key と空でない transcript を区別して返す。"""

        if key not in self.entries:
            return None
        return StoredTranscript(
            entries=tuple(dict(entry) for entry in self.entries[key]),
            updated_at=self.updated_at[key],
        )

    async def list_main(self, project_key: str) -> tuple[tuple[str, datetime], ...]:
        """指定 project の main transcript だけを返す。"""

        return tuple(
            (key.session_id, self.updated_at[key])
            for key in self.entries
            if key.project_key == project_key and not key.subpath
        )

    async def delete(self, key: TranscriptKey) -> None:
        """Main key は Session 全体、subpath は対象だけを削除する。"""

        targets = [key]
        if not key.subpath:
            targets = [
                candidate
                for candidate in self.entries
                if candidate.project_key == key.project_key
                and candidate.session_id == key.session_id
            ]
        for target in targets:
            self.entries.pop(target, None)
            self.updated_at.pop(target, None)

    async def list_subpaths(self, project_key: str, session_id: str) -> tuple[str, ...]:
        """Main key を除く subpath を sort して返す。"""

        return tuple(
            sorted(
                key.subpath
                for key in self.entries
                if key.project_key == project_key and key.session_id == session_id and key.subpath
            )
        )


def _main_key(*, project_key: str = "project-a", session_id: str = "session-1") -> SessionKey:
    """Main transcript 用 SDK SessionKey を返す。"""

    return {"project_key": project_key, "session_id": session_id}


@pytest.mark.asyncio
async def test_append_load_round_trip_and_uuid_idempotency() -> None:
    """Opaque JSON は無損失で往復し、同じ UUID の retry は一度だけ保存する。"""

    backend = MemoryTranscriptBackend()
    store = PostgresSessionStore(backend)
    key = _main_key()
    first = {
        "type": "assistant",
        "uuid": "entry-1",
        "message": {"content": [{"type": "text", "text": "分析結果"}]},
    }
    marker = {"type": "custom-title", "title": "JAF-1234"}

    await store.append(key, [first, marker])
    first["message"] = {"mutated": True}
    await store.append(key, [{"type": "assistant", "uuid": "entry-1"}, marker])

    loaded = await store.load(key)
    assert loaded == [
        {
            "type": "assistant",
            "uuid": "entry-1",
            "message": {"content": [{"type": "text", "text": "分析結果"}]},
        },
        marker,
        marker,
    ]


@pytest.mark.asyncio
async def test_subkeys_listing_and_main_delete_cascade() -> None:
    """Resume が subagent transcript を発見でき、Main 削除で孤立 data を残さない。"""

    backend = MemoryTranscriptBackend()
    store = PostgresSessionStore(backend)
    main = _main_key()
    subagent: SessionKey = {
        **main,
        "subpath": "subagents/agent-reviewer",
    }
    await store.append(main, [{"type": "user", "uuid": "main-1"}])
    await store.append(subagent, [{"type": "assistant", "uuid": "sub-1"}])

    assert await store.list_subkeys(main) == ["subagents/agent-reviewer"]
    assert len(await store.list_sessions("project-a")) == 1

    await store.delete(main)

    assert await store.load(main) is None
    assert await store.load(subagent) is None
    assert await store.list_subkeys(main) == []


@pytest.mark.asyncio
async def test_project_key_isolation() -> None:
    """同じ session ID でも project_key が異なれば相互に列挙・読取されない。"""

    backend = MemoryTranscriptBackend()
    store = PostgresSessionStore(backend)
    await store.append(_main_key(project_key="project-a"), [{"type": "user"}])
    await store.append(_main_key(project_key="project-b"), [{"type": "user"}])

    project_a = await store.list_sessions("project-a")

    assert [item["session_id"] for item in project_a] == ["session-1"]


@pytest.mark.asyncio
async def test_invalid_key_and_non_json_entry_are_rejected() -> None:
    """Materialize 不能な key や JSON 値を database transaction 前に拒否する。"""

    store = PostgresSessionStore(MemoryTranscriptBackend())
    with pytest.raises(ValueError, match="subpath"):
        await store.append({**_main_key(), "subpath": ""}, [{"type": "user"}])
    with pytest.raises(ValueError, match="subpath"):
        await store.append(
            {**_main_key(), "subpath": "../outside"},
            [{"type": "user"}],
        )
    with pytest.raises(ValueError, match="non-empty type"):
        await store.append(_main_key(), [{"uuid": "entry-1"}])  # type: ignore[typeddict-item]
    with pytest.raises(ValueError, match="JSON-safe"):
        await store.append(
            _main_key(),
            [{"type": "user", "unsupported": object()}],  # type: ignore[typeddict-unknown-key]
        )


def test_append_plan_assigns_continuous_sequence_after_deduplication() -> None:
    """DB row lock 内の sequence 採番が retry や batch 内重複で欠番を作らない。"""

    planned = _plan_new_entries(
        (
            {"type": "assistant", "uuid": "existing"},
            {"type": "assistant", "uuid": "new"},
            {"type": "assistant", "uuid": "new"},
            {"type": "marker"},
        ),
        {"existing"},
        7,
    )

    assert [sequence for sequence, _ in planned] == [7, 8]
    assert [entry.get("uuid") for _, entry in planned] == ["new", None]


def test_content_hash_is_independent_of_json_key_order() -> None:
    """JSONB の key 並び替え後も監査 checksum が一致する。"""

    assert _content_hash({"type": "user", "uuid": "1"}) == _content_hash(
        {"uuid": "1", "type": "user"}
    )
