"""原入力 object の別名変更に対する同期 import の複写境界を実 service で検証する。"""

from __future__ import annotations

from dataclasses import replace
from typing import cast
from uuid import uuid4

import pytest

from tests.skills.skill_import_authorization_harness import (
    INLINE_FILES,
    KINDS,
    UPLOAD_FILES,
    ImportKind,
)
from tests.skills.test_skill_import_authorization import prepare


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("target", ["actor", "session-token"])
async def test_first_await_cannot_replace_fields_inside_original_access_object(
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
    target: str,
) -> None:
    """変数の付替えではなく原 frozen object 自体を破損させ、私有 deepcopy が必要と示す。"""
    session = await prepare(monkeypatch, kind)
    original = session.access
    original_user_id = original.actor.user_id
    original_session_token = original.session_token

    def change_original(point: str) -> None:
        """呼出側 alias の故意の変更を再現し、実保存 User/Session は一切変更しない。"""
        if point == "organization:1":
            if target == "actor":
                object.__setattr__(original.actor, "user_id", uuid4())
            else:
                object.__setattr__(original, "session_token", "synthetic-invalid-token")

    session.on_step = change_original
    stored = await session.operate(kind)
    assert session.sources[0].imported_by == original_user_id
    assert stored.organization_id == session.organization_id
    assert session.user.id == session.auth_session.user_id == original_user_id
    if target == "actor":
        assert original.actor.user_id != original_user_id
    else:
        assert original.session_token != original_session_token
    assert len(session.storage.attempts) == (2 if kind == "upload" else 0)


@pytest.mark.asyncio
async def test_upload_copies_mutable_bytearray_before_any_authorization_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """型境界を破る mutable Provider 入力でも、解析時と PUT 時の原 bytes を一致させる。"""
    session = await prepare(monkeypatch, "upload")
    mutable = bytearray(UPLOAD_FILES[0].data)
    original_bytes = bytes(mutable)
    # 故意に bytes 型契約を破る局部呼出側を模倣し、production の bytes 固定を検証する。
    files = (replace(UPLOAD_FILES[0], data=cast(bytes, mutable)), UPLOAD_FILES[1])
    expected = session.service().preview_upload(UPLOAD_FILES)

    def change_bytes(point: str) -> None:
        """最初の await 後に同じ buffer を変更し、list 要素の差替えで済ませない。"""
        if point == "organization:1":
            mutable[:] = b"# Mutated original buffer\n"

    session.on_step = change_bytes
    stored = await session.service().save_upload(access=session.access, files=files)
    assert bytes(mutable) != original_bytes
    assert stored.source_hash == expected.normalized_package["source"]["content_hash"]
    assert stored.preview.normalized_package == expected.normalized_package
    first_key, first_bytes, _ = session.storage.attempts[0]
    assert first_bytes == original_bytes
    assert await session.storage.get(first_key) == original_bytes
    assert session.sources[0].source_snapshot_json[0] == {
        "path": UPLOAD_FILES[0].path,
        "content": original_bytes.decode("utf-8"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_import_freezes_original_file_fields_before_first_await(
    monkeypatch: pytest.MonkeyPatch,
    kind: ImportKind,
) -> None:
    """tuple 内の原 file object 自体を変え、単に入力 list を固定するだけでは不十分と示す。"""
    session = await prepare(monkeypatch, kind)
    # 共用定数を変更せず、この呼出側だけが保持する frozen object を故意に書き換える。
    inline = tuple(replace(file) for file in INLINE_FILES)
    upload = tuple(replace(file) for file in UPLOAD_FILES)
    service = session.service()
    expected = service.preview_inline(inline)

    def change_original_file(point: str) -> None:
        """parser 完了後の最初の資格待機で、同じ file alias の中身だけを変更する。"""
        if point == "organization:1":
            if kind == "inline":
                object.__setattr__(inline[0], "content", "# Changed original file\n")
            else:
                object.__setattr__(upload[1], "path", "references/changed.md")

    session.on_step = change_original_file
    if kind == "inline":
        stored = await service.save_inline(access=session.access, files=inline)
        assert inline[0].content != INLINE_FILES[0].content
    else:
        stored = await service.save_upload(access=session.access, files=upload)
        assert upload[1].path != UPLOAD_FILES[1].path
    assert stored.source_hash == expected.normalized_package["source"]["content_hash"]
    assert stored.preview == expected
    assert session.sources[0].source_snapshot_json == [
        {"path": file.path, "content": file.content} for file in INLINE_FILES
    ]
    if kind == "upload":
        prefix = session.sources[0].storage_uri.removeprefix("s3://skillmind/")
        assert [key for key, _, _ in session.storage.attempts] == [
            f"{prefix}/{file.path}" for file in UPLOAD_FILES
        ]
        for file in UPLOAD_FILES:
            assert await session.storage.get(f"{prefix}/{file.path}") == file.data
