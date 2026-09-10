"""主/子 Result と checkpoint が共通 Artifact byte 核験を省略しないことを検証する。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from projectmind.agent.result_validation import (
    PostgresArtifactLookup,
    ResultValidationError,
    ResultValidator,
)
from projectmind.artifacts.domain import ArtifactIntegrityError
from projectmind.runs.repository import RunRepository
from tests.agent.test_result_references import validate_outcome
from tests.agent.test_result_validation import MemoryEvidenceLookup, _outcome


class ArtifactIndex:
    """一つの Run にだけ検証済み byte がある明示的な lookup port。"""

    def __init__(self, run_id: UUID, refs: frozenset[str]) -> None:
        """同 Project を所有確認の代わりにせず、正確な Run を記録する。"""

        self.run_id = run_id
        self.refs = refs
        self.calls: list[tuple[UUID, frozenset[str]]] = []

    async def verified_refs(self, run_id: UUID, refs: frozenset[str]) -> frozenset[str]:
        """原 Run 以外、未保存 ref は検証集合に含めない。"""

        self.calls.append((run_id, refs))
        return self.refs & refs if run_id == self.run_id else frozenset()


def artifact_outcome(*, top: bool, nested: bool) -> dict[str, Any]:
    """全て正式 Schema で許可された位置を使い、業務 JSON とプラットフォーム参照を分ける。"""

    value = _outcome(evidence_refs=[])
    value["artifact_refs"] = ["art_original"] if top else []
    if nested:
        value["deliverables"] = [
            {
                "key": "report",
                "kind": "artifact",
                "title": "Report",
                "artifact_ref": "art_original",
            },
        ]
    return value


@pytest.mark.parametrize("top,nested", [(True, False), (False, True), (True, True)])
async def test_verified_artifacts_are_saved_with_exact_v2_scope(top: bool, nested: bool) -> None:
    """包絡内の重複位置を一度だけ照会し、保存済み byte の検証範囲を明記する。"""

    run_id = uuid4()
    index = ArtifactIndex(run_id, frozenset({"art_original"}))
    result = await validate_outcome(
        ResultValidator(MemoryEvidenceLookup(frozenset()), artifact_lookup=index),
        run_id,
        artifact_outcome(top=top, nested=nested),
    )
    assert index.calls == [(run_id, frozenset({"art_original"}))]
    assert result.artifact_refs == {"art_original"}
    assert result.validation["artifact_count"] == 1
    assert result.validation["artifact_refs_valid"] is True
    assert (
        result.validation["reference_checks"]["version"] == "projectmind.result-reference-checks/v2"
    )
    assert result.validation["reference_checks"]["artifacts"] == "RUN_OWNERSHIP_AND_CONTENT"


@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("nested", [False, True])
async def test_missing_or_other_run_artifact_is_rejected(foreign: bool, nested: bool) -> None:
    """同じ art_ の形式や他 Run の成功回执は、その Run の交付根拠にならない。"""

    run_id = uuid4()
    index = ArtifactIndex(
        uuid4() if foreign else run_id, frozenset({"art_original"}) if foreign else frozenset()
    )
    with pytest.raises(ResultValidationError) as captured:
        await validate_outcome(
            ResultValidator(MemoryEvidenceLookup(frozenset()), artifact_lookup=index),
            run_id,
            artifact_outcome(top=not nested, nested=nested),
        )
    assert captured.value.code == "artifact_reference_invalid"
    assert "art_original" not in captured.value.message


async def test_corrupt_bytes_fail_without_exposing_repository_details() -> None:
    """保存 hash/実 byte の矛盾を成功候補にせず、内部メッセージは model に渡さない。"""

    index = AsyncMock(spec=ArtifactIndex)
    index.verified_refs.side_effect = ArtifactIntegrityError("synthetic-private-detail")
    with pytest.raises(ResultValidationError) as captured:
        await validate_outcome(
            ResultValidator(MemoryEvidenceLookup(frozenset()), artifact_lookup=index),
            uuid4(),
            artifact_outcome(top=True, nested=True),
        )
    assert captured.value.code == "artifact_reference_invalid"
    assert "synthetic-private-detail" not in captured.value.message


async def test_original_candidate_cannot_change_while_snapshot_is_read() -> None:
    """共有 ResultValidator が Artifact の待機中も元候補を固定して保存する。"""

    run_id = uuid4()
    original = artifact_outcome(top=True, nested=True)
    index = AsyncMock(spec=ArtifactIndex)

    async def verify(target: UUID, refs: frozenset[str]) -> frozenset[str]:
        """lookup 中に producer が参照集合と本文を変更する競争を再現する。"""

        assert target == run_id and refs == {"art_original"}
        original["artifact_refs"].append("art_injected")
        original["deliverables"][0]["artifact_ref"] = "art_injected"
        return refs

    index.verified_refs.side_effect = verify
    result = await validate_outcome(
        ResultValidator(MemoryEvidenceLookup(frozenset()), artifact_lookup=index),
        run_id,
        original,
    )
    assert result.artifact_refs == {"art_original"}
    assert result.data["artifact_refs"] == ["art_original"]
    assert result.data["deliverables"][0]["artifact_ref"] == "art_original"


@pytest.mark.parametrize("valid", [False, True])
async def test_checkpoint_uses_same_content_verifier(
    monkeypatch: pytest.MonkeyPatch,
    valid: bool,
) -> None:
    """次 Segment の checkpoint でも同じ ref+Run の byte 核験を要求する。"""

    session = MagicMock(spec=AsyncSession)
    repository = RunRepository(session)
    lookup = AsyncMock(return_value=frozenset({"art_original"}) if valid else frozenset())
    monkeypatch.setattr("projectmind.artifacts.repository.ArtifactRepository.verified_refs", lookup)
    run_id = uuid4()
    checkpoint = {
        "evidence_refs": [],
        "artifact_refs": ["art_original"],
        "change_proposal_refs": [],
    }
    if valid:
        await repository._validate_checkpoint_refs(run_id, checkpoint)
    else:
        with pytest.raises(ValueError, match="unavailable Artifact"):
            await repository._validate_checkpoint_refs(run_id, checkpoint)
    lookup.assert_awaited_once_with(run_id, frozenset({"art_original"}))


async def test_postgres_lookup_uses_shared_repository_with_own_read_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """production の主/子装配も download と同じ repository に接続される。"""

    session = MagicMock(spec=AsyncSession)
    session.__aenter__.return_value = session
    factory = MagicMock(return_value=session)
    lookup = AsyncMock(return_value=frozenset({"art_original"}))
    monkeypatch.setattr("projectmind.artifacts.repository.ArtifactRepository.verified_refs", lookup)
    adapter = PostgresArtifactLookup(factory)
    run_id = uuid4()
    assert await adapter.verified_refs(run_id, frozenset()) == frozenset()
    factory.assert_not_called()
    assert await adapter.verified_refs(run_id, frozenset({"art_original"})) == {"art_original"}
    lookup.assert_awaited_once_with(run_id, frozenset({"art_original"}))
    session.__aexit__.assert_awaited_once()
