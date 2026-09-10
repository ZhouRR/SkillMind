"""Artifact の不変 byte、公開 metadata、限額を外部 I/O 無しで確認する。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from projectmind.artifacts.domain import (
    MAX_ARTIFACT_BYTES,
    ArtifactContent,
    ArtifactDraft,
    ArtifactIntegrityError,
    ArtifactMetadata,
)


def metadata(content: bytes = b"original") -> ArtifactMetadata:
    """原 byte に対応する完全な公開 fixture を作る。"""

    draft = ArtifactDraft(path="output/report.txt", content=content)
    return ArtifactMetadata(
        artifact_ref="art_original", project_id=uuid4(), run_id=uuid4(), tool_call_id=uuid4(),
        evidence_ref="ev_original", path=draft.path, size_bytes=len(content),
        mime_type=draft.mime_type, checksum=draft.checksum, created_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("content", [b"", b"a" * MAX_ARTIFACT_BYTES, "日本語\n内容".encode()])
def test_draft_and_content_verify_exact_utf8_bytes_without_printing_body(content: bytes) -> None:
    """空 file と限界 byte を認め、private content を repr へ含めない。"""

    draft = ArtifactDraft(path="output/report.txt", content=content)
    original = ArtifactContent(metadata(content), content)
    assert original.content is content and draft.content is content
    assert "content=" not in repr(draft) and "content=" not in repr(original)
    with pytest.raises(FrozenInstanceError):
        draft.content = b"replacement"  # type: ignore[misc]


@pytest.mark.parametrize("path", [
    "", "output", "output/", "/output/report", "workspace/report", "output//report",
    "output/./report", "output/../report", "output/a/..", "output/a/.", "output/a\\b",
    "output/report\n", "output/report\x00", "output/report\x7f", "output/\u202ereport",
    "output/" + "a" * 4090, "output/\ud800report",
])
def test_draft_rejects_noncanonical_or_nonoutput_path(path: str) -> None:
    """論理 path を filesystem や URL の解釈で補正せず拒否する。"""

    with pytest.raises(ArtifactIntegrityError, match=r"^Artifact does not match"):
        ArtifactDraft(path=path, content=b"original")


@pytest.mark.parametrize("path", [
    "output/中文 (1).txt", "output/a;b\".txt", "output/" + "a" * 4089,
])
def test_draft_preserves_printable_unicode_and_exact_path_limit(path: str) -> None:
    """安全な表示文字を勝手に改名せず、HTTP header は別の共有変換へ委ねる。"""

    assert ArtifactDraft(path=path, content=b"").path == path


@pytest.mark.parametrize("content", [
    b"\xff", b"a" * (MAX_ARTIFACT_BYTES + 1), bytearray(b"a"), "a",
])
def test_draft_rejects_non_utf8_oversized_or_mutable_content(content: Any) -> None:
    """bytearray や str を後で読み換えず、受信した immutable bytes だけを使う。"""

    with pytest.raises(ArtifactIntegrityError):
        ArtifactDraft(path="output/file", content=content)


@pytest.mark.parametrize("changes", [
    {"artifact_ref": "ev_wrong"}, {"artifact_ref": "art_"}, {"artifact_ref": "art_" + "a" * 61},
    {"artifact_ref": "art_bad\n"}, {"evidence_ref": "art_wrong"},
    {"project_id": UUID(int=0)}, {"run_id": "not-a-uuid"}, {"tool_call_id": None},
    {"size_bytes": True}, {"size_bytes": -1}, {"size_bytes": MAX_ARTIFACT_BYTES + 1},
    {"mime_type": "text/html"}, {"mime_type": "text/plain; charset=utf-8"},
    {"checksum": "sha256:" + "A" * 64}, {"checksum": "bad"},
    {"created_at": datetime(2026, 1, 1)}, {"created_at": "2026-01-01T00:00:00Z"},
])
def test_metadata_rejects_invalid_saved_values(changes: dict[str, Any]) -> None:
    """DB 値を正規化して損傷を隠さず、identity/size/hash/time の不一致を拒否する。"""

    with pytest.raises(ArtifactIntegrityError):
        replace(metadata(), **changes)


@pytest.mark.parametrize("content", [b"", b"different", b"originaL", b"\xff"])
def test_content_never_repairs_size_hash_or_utf8(content: bytes) -> None:
    """同長でも異なる byte を受け入れず、原 metadata の hash を更新しない。"""

    original = metadata()
    checksum = original.checksum
    with pytest.raises(ArtifactIntegrityError):
        ArtifactContent(original, content)
    assert original.checksum == checksum


def test_draft_rejects_mime_outside_current_utf8_producer() -> None:
    """任意 HTML/binary producer を MIME の自己申告だけで追加しない。"""

    with pytest.raises(ArtifactIntegrityError):
        ArtifactDraft(path="output/file", content=b"plain", mime_type="text/html")
