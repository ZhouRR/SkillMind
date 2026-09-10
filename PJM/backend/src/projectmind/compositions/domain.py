"""SkillComposition(module)の read model、command、domain error を定義する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class ModuleNotFoundError(Exception):
    """Project 内に module が存在しない、または越権参照であることを表す。"""


class ModuleSkillInvalidError(Exception):
    """束縛対象が Project 内の PUBLISHED SkillVersion でないことを表す。"""


class ModuleValidationError(Exception):
    """名称や束縛集合が module の入力規則を満たさないことを表す。"""


@dataclass(frozen=True, slots=True)
class ModuleSkillBinding:
    """保存時に有効だった精確版を表示し、後日の停用・廃止で設定を補修しない。"""

    skill_version_id: UUID
    skill_id: UUID
    skill_key: str
    skill_name: str
    version: str
    sort_order: int


@dataclass(frozen=True, slots=True)
class StoredModule:
    """Project で有効な module の read model。"""

    module_id: UUID
    project_id: UUID
    name: str
    description: str
    skills: tuple[ModuleSkillBinding, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CreateModuleCommand:
    """Module を作成し、現在 Project へ有効化する command。"""

    project_id: UUID
    created_by: UUID
    name: str
    description: str
    skill_version_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class UpdateModuleCommand:
    """Module の名称/説明/束縛集合を置き換える command。"""

    project_id: UUID
    module_id: UUID
    name: str
    description: str
    skill_version_ids: tuple[UUID, ...]
