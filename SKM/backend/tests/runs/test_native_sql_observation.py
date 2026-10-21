"""原生 SQL は業務観測を要求せず、旧行単位書込の観測境界は維持する。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from skillmind.effects.proposal_evidence import validate_database_observation


async def test_native_sql_does_not_query_for_prior_observation():
    """新規行への DML を UUID/時刻/事前 SELECT の証拠 hash に依存させない。"""
    session = AsyncMock()
    await validate_database_observation(
        session,
        SimpleNamespace(
            capability_version="database.execute/v1", evidence_refs=(), precondition={}
        ),
        binding=SimpleNamespace(),
        payload={"statement": {"sql": "DELETE FROM public.items"}},
    )
    session.scalars.assert_not_awaited()
