import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  archiveProject,
  createProject,
  deleteProject,
  loadAuthSession,
  loadProject,
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
    expect(fetchMock.mock.calls[0]?.[1]?.cache).toBe('no-store')
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
    expect(fetchMock.mock.calls[0]?.[1]?.cache).toBe('no-store')
    expect(loginRequest?.cache).toBe('no-store')
  })

  it('sends the in-memory session CSRF when logging out', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    await logout(SESSION.csrf_token)

    const request = fetchMock.mock.calls[0]?.[1]
    expect(new Headers(request?.headers).get('X-CSRF-Token')).toBe(SESSION.csrf_token)
    expect(request?.method).toBe('POST')
    expect(request?.cache).toBe('no-store')

  })

  it.each([429, 503])('stops before sending a password when the context returns %d', async (status) => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ detail: 'Unavailable' }, status))
    vi.stubGlobal('fetch', fetchMock)
    await expect(login({ email: 'reader@example.com', password: 'test-only password' }))
      .rejects.toMatchObject({ status })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBeUndefined()
  })

  it.each([429, 503])('does not retry a password rejected with %d', async (status) => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'c'.repeat(32), expires_in_seconds: 300 }))
      .mockResolvedValueOnce(jsonResponse({ detail: 'Unavailable' }, status))
    vi.stubGlobal('fetch', fetchMock)
    await expect(login({ email: 'reader@example.com', password: 'test-only password' }))
      .rejects.toMatchObject({ status })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('does not begin an already aborted login', async () => {
    const controller = new AbortController()
    controller.abort()
    const fetchMock = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetchMock)
    await expect(login({ email: 'reader@example.com', password: 'test-only password' }, controller.signal))
      .rejects.toMatchObject({ name: 'AbortError' })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it.each(['context', 'password'])('rejects a late %s response even if transport ignores abort', async (stage) => {
    const controller = new AbortController()
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (_url, init) => {
      const password = init?.method === 'POST'
      if (password === (stage === 'password')) controller.abort()
      return jsonResponse(password ? SESSION : { csrf_token: 'c'.repeat(32), expires_in_seconds: 300 })
    })
    vi.stubGlobal('fetch', fetchMock)
    await expect(login({ email: 'reader@example.com', password: 'test-only password' }, controller.signal))
      .rejects.toMatchObject({ name: 'AbortError' })
    expect(fetchMock).toHaveBeenCalledTimes(stage === 'context' ? 1 : 2)
  })

  it('keeps the server token opaque across multiple session reads', async () => {
    // 版文字列を Web で導出せず、同じ会話の各ページが server の値をそのまま使う。
    const session = { ...SESSION, csrf_token: `csrf2.${'s'.repeat(43)}` }
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(session))
    vi.stubGlobal('fetch', fetchMock)

    const first = await loadAuthSession()
    fetchMock.mockResolvedValueOnce(jsonResponse(session))
    const second = await loadAuthSession()

    expect(first?.csrf_token).toBe(session.csrf_token)
    expect(second?.csrf_token).toBe(first?.csrf_token)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})

describe('project API client', () => {
  it.each(['ACTIVE', 'ARCHIVED'] as const)('reads the requested %s project outside the active list', async (status) => {
    const project = { ...PROJECT, status }
    const controller = new AbortController()
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(project))
    vi.stubGlobal('fetch', fetchMock)

    await expect(loadProject(PROJECT.project_id.toUpperCase(), controller.signal)).resolves.toEqual(project)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]?.[0]).toMatch(new RegExp(`/projects/${PROJECT.project_id.toUpperCase()}$`))
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      signal: controller.signal, cache: 'no-store', credentials: 'same-origin',
    })
  })

  it.each(['', 'not-a-uuid', '../projects', `${PROJECT.project_id}?project=other`])('does not request a malformed project identity %s', async (id) => {
    const fetchMock = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetchMock)
    await expect(loadProject(id)).rejects.toThrow('Invalid project identity')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects a valid response for a different project without looking for a replacement', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      ...PROJECT, project_id: 'ffffffff-ffff-ffff-ffff-ffffffffffff',
    }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(loadProject(PROJECT.project_id)).rejects.toThrow('requested identity')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each([
    { project_id: 'bad-id' }, { organization_id: 'private' }, { key: '' },
    { name: '' }, { name: 'x'.repeat(201) }, { description: 'x'.repeat(4001) },
    { retention_days: 0 }, { retention_days: 3651 }, { retention_days: 1.5 },
    { status: 'DELETED' }, { created_at: '2026-02-30T00:00:00Z' },
    { updated_at: '2026-09-09' }, { settings: [] },
  ])('rejects details outside the current Project contract: %j', async (patch) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ ...PROJECT, ...patch })))
    await expect(loadProject(PROJECT.project_id)).rejects.toThrow('Project response did not match its contract')
  })

  it('accepts Unicode character limits without counting surrogate pairs twice', async () => {
    const project = { ...PROJECT, name: '😀'.repeat(200) }
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(project)))
    await expect(loadProject(PROJECT.project_id)).resolves.toEqual(project)
  })

  it('keeps missing or inaccessible projects as one 404 without a list fallback', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      code: 'project_not_found', detail: 'Project not found.',
    }, 404))
    vi.stubGlobal('fetch', fetchMock)
    await expect(loadProject(PROJECT.project_id)).rejects.toMatchObject({ status: 404, code: 'project_not_found' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not read details after an already aborted request', async () => {
    const controller = new AbortController()
    controller.abort()
    const fetchMock = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetchMock)
    await expect(loadProject(PROJECT.project_id, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects late project data when a transport ignores abort', async () => {
    const controller = new AbortController()
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockImplementation(async () => {
      controller.abort()
      return jsonResponse(PROJECT)
    }))
    await expect(loadProject(PROJECT.project_id, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  })

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
    await archiveProject(PROJECT.project_id, PROJECT.row_version, SESSION.csrf_token)

    for (const call of fetchMock.mock.calls) {
      expect(new Headers(call[1]?.headers).get('X-CSRF-Token')).toBe(SESSION.csrf_token)
    }
  })

  it.each([
    'project_delete_requires_archive', 'project_delete_blocked_by_runs',
    'project_delete_blocked_by_schedules',
  ])('preserves the deletion conflict %s and never retries or removes references', async (code) => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ code, detail: 'Deletion blocked.' }, 409))
    vi.stubGlobal('fetch', fetchMock)
    await expect(deleteProject(PROJECT.project_id, PROJECT.row_version, SESSION.csrf_token)).rejects.toMatchObject({ status: 409, code })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('DELETE')
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('X-CSRF-Token')).toBe(SESSION.csrf_token)
  })
})

/** Fetch test 用の JSON response を生成する。 */
function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}
