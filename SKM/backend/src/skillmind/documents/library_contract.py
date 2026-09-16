"""文書庫の保存専用 binding 契約を、解釈・就緒度・Run 作成で共有する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

DOCUMENT_WRITE_CAPABILITY = "document.write/v1"
DOCUMENT_LIBRARY_CAPABILITIES_MESSAGE = (
    "A document library output resource must declare only document.write/v1. "
    "Use a separate frozen input resource for document reads, or workspace.read/v1 "
    "for retained files in the same Run workspace. "
    "Reinterpret the Skill before starting a new task."
)


def supports_document_library_capabilities(capabilities: Sequence[str]) -> bool:
    """保存 binding が満たせる宣言だけを受理し、読取権限を暗黙に足さない。"""

    return tuple(capabilities) == (DOCUMENT_WRITE_CAPABILITY,)


def has_invalid_document_library_capabilities(requirement: Mapping[str, Any]) -> bool:
    """既知の保存能力との混在を拒否し、未知の業務能力まで発行禁止にしない。"""

    capabilities = requirement.get("capabilities", [])
    return (
        requirement.get("kind") == "document"
        and requirement.get("access") == "write"
        and DOCUMENT_WRITE_CAPABILITY in capabilities
        and not supports_document_library_capabilities(capabilities)
    )
