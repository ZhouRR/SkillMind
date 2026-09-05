import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  archiveProject,
  createProject,
  loadAuthSession,
  loadProjects,
  login,
  logout,
} from '../../src/api/index'
import { DEMO_PROJECT, demoSession } from '../fixtures'

const SESSION = demoSession('ADMIN')
const PROJECT = DEMO_PROJECT

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('auth API client', () => {
  it('treats authentication_required as an anonymous session', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(
      { detail: 'A valid session is required.', code: 'authentication_required' },
      401,
    ))
    vi.stubGlobal('fetch', fetchMock)

    await expect(loadAuthSession()).resolves.toBeNull()
  })

  it('uses the one-time login challenge and retains the returned session CSRF', async () => {
    const loginCsrf = 'l'.repeat(32)
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: loginCsrf, expires_in_seconds: 300 }))
      .mockResolvedValueOnce(jsonResponse(SESSION))
    vi.stubGlobal('fetch', fetchMock)

    await expect(login({ email: 'admin@example.com', password: 'password value' }))
      .resolves.toEqual(SESSION)
    const loginRequest = fetchMock.mock.calls[1]?.[1]
    expect(new Headers(loginRequest?.headers).get('X-CSRF-Token')).toBe(loginCsrf)
    expect(loginRequest?.credentials).toBe('same-origin')
  })

  it('sends the in-memory session CSRF when logging out', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    await logout(SESSION.csrf_token)

    const request = fetchMock.mock.calls[0]?.[1]
    expect(new Headers(request?.headers).get('X-CSRF-Token')).toBe(SESSION.csrf_token)
    expect(request?.method).toBe('POST')
  })
})

describe('project API client', () => {
  it('validates accessible project lists', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ items: [PROJECT] })))

    await expect(loadProjects()).resolves.toEqual([PROJECT])
  })

  it('rejects a project response that omits its isolation identity', async () => {
    const { project_id: _projectId, ...invalid } = PROJECT
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ items: [invalid] })))

    await expect(loadProjects()).rejects.toThrow('Project list response did not match its contract')
  })

  it('uses CSRF for create and archive mutations', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(PROJECT, 201))
      .mockResolvedValueOnce(jsonResponse({ ...PROJECT, status: 'ARCHIVED' }))
    vi.stubGlobal('fetch', fetchMock)

    await createProject({
      key: PROJECT.key,
      name: PROJECT.name,
      description: PROJECT.description,
      settings: {},
      retention_days: 90,
    }, SESSION.csrf_token)
    await archiveProject(PROJECT.project_id, SESSION.csrf_token)

    for (const call of fetchMock.mock.calls) {
      expect(new Headers(call[1]?.headers).get('X-CSRF-Token')).toBe(SESSION.csrf_token)
    }
  })
})

/** Fetch test 用の JSON response を生成する。 */
function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}
