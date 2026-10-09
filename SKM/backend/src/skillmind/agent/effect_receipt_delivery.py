"""原 Effect 回执を保持し、モデルへの大きい本文交付だけを Run file に移す。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from skillmind.agent.domain import RunContext, RunWorkspace
from skillmind.agent.materialization_storage import MaterializationError, write_workspace_file
from skillmind.core.hashing import canonical_json, sha256_hex
from skillmind.core.timing import safe_observation
from skillmind.effects.continuation import validated_effect_result

INLINE_RECEIPT_BYTES = 8_192


def _file_path(receipt: Mapping[str, Any], checksum: str) -> str:
    """原 Effect と全回执 hash を固定し、別操作や現在の遠端値で上書きしない。"""
    return (
        f"workspace/resources/effects/{receipt['effect_execution_id']}/"
        f"{checksum.removeprefix('sha256:')}/receipt.json"
    )


async def materialize_receipt_files(
    workspace: RunWorkspace,
    receipts: Sequence[Mapping[str, Any]],
    allowed_capabilities: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    """検証済みの原値だけを物化し、読取権や file 保存がない場合は本文交付を保つ。"""
    if "workspace.read/v1" not in allowed_capabilities:
        return ()
    files: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    for original in receipts:
        receipt = validated_effect_result(original)
        raw = canonical_json(receipt).encode("utf-8")
        identity = receipt["effect_execution_id"]
        checksum = "sha256:" + sha256_hex(raw)
        if identity in hashes:
            if hashes[identity] != checksum:
                raise ValueError("Original effect receipts disagree")
            continue
        hashes[identity] = checksum
        if len(raw) <= INLINE_RECEIPT_BYTES:
            continue
        path = _file_path(receipt, checksum)
        try:
            await asyncio.to_thread(
                write_workspace_file, workspace.root, path, raw,
                expected_hash="absent", reuse_identical=True,
            )
        except (MaterializationError, OSError):
            # 外部操作は既に確定済み。付加的な交付 file の失敗で業務結果を失敗にしない。
            # 存在しない/改変済み file を案内せず、原回执の完全な本文を返す。
            continue
        files[identity] = {
            "effect_execution_id": identity,
            "file": {"path": path, "content_hash": checksum, "size_bytes": len(raw)},
        }
    return tuple(files.values())


def project_receipt(
    original: Mapping[str, Any], files: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """モデル用の参照は原回执全体へ結び、APPLIED を業務成功や現在状態へ変換しない。"""
    receipt = validated_effect_result(original)
    selected = next(
        (item for item in files if item["effect_execution_id"] == receipt["effect_execution_id"]),
        None,
    )
    if selected is None:
        return receipt
    raw = canonical_json(receipt).encode("utf-8")
    checksum = "sha256:" + sha256_hex(raw)
    file = selected["file"]
    if file != {
        "path": _file_path(receipt, checksum),
        "content_hash": checksum,
        "size_bytes": len(raw),
    }:
        raise ValueError("Receipt file does not identify the original bytes")
    projected = {
        key: receipt[key] for key in (
            "effect_execution_id", "proposal_ref", "capability_version", "status",
            "before_ref", "after_ref", "after_content_hash",
        ) if key in receipt
    }
    projected.update(
        delivery_format="effect-receipt-file/v1", file=dict(file), after_pointer="/after",
    )
    keys = list(receipt["after"])
    if len(canonical_json(keys).encode("utf-8")) <= 1_024:
        projected["after_keys"] = keys
    # 通常の短い検証結果は一緒に渡す。任意 Provider の大きい検証正文も全文は file に残す。
    if len(canonical_json(receipt["verification"]).encode("utf-8")) <= 1_024:
        projected["verification"] = receipt["verification"]
    else:
        projected["verification_pointer"] = "/verification"
    return projected


async def deliver_receipt(context: RunContext, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """即時回执も保存済み原値から交付し、外部 Provider や再実行を呼ばない。"""
    if not any(tool.capability == "workspace.read/v1" for tool in context.tools):
        return validated_effect_result(receipt)
    files = await materialize_receipt_files(
        context.workspace, (receipt,), context.permission_snapshot.get("allowed_capabilities", ()),
    )
    projected = project_receipt(receipt, files)
    safe_observation(
        "run.performance.receipt_delivery", run_id=context.run_id,
        run_attempt_id=context.run_attempt_id, effect_execution_id=receipt["effect_execution_id"],
        receipt_delivery="FILE" if "file" in projected else "INLINE",
        source_bytes=len(canonical_json(receipt).encode("utf-8")),
        output_bytes=len(canonical_json(projected).encode("utf-8")),
    )
    return projected


def checkpoint_receipts(checkpoint: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """既存の内部 checkpoint から、現在と過去の原回执だけを集める。"""
    receipts: list[Mapping[str, Any]] = []
    latest = checkpoint.get("effect_result")
    if isinstance(latest, Mapping):
        receipts.append(latest)
    receipts.extend(checkpoint.get("effect_receipts", ()))
    return receipts


def model_checkpoint(
    brief: Mapping[str, Any], files: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """監査 Brief は変更せず、モデル用の checkpoint に file 参照と一回の原操作を載せる。"""
    checkpoint = dict(brief["checkpoint"])
    latest = checkpoint.get("effect_result")
    latest_id = None
    if isinstance(latest, Mapping):
        checkpoint["effect_result"] = project_receipt(latest, files)
        latest_id = latest["effect_execution_id"]
    if "effect_receipts" in checkpoint:
        seen = {latest_id} if latest_id else set()
        projected = []
        for receipt in checkpoint["effect_receipts"]:
            identity = receipt["effect_execution_id"]
            if identity in seen:
                continue
            projected.append(project_receipt(receipt, files))
            seen.add(identity)
        checkpoint["effect_receipts"] = projected
    return checkpoint
