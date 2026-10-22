"""認可・原批准を検証済みの記録から、只読照会用の元要求を組み立てる。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from skillmind.core.hashing import canonical_json
from skillmind.db.models import ChangeProposal, EffectExecution, Integration, ResourceBinding
from skillmind.documents.library import (
    DOCUMENT_WRITE_CAPABILITY,
    DocumentLibraryTarget,
    document_library_revision,
)
from skillmind.effects.database_write import DATABASE_WRITE_CAPABILITY, build_database_write
from skillmind.effects.document_command import load_document_effect_command
from skillmind.effects.git_receipt import GitCommitCommand, git_effect_commit_message
from skillmind.effects.mcp_receipt import McpOperationCommand
from skillmind.effects.reconciliation_domain import EffectReconciliationTarget


async def build_reconciliation_target(
    session: AsyncSession, *, project_id: UUID, run_id: UUID, proposal: ChangeProposal,
    execution: EffectExecution, binding: ResourceBinding, payload: dict[str, Any],
    document_library_target: DocumentLibraryTarget | None, legacy_document: bool,
) -> EffectReconciliationTarget:
    """原要求の値を保持し、transaction や外部 write を新設しない。"""
    if proposal.capability_version == "database.execute/v1":
        raise ValueError(
            "Native SQL has no remote operation receipt; "
            "inspect the original audit before deciding recovery"
        )
    if proposal.capability_version == DATABASE_WRITE_CAPABILITY:
        if proposal.integration_id is None:
            raise ValueError("Original database effect has no Integration")
        integration = await session.get(Integration, proposal.integration_id)
        if integration is None:
            raise ValueError("Original database Integration is unavailable")
        command = build_database_write(
            effect_id=execution.id,
            project_id=project_id,
            run_id=run_id,
            integration_id=integration.id,
            scope=binding.scope_json,
            **payload,
        )
        return EffectReconciliationTarget(
            proposal.id,
            binding.id,
            proposal.checksum,
            execution.provider,
            execution.provider_version,
            command,
            canonical_json(integration.config_json),
            integration.secret_reference_id,
        )
    if proposal.capability_version == "mcp.call/v1":
        integration = await session.get(Integration, proposal.integration_id)
        if integration is None:
            raise ValueError("Original MCP Integration is unavailable")
        command_mcp = McpOperationCommand(
            execution.id,
            project_id,
            run_id,
            integration.id,
            payload["name"],
            canonical_json(payload),
        )
        return EffectReconciliationTarget(
            proposal.id,
            binding.id,
            proposal.checksum,
            execution.provider,
            execution.provider_version,
            command_mcp,
            canonical_json(integration.config_json),
            integration.secret_reference_id,
        )
    if proposal.capability_version == "repository.write/v1" and execution.provider == "git":
        integration = await session.get(Integration, proposal.integration_id)
        if integration is None:
            raise ValueError("Original Git Integration is unavailable")
        git_command = GitCommitCommand(
            execution.id,
            project_id,
            run_id,
            integration.id,
            payload["target_branch"],
            payload["base_revision"],
            git_effect_commit_message(
                display=proposal.target_json.get("display"),
                proposal_ref=proposal.proposal_ref,
                effect_id=execution.id,
                fingerprint=execution.request_fingerprint,
            ),
            tuple(sorted(payload["files"].items())),
        )
        return EffectReconciliationTarget(
            proposal.id,
            binding.id,
            proposal.checksum,
            execution.provider,
            execution.provider_version,
            git_command,
            canonical_json(integration.config_json),
            integration.secret_reference_id,
        )
    if proposal.capability_version != DOCUMENT_WRITE_CAPABILITY:
        raise ValueError("Original effect Provider does not support reconciliation")
    if document_library_target is None:
        raise ValueError("Original document storage is unavailable")
    if proposal.operation not in {"CREATE", "UPDATE"}:
        from types import SimpleNamespace

        from skillmind.effects.document_management import (
            DocumentManagementCommand,
            mutation_checksum,
        )

        identity = SimpleNamespace(
            effect_execution_id=execution.id,
            project_id=project_id,
            run_id=run_id,
            operation=proposal.operation,
        )
        return EffectReconciliationTarget(
            proposal.id,
            binding.id,
            proposal.checksum,
            execution.provider,
            execution.provider_version,
            DocumentManagementCommand(
                execution.id, project_id, run_id, mutation_checksum(identity, payload)
            ),
            "{}",
            None,
        )
    revision = document_library_revision(binding.scope_json)
    if (revision == "1") != legacy_document:
        raise ValueError("Original document protocol does not match its binding")
    object_command = await load_document_effect_command(
        session,
        effect_id=execution.id,
        project_id=project_id,
        run_id=run_id,
        payload=payload,
        target=document_library_target,
        protocol_version=int(revision),
    )
    return EffectReconciliationTarget(
        proposal.id,
        binding.id,
        proposal.checksum,
        execution.provider,
        execution.provider_version,
        object_command,
        "{}",
        None,
    )

