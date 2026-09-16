"""既存監査から明示的に証明できる回復と反復だけを数え、業務状態は変更しない。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from skillmind.db.models import ToolCall


class ExecutionMetrics(TypedDict):
    """不明値をゼロと混同しない、Run 詳細の非業務投影。"""

    structural_errors: int
    repeated_query_errors: int
    correction_attempts: int
    corrected_reads: int
    unresolved_tool_errors: int
    duplicate_reads: int
    schema_reads: int
    schema_cache_hits: int
    schema_refreshes: int
    manual_responses: int
    query_error_interventions: int | None


def execution_metrics(
    calls: Sequence[ToolCall], *, modern: bool, manual_responses: int
) -> ExecutionMetrics | None:
    """成功した任意 query を失敗へ関連付けず、Provider が検証した訂正鎖だけを集計する。"""
    if not modern:
        return None
    try:
        identities = {str(c.id): c.arguments_summary.get("database", {}) for c in calls}
        failures = {str(c.id): c for c in calls if c.status == "FAILED"}
        structural = {
            key
            for key, c in failures.items()
            if (c.error_json or {}).get("database", {}).get("reason_code")
            in {"undefined_column", "parameter_type_mismatch"}
        }
        inspections = {
            str(c.id): c
            for c in calls
            if c.capability_version == "database.describe/v1" and c.status == "SUCCEEDED"
        }
        recovered: set[str] = set()
        corrections = 0
        for c in calls:
            identity = identities[str(c.id)]
            root = identity.get("recovery_from")
            if c.capability_version != "database.read/v2" or root not in structural:
                continue
            corrections += 1
            if c.status != "SUCCEEDED":
                continue
            schema_ref = identity.get("schema_evidence_ref")
            if any(
                identities[key].get("recovery_from") == root
                and schema_ref in (schema.result_json or {}).get("evidence_refs", [])
                and schema.integration_id == c.integration_id
                and identities[key].get("binding_id") == identity.get("binding_id")
                and identities[key].get("table") == identity.get("table")
                for key, schema in inspections.items()
            ):
                recovered.add(root)

        def duplicate_count(selected: Sequence[ToolCall]) -> int:
            """同一 binding/table/条件 hash を数えるだけで、読取を抑止しない。"""
            counts = Counter(
                (
                    str(c.integration_id),
                    identities[str(c.id)].get("binding_id"),
                    identities[str(c.id)].get("table"),
                    identities[str(c.id)].get("query_checksum"),
                )
                for c in selected
                if identities[str(c.id)].get("query_checksum")
            )
            return sum(max(0, count - 1) for count in counts.values())

        reads = [c for c in calls if c.capability_version == "database.read/v2"]
        return ExecutionMetrics(
            structural_errors=len(structural),
            repeated_query_errors=duplicate_count([c for c in reads if str(c.id) in structural]),
            correction_attempts=corrections,
            corrected_reads=len(recovered),
            unresolved_tool_errors=len(failures.keys() - recovered),
            duplicate_reads=duplicate_count(reads),
            schema_reads=sum(
                (c.result_json or {}).get("cached") is False for c in inspections.values()
            )
            + sum(
                "table_schema" in (c.result_json or {}) for c in reads if c.status == "SUCCEEDED"
            ),
            schema_cache_hits=sum(
                (c.result_json or {}).get("cached") is True for c in inspections.values()
            ),
            schema_refreshes=sum(
                bool(identities[key].get("refresh") or identities[key].get("recovery_from"))
                for key in inspections
            ),
            manual_responses=manual_responses,
            # clarification 本文から因果を推定しない。明示関連のない既存回答は不明のまま。
            query_error_interventions=None,
        )
    except (TypeError, KeyError, AttributeError):
        # 観測欠損で結果閲覧を止めたり、ゼロ成功へ塗り替えたりしない。
        return None
