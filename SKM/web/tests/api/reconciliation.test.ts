import record from '../../../contracts/examples/effect-reconciliation.v1.json'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { confirmReconciliation, loadLatestReconciliation, parseReconciliation, requestReconciliation } from '../../src/api/reconciliation'

const scope = { projectId: record.project_id, runId: record.run_id, effectId: record.effect_execution_id }
afterEach(() => vi.unstubAllGlobals())

describe('reconciliation boundary', () => {
  it.each(['project_id', 'run_id', 'effect_execution_id', 'request_id'])('rejects a different %s', (field) => {
    expect(() => parseReconciliation({ ...record, [field]: '00000000-0000-4000-8000-000000000088' }, scope, record.request_id)).toThrow()
  })
  it.each([
    { receipt_json: {} }, { owner_hash: 'private' }, { status: 'APPLIED' }, { observed_at: null },
    { status: 'FAILED' }, { error_code: 'private failure' },
    { observed_at: '2026-09-11T11:59:59Z' }, { finished_at: '2026-09-11T12:00:01Z' },
  ])('rejects private fields and contradictory observations', (change) => {
    expect(() => parseReconciliation({ ...record, ...change }, scope)).toThrow()
  })
  it('accepts all public observation outcomes without changing effect state', () => {
    for (const observation_status of ['CONFIRMED', 'NOT_OBSERVED', 'CONFLICT']) {
      expect(parseReconciliation({ ...record, observation_status }, scope).status).toBe('SUCCEEDED')
    }
  })
  it('sends the original ID and CSRF, and distinguishes an empty latest lookup', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(record), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(record)))
      .mockResolvedValueOnce(new Response(JSON.stringify({ latest: null })))
    vi.stubGlobal('fetch', fetch)
    await requestReconciliation(scope, record.request_id, 'fixture-csrf')
    expect(JSON.parse(fetch.mock.calls[0]![1].body)).toEqual({ request_id: record.request_id })
    expect(fetch.mock.calls[0]![1].headers['X-CSRF-Token']).toBe('fixture-csrf')
    await confirmReconciliation(scope, record.request_id)
    expect(fetch.mock.calls[1]![0]).toContain(`/effect-reconciliations/${record.request_id}`)
    expect(await loadLatestReconciliation(scope)).toBeNull()
  })
})
