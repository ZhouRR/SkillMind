import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  loadProjectPreference,
  loadUiLanguage,
  saveProjectPreference,
  saveUiLanguage,
} from '../../src/api/preferences'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('project preference API', () => {
  it('loads nullable project preference', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ project_id: null })))

    await expect(loadProjectPreference()).resolves.toEqual({ project_id: null })
  })

  it('saves the selected project with session CSRF', async () => {
    const projectId = '00000000-0000-4000-8000-000000000010'
    const csrfToken = 's'.repeat(32)
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ project_id: projectId }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(saveProjectPreference(projectId, csrfToken)).resolves.toEqual({
      project_id: projectId,
    })
    const request = fetchMock.mock.calls[0]?.[1]
    expect(request?.method).toBe('PUT')
    expect(request?.credentials).toBe('same-origin')
    expect(new Headers(request?.headers).get('X-CSRF-Token')).toBe(csrfToken)
    expect(request?.body).toBe(JSON.stringify({ project_id: projectId }))
  })

  it('rejects a response without an explicit nullable project identity', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({})))

    await expect(loadProjectPreference()).rejects.toThrow(
      'Project preference response did not match its contract',
    )
  })
})

describe('ui language preference API', () => {
  it('loads nullable ui language', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ ui_language: null })))

    await expect(loadUiLanguage()).resolves.toBeNull()
  })

  it('saves the selected language with session CSRF', async () => {
    const csrfToken = 's'.repeat(32)
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ ui_language: 'ja' }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(saveUiLanguage('ja', csrfToken)).resolves.toBe('ja')
    const request = fetchMock.mock.calls[0]?.[1]
    expect(request?.method).toBe('PUT')
    expect(request?.credentials).toBe('same-origin')
    expect(new Headers(request?.headers).get('X-CSRF-Token')).toBe(csrfToken)
    expect(request?.body).toBe(JSON.stringify({ ui_language: 'ja' }))
  })

  it('rejects languages outside the allowed set', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ ui_language: 'fr' })))

    await expect(loadUiLanguage()).rejects.toThrow(
      'UI language preference response did not match its contract',
    )
  })
})

/** Fetch test 用の JSON response を生成する。 */
function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}
