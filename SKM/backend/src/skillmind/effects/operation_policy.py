"""Provider 操作の語彙と原文実行の最低リスクを共有する。"""
from __future__ import annotations

from skillmind.core.hashing import canonical_json, sha256_hex

# 各 Provider の提案 validator と一致する操作名。資格情報や接続範囲を生成しない。
WRITE_OPERATIONS: dict[str, tuple[str, ...]] = {
    "database.write/v1": ("INSERT", "UPDATE"),
    "document.write/v1": ("CREATE",),
    "repository.write/v1": ("commit",),
    "issue.update/v1": ("update",),
}


def operation_authorization_key(resource: str, capability: str, operation: str) -> str:
    """既存 Proposal 保存欄へ、model の意図でなく実際の操作を識別する key を渡す。"""

    return "op-" + sha256_hex(canonical_json([resource, capability, operation]))


def operation_risk(capability: str) -> str:
    """原文実行の最低 risk は platform が決め、model に低減させない。"""

    return "MEDIUM" if capability in {"database.write/v1", "document.write/v1"} else "HIGH"


