import { afterEach, expect, it, vi } from 'vitest'
import { changeRunDeletion, previewRunDeletion, manageDocuments, loadDocumentFolders, purgeProjectDocument } from '../../src/api'

const project = '00000000-0000-4000-8000-000000000020'
const run = '00000000-0000-4000-8000-000000000021'
const csrf = 's'.repeat(32)
const preview = { run_id: run, deleted: true, output_count: 0, protected_output_count: 0, cleanup_pending: 0, outputs: [] }
afterEach(() => vi.unstubAllGlobals())

it('sends explicit permanent removal without retrying or dropping output selection', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(preview), { status: 200 }))
  vi.stubGlobal('fetch', fetch)
  await changeRunDeletion(project, run, 'PURGE', true, csrf, new AbortController().signal)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0]?.[0]).toContain(`/runs/${run}/purge`)
  expect(fetch.mock.calls[0]?.[1]).toMatchObject({ method: 'DELETE', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ include_outputs: true }) })
})

it('rejects preview data belonging to another run or inconsistent protected counts', async () => {
  for (const data of [{ ...preview, run_id: project }, { ...preview, output_count: 1 }, { ...preview, protected_output_count: -1 }]) {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(data), { status: 200 })))
    await expect(previewRunDeletion(project, run, new AbortController().signal)).rejects.toThrow()
  }
})

it('keeps expected paths on a bulk move and does not resend an unknown write', async () => {
  const fetch = vi.fn().mockRejectedValue(new TypeError('connection lost'))
  vi.stubGlobal('fetch', fetch)
  const body = { action: 'MOVE' as const, changes: [{ document_id: run, expected_folder: 'specs', expected_name: 'original.md', folder: 'review', name: 'renamed.md' }] }
  await expect(manageDocuments(project, body, csrf)).rejects.toThrow()
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0]?.[1]).toMatchObject({ body: JSON.stringify(body) })
})

it('only invokes the trash-only purge endpoint for document permanent deletion', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
  vi.stubGlobal('fetch', fetch)
  await purgeProjectDocument(project, run, csrf)
  expect(fetch.mock.calls[0]?.[0]).toContain(`/documents/${run}?purge=true`)
})

it('rejects malformed folder lists', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"folders":[4]}', { status: 200 })))
  await expect(loadDocumentFolders(project)).rejects.toThrow()
})
