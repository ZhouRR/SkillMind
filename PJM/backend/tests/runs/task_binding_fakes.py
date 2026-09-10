"""実共有 guard の SQL を評価し、Run/調度で可用性の stub 成功を使わない。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Select, select

from projectmind.db.models import Project, ProjectSkillVersion, Skill, SkillSource, SkillVersion


class TaskBindingRows:
    """一つの精確版・組織・Project 関係だけを持つ局部 DB seam。"""

    def __init__(self, project: Project, version: SkillVersion) -> None:
        """公開済み版の所有関係と明示有効化を、互いに独立した実 ORM 行で作る。"""

        self.project = project
        self.skill = Skill(id=uuid4(), organization_id=project.organization_id)
        self.source = SkillSource(id=uuid4(), organization_id=project.organization_id)
        self.version = version
        self.version.skill_id = self.skill.id
        self.version.skill_source_id = self.source.id
        self.version_present = True
        self.binding: ProjectSkillVersion | None = ProjectSkillVersion(
            id=uuid4(),
            project_id=project.id,
            skill_version_id=version.id,
            enabled_at=datetime.now(UTC),
            disabled_at=None,
        )

    def scalar(self, statement: Select[Any]) -> SkillVersion | ProjectSkillVersion | None:
        """完全な predicate/結合/SHARE/refresh を照合し、異組織や別 Project を拾わない。"""

        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        assert statement.get_execution_options()["populate_existing"] is True
        if entity is SkillVersion:
            assert set(params) == {
                "id_1",
                "status_1",
                "organization_id_1",
                "organization_id_2",
                "id_2",
            }
            expected = (
                select(SkillVersion)
                .join(SkillSource, SkillSource.id == SkillVersion.skill_source_id)
                .join(Skill, Skill.id == SkillVersion.skill_id)
                .join(Project, Project.organization_id == Skill.organization_id)
                .where(
                    SkillVersion.id == params["id_1"],
                    SkillVersion.status == "PUBLISHED",
                    Skill.organization_id == params["organization_id_1"],
                    SkillSource.organization_id == params["organization_id_2"],
                    Project.id == params["id_2"],
                )
                .with_for_update(read=True, of=SkillVersion)
            )
            assert statement.compare(expected)
            assert params["organization_id_1"] == params["organization_id_2"]
            return (
                self.version
                if (
                    self.version_present
                    and self.version.id == params["id_1"]
                    and self.version.status == "PUBLISHED"
                    and self.version.skill_id == self.skill.id
                    and self.version.skill_source_id == self.source.id
                    and self.skill.organization_id == params["organization_id_1"]
                    and self.source.organization_id == params["organization_id_2"]
                    and self.project.organization_id == self.skill.organization_id
                    and self.project.id == params["id_2"]
                )
                else None
            )
        assert entity is ProjectSkillVersion
        assert set(params) == {"project_id_1", "skill_version_id_1"}
        expected_binding = (
            select(ProjectSkillVersion)
            .where(
                ProjectSkillVersion.project_id == params["project_id_1"],
                ProjectSkillVersion.skill_version_id == params["skill_version_id_1"],
            )
            .with_for_update(read=True)
        )
        assert statement.compare(expected_binding)
        return (
            self.binding
            if (
                self.binding is not None
                and self.binding.project_id == params["project_id_1"]
                and self.binding.skill_version_id == params["skill_version_id_1"]
            )
            else None
        )
