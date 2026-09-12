"""DB effect の Provider/権限回帰に使う、外部接続のない合成 snapshot。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from skillmind.effects.domain import ClaimedEffectExecution


def database_execution() -> ClaimedEffectExecution:
    """個別業務の schema や実 credential を含めず批准済み単行要求を作る。"""

    return ClaimedEffectExecution(
        effect_execution_id=uuid4(),
        proposal_id=uuid4(),
        proposal_ref="cp_database001",
        approval_id=uuid4(),
        run_id=uuid4(),
        run_segment_id=uuid4(),
        run_attempt_id=uuid4(),
        agent_session_id=uuid4(),
        project_id=uuid4(),
        integration_id=uuid4(),
        binding_id=uuid4(),
        capability_version="database.write/v1",
        operation="INSERT",
        target={"locator": "example.records"},
        changes=(
            {
                "path": "/row",
                "action": "SET",
                "value": {
                    "key": {"id": "row-1"},
                    "values": {"status": "RUNNING"},
                    "expected": None,
                },
            },
        ),
        precondition={"revision": "absent"},
        verification={"method": "READ_BACK", "paths": ["/row"]},
        idempotency_key="database:effect:001",
        request_fingerprint="a" * 64,
        provider="postgres",
        integration_revision=1,
        integration_scope={
            "tables": ["example.records"],
            "operations": ["INSERT", "UPDATE"],
            "write_columns": ["example.records.id", "example.records.status"],
        },
        integration_config={"host": "postgres.example.test", "database": "example"},
        secret_reference_id=uuid4(),
        lease_token="unit-test-lease",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
        attempt_no=1,
    )
