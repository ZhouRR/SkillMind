import { afterEach, expect, it, vi } from 'vitest'
import fixture from '../../../contracts/examples/run-detail-metrics.v1.json'
import { loadRunDetail } from '../../src/api'

afterEach(() => vi.unstubAllGlobals())

it('accepts the shared duration/metrics example and retains unknown causal counts', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture))))
  const detail = await loadRunDetail(fixture.project_id, fixture.run_id)
  expect(detail.started_at).toEqual(fixture.started_at)
  expect(detail.execution_metrics?.query_error_interventions).toBeNull()
})

it.each([-1, 0.5, '1', null])('rejects malformed recovery counters: %s', async (count) => {
  const value = { ...fixture, execution_metrics: { ...fixture.execution_metrics, corrected_reads: count } }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(value))))
  await expect(loadRunDetail(fixture.project_id, fixture.run_id)).rejects.toThrow('contract')
})
