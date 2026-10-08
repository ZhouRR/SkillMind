import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadPendingRunPage, loadPendingRuns } from '../../src/api'
import { interactionRun } from '../fixtures/interaction'

afterEach(() => vi.unstubAllGlobals())
describe('Pending-run page metadata', () => {
  it('retains has_more and the shared pending statuses without changing the legacy list consumer', async () => {
    const item = { ...interactionRun(), input: {}, selected_sources: {}, started_at: null, finished_at: null,
      result_summary: null, result_confidence: null, result_needs_review: null }
    const page = { items: [item], limit: 10, offset: 0, has_more: true }
    const transport = vi.fn<typeof fetch>().mockImplementation(async () => new Response(JSON.stringify(page), {
      headers: { 'Content-Type': 'application/json' }, status: 200,
    }))
    vi.stubGlobal('fetch', transport)
    await expect(loadPendingRunPage(item.project_id, 10)).resolves.toEqual(page)
    const url = new URL(String(transport.mock.calls[0]![0]), 'https://example.test')
    expect(url.searchParams.getAll('status')).toEqual(['WAITING_FOR_INPUT', 'WAITING_FOR_APPROVAL'])
    await expect(loadPendingRuns(item.project_id, 10)).resolves.toEqual(page.items)
  })
})
