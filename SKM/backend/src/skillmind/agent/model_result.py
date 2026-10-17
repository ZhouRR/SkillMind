"""モデルは業務内容と短参照だけを返し、保存包絡の参照と監査項目は原記録から組む。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skillmind.agent.domain import RunContext
from skillmind.agent.outcome import OUTCOME_ENVELOPE_VERSION
from skillmind.core.hashing import sha256_hex
from skillmind.db.models import ChangeProposal, EffectExecution, Evidence, ToolCall

MODEL_RESULT_VERSION = "skillmind.model-outcome/v1"
MODEL_RESULT_INSTRUCTIONS = (
    "Final result: write business conclusions, findings, deliverables and limitations. "
    "Tool result_references provide stable E... evidence and A... artifact labels, "
    "with JSON Pointer locations in that same original result. Use sources for each "
    "finding and artifact for a published deliverable. Select only sources that support "
    "that finding. Do not copy long platform IDs, repeat global reference lists, or "
    "reconstruct effects and their statuses. The platform assembles audit fields from "
    "saved records and validates the complete result. Short labels are references, "
    "not new permissions. Skill-required business files and IDs remain unchanged."
)
_REFERENCE = re.compile(r"^(ev|art)_[a-f0-9]{32}$")


def compile_model_result_schema(stored: Mapping[str, Any]) -> dict[str, Any]:
    """保存 Schema の業務部分を保ち、モデルが再生成する監査項目だけを除く。"""
    schema = deepcopy(dict(stored))
    schema.pop("$id", None)
    schema["title"] = MODEL_RESULT_VERSION
    # 非空の業務内容は組立後の原 Schema で検査する。SDK に root 全体を JSON 文字列へ
    # 包み直させず、業務の骨格を native object として渡す。
    schema.pop("anyOf", None)
    schema["properties"]["confidence"] = {
        "type": ["number", "null"], "minimum": 0, "maximum": 1,
    }
    removed = {"outcome_version", "evidence_refs", "artifact_refs",
               "change_proposal_refs", "effects"}
    for key in removed:
        schema["properties"].pop(key)
    schema["required"] = [key for key in schema["required"] if key not in removed]
    definitions = schema["$defs"]
    finding = definitions["finding"]
    finding["properties"]["sources"] = {
        "type": "array", "maxItems": 500, "uniqueItems": True,
        "items": {"type": "string", "pattern": "^E[0-9a-f]{12}$"},
        "description": "Select evidence labels from tool result_references for this finding.",
    }
    finding["properties"].pop("evidence_refs")
    finding["required"] = ["sources" if key == "evidence_refs" else key
                            for key in finding["required"]]
    deliverable = definitions["deliverable"]["properties"]
    deliverable.pop("artifact_ref")
    deliverable["artifact"] = {
        "type": "string", "pattern": "^A[0-9a-f]{12}$",
        "description": "Select the published artifact label from tool result_references.",
    }
    for key in ("evidenceRef", "artifactRef", "changeProposalRef", "effectSummary"):
        definitions.pop(key)
    return schema


def model_result_schema(context: RunContext) -> Mapping[str, Any]:
    """SDK と続行は作成時に凍結したモデル契約を使い、保存契約は変更しない。"""
    if "model_output_schema_json" not in context.task_snapshot:
        return context.result_schema
    schema = context.task_snapshot.get("model_output_schema_json")
    if not isinstance(schema, Mapping):
        raise ValueError("Run model result schema is invalid")
    return schema


def reference_label(reference: str) -> str:
    """原 ID から安定した短参照を導く。衝突は組立時に拒否し、類似一致は行わない。"""
    return ("A" if reference.startswith("art_") else "E") + sha256_hex(reference)[:12]


def project_result_references(value: Mapping[str, Any]) -> dict[str, Any]:
    """SDK 交付だけに位置付き短参照を添え、監査済み本文や原 ID は変更しない。"""
    result = deepcopy(dict(value))
    labels: dict[str, str] = {}

    def visit(item: Any, path: str = "") -> None:
        """型付きの参照位置だけを拾い、正文中の文字列を参照と推定しない。"""
        if isinstance(item, Mapping):
            for key, child in item.items():
                pointer = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
                if key in {"evidence_ref", "artifact_ref", "before_ref", "after_ref"}:
                    add(child, pointer)
                elif key in {"evidence_refs", "artifact_refs"} and isinstance(child, list):
                    for index, ref in enumerate(child):
                        add(ref, f"{pointer}/{index}")
                else:
                    visit(child, pointer)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}/{index}")

    def add(ref: Any, path: str) -> None:
        """入力値や本文を複写せず、原値へ戻れる位置だけを渡す。"""
        if isinstance(ref, str) and _REFERENCE.fullmatch(ref):
            label = reference_label(ref)
            labels[label] = path

    visit(value)
    if labels:
        result["result_references"] = labels
    return result


@dataclass(frozen=True, slots=True)
class ResultAssemblySnapshot:
    """同 Run の参照集合と、保存済み提案・効果の表示事実。本文や資格情報は含まない。"""

    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    proposal_refs: tuple[str, ...] = ()
    effects: tuple[Mapping[str, Any], ...] = ()


class ResultAssemblyLookup(Protocol):
    """組立は読取のみ。外部 Provider や再実行に依存しない。"""

    async def load(self, context: RunContext, session_id: UUID | None) -> ResultAssemblySnapshot:
        """子は自身の SDK Session の観測だけを返し、親の監査を公開しない。"""
        ...


class PostgresResultAssemblyLookup:
    """既存 Evidence/Proposal/Effect を読取投影し、新しい永続テーブルを持たない。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """正本の参照だけを読む session factory を保持する。"""
        self._session_factory = session_factory

    async def load(self, context: RunContext, session_id: UUID | None) -> ResultAssemblySnapshot:
        """全提案を対象にし、モデルの省略で未完了操作が検証から落ちることを防ぐ。"""
        child = "subagent_of" in context.permission_snapshot
        statement = select(Evidence.evidence_ref, Evidence.artifact_ref).join(
            ToolCall, ToolCall.id == Evidence.tool_call_id,
        ).where(Evidence.run_id == context.run_id, ToolCall.run_id == context.run_id)
        if child:
            if session_id is None:
                raise ValueError("Child result assembly requires its SDK session")
            statement = statement.where(ToolCall.agent_session_id == session_id)
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
            proposals = [] if child else (await session.execute(select(
                ChangeProposal.proposal_ref, ChangeProposal.status, ChangeProposal.summary,
                EffectExecution.before_ref, EffectExecution.after_ref,
            ).outerjoin(EffectExecution, EffectExecution.proposal_id == ChangeProposal.id).where(
                ChangeProposal.run_id == context.run_id,
            ).order_by(ChangeProposal.created_at, ChangeProposal.id))).all()
        effects = []
        for ref, status, summary, before, after in proposals:
            status = "PROPOSED" if status == "DRAFT" else status
            # 待機状態は全 proposal_refs に残して既存の未完了検査で拒否する。
            # 取消の記録は原 Proposal に残し、FAILED/APPLIED へ読み替えない。
            if status not in {"PROPOSED", "REJECTED", "APPLIED", "FAILED", "STALE"}:
                continue
            effect = {"proposal_ref": ref, "status": status, "summary": summary}
            if before is not None:
                effect["before_ref"] = before
            if after is not None:
                effect["after_ref"] = after
            effects.append(effect)
        return ResultAssemblySnapshot(
            evidence_refs=tuple(row[0] for row in rows),
            artifact_refs=tuple(row[1] for row in rows if row[1] is not None),
            proposal_refs=tuple(row[0] for row in proposals), effects=tuple(effects),
        )


class ResultAssemblyError(ValueError):
    """推測せず拒否すべき短参照の欠落・衝突を表す。"""

    def __init__(self, message: str, *, code: str = "evidence_reference_invalid") -> None:
        """既存の参照エラー分類と安全な位置診断だけを保持する。"""
        self.code = code
        super().__init__(message)


def assemble_model_result(
    candidate: Mapping[str, Any], snapshot: ResultAssemblySnapshot,
) -> dict[str, Any]:
    """モデルの業務内容を保持し、選択された引用と正本の操作事実から保存包絡を作る。"""
    references: dict[str, str] = {}
    for ref in (*snapshot.evidence_refs, *snapshot.artifact_refs):
        label = reference_label(ref)
        if label in references and references[label] != ref:
            raise ResultAssemblyError(
                "Result reference label is ambiguous",
                code="artifact_reference_invalid" if ref.startswith("art_")
                else "evidence_reference_invalid",
            )
        references[label] = ref

    def resolve(label: str, *, path: str, kind: str) -> str:
        """同 Run・同種の完全一致だけを解決し、欠落を削除や類似 ID で補完しない。"""
        ref = references.get(label)
        if ref is None or not ref.startswith(kind):
            raise ResultAssemblyError(
                f"Result reference is unavailable at {path}",
                code="artifact_reference_invalid" if kind == "art_"
                else "evidence_reference_invalid",
            )
        return ref

    result = deepcopy(dict(candidate))
    evidence: set[str] = set()
    artifacts: set[str] = set()
    for index, finding in enumerate(result["findings"]):
        finding["evidence_refs"] = [
            resolve(label, path=f"/findings/{index}/sources/{position}", kind="ev_")
            for position, label in enumerate(finding.pop("sources"))
        ]
        evidence.update(finding["evidence_refs"])
    for index, deliverable in enumerate(result["deliverables"]):
        if "artifact" in deliverable:
            ref = resolve(deliverable.pop("artifact"), path=f"/deliverables/{index}/artifact",
                          kind="art_")
            deliverable["artifact_ref"] = ref
            artifacts.add(ref)
    effects = deepcopy(list(snapshot.effects))
    for effect in effects:
        evidence.update(effect[key] for key in ("before_ref", "after_ref") if key in effect)
    result.update(
        outcome_version=OUTCOME_ENVELOPE_VERSION,
        evidence_refs=sorted(evidence), artifact_refs=sorted(artifacts),
        change_proposal_refs=list(snapshot.proposal_refs), effects=effects,
    )
    return result
