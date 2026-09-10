import { describe, expect, it, vi, afterEach } from 'vitest'

import fixture from '../../../contracts/examples/run-detail-documents.v1.json'
import { loadRunDetail } from '../../src/api'
import { isRunDocumentSnapshots, isRunSourceSummaries } from '../../src/api/runResources'

afterEach(() => vi.unstubAllGlobals())

describe('frozen document public projection', () => {
  it('accepts the same representative example as the backend and keeps all three states', async () => {
    // API wrapper と純 validator の両方が同じ契約 example を受理する。
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture), { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    const result = await loadRunDetail(fixture.project_id, fixture.run_id)
    expect(result.document_snapshots.map((entry) => entry.status)).toEqual(['FROZEN', 'LEGACY_UNAVAILABLE', 'INVALID'])
    expect(result.document_snapshots).toEqual(fixture.document_snapshots)
    expect(isRunSourceSummaries(result.selected_sources)).toBe(true)
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('does not fabricate an empty scope when an older API omits the field', async () => {
    const body: Record<string, unknown> = { ...fixture }
    delete body.document_snapshots
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(body))))
    await expect(loadRunDetail(fixture.project_id, fixture.run_id)).rejects.toThrow('contract')
    expect(isRunDocumentSnapshots([], fixture.project_id, fixture.selected_sources)).toBe(true)
  })

  it.each(['provider', 'scope', 'secret_locator', 'document_snapshot'])('rejects internal or mistyped summary data: %s', (key) => {
    const value = { documents: { provider: 'project-documents', [key]: { private: true } } }
    expect(isRunSourceSummaries(value)).toBe(false)
  })

  it.each(['project', 'slot', 'checksum-shape', 'mode', 'duplicate', 'path', 'extra', 'state', 'null', 'reordered', 'cross-slot'])('rejects inconsistent public snapshots: %s', (mutation) => {
    // 内部値を UI へ渡す前に拒否し、現在の文書を追加取得して補修しない。
    const body = structuredClone(fixture)
    const entry = body.document_snapshots[0]!
    const snapshot = entry.snapshot!
    if (mutation === 'project') snapshot.project_id = '00000000-0000-4000-8000-000000000099'
    if (mutation === 'slot') snapshot.requirement_key = 'another-slot'
    if (mutation === 'checksum-shape') snapshot.checksum = 'invalid'
    if (mutation === 'mode') snapshot.selection_mode = 'SINGLE'
    if (mutation === 'duplicate') snapshot.documents[1] = structuredClone(snapshot.documents[0]!)
    if (mutation === 'path') snapshot.documents[0]!.folder = '../outside'
    if (mutation === 'extra') Object.assign(snapshot.documents[0]!, { blob_key: 'not-public' })
    if (mutation === 'state') entry.status = 'LEGACY_UNAVAILABLE'
    if (mutation === 'null') entry.snapshot = null
    if (mutation === 'reordered') snapshot.documents.reverse()
    if (mutation === 'cross-slot') {
      const extra = structuredClone(entry)
      extra.requirement_key = 'invalid-documents'
      extra.snapshot!.requirement_key = extra.requirement_key
      extra.snapshot!.documents[0]!.content_hash = `sha256:${'f'.repeat(64)}`
      body.document_snapshots[2] = extra
    }
    expect(isRunDocumentSnapshots(body.document_snapshots, body.project_id, body.selected_sources)).toBe(false)
  })
})
