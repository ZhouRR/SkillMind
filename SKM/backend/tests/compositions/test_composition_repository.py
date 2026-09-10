"""共有精確版 gate と読取組織条件を実 repository の SQL で検証する。"""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.compositions.domain import ModuleSkillInvalidError
from skillmind.compositions.repository import CompositionRepository
from tests.compositions.composition_authorization_harness import CompositionSession
from tests.skills.test_skill_publication_authorization import NOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "unknown",
        "deprecated",
        "no-binding",
        "disabled",
        "foreign-source",
        "foreign-skill",
    ],
)
async def test_current_version_binding_refuses_with_one_static_reason(case: str) -> None:
    """他組織の有効状態を理由文字列で漏らさず、本番共有 gate を実 SQL で消費する。"""
    session = CompositionSession("update")
    target = session.version.id
    if case == "unknown":
        target = uuid4()
    elif case == "deprecated":
        session.version.status = "DEPRECATED"
    elif case == "no-binding":
        session.bindings.clear()
    elif case == "disabled":
        session.bindings[0].disabled_at = NOW
    elif case == "foreign-source":
        session.source.organization_id = uuid4()
    elif case == "foreign-skill":
        session.skill.organization_id = uuid4()
    repository = CompositionRepository(cast(AsyncSession, session))
    before = session.frozen_values()

    async def check() -> None:
        """親 transaction の資格 callback を渡し、業務 version gate 自体は差し替えない。"""
        await repository._require_published_versions(
            organization_id=session.organization_id,
            project_id=session.project.id,
            skill_version_ids=(target,),
            authorize=lambda: NOW,
        )

    if case == "valid":
        await check()
        assert session.timeline == ["version:1", "binding:1"]
    else:
        with pytest.raises(
            ModuleSkillInvalidError,
            match=r"^Skill versions are not available for this project$",
        ):
            await check()
    assert session.frozen_values() == before and session.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "foreign-parent",
        "foreign-project",
        "foreign-skill",
        "foreign-source",
        "deprecated",
        "disabled",
    ],
)
async def test_listing_scopes_parent_and_project_and_hides_foreign_names(case: str) -> None:
    """旧設定の廃止/停止版は表示できるが、別組織の名前や共有父行を投影しない。"""
    session = CompositionSession("update")
    assert session.composition is not None
    if case == "foreign-parent":
        session.composition.organization_id = uuid4()
    elif case == "foreign-project":
        session.project.organization_id = uuid4()
    elif case == "foreign-skill":
        session.skill.organization_id = uuid4()
    elif case == "foreign-source":
        session.source.organization_id = uuid4()
    elif case == "deprecated":
        session.version.status = "DEPRECATED"
    elif case == "disabled":
        session.bindings[0].disabled_at = NOW
    before = session.frozen_values()
    records = await session.service().list_modules(project_id=session.project.id)
    if case in {"foreign-parent", "foreign-project"}:
        assert records == []
    else:
        assert len(records) == 1
        record = records[0]
        assert record.module_id == session.module_id and record.project_id == session.project.id
        assert len(record.skills) == 1
        expected = "unknown" if case in {"foreign-skill", "foreign-source"} else session.skill.key
        assert record.skills[0].skill_key == expected
    assert session.frozen_values() == before
    assert session.transactions == 0 and session.mutations == []
    assert not any("FOR UPDATE" in query or "FOR SHARE" in query for query in session.queries)
