import { afterEach, describe, expect, it, vi } from 'vitest'

import account from '../../../contracts/examples/user-account.v1.json'
import event from '../../../contracts/examples/user-security-event.v1.json'
import {
  changeMyPassword, createUser, loadMyAccount, loadMySecurityEvents, loadUserAccount,
  loadUserSecurityEvents, loadUsers, revokeMySessions, revokeUserSessions, updateUser,
} from '../../src/api'

const ID = account.user_id
const CSRF = 'test-only-csrf-token'
const PASSWORD = 'test-only initial password'
const change = { display_name: 'Changed', system_role: 'USER' as const, status: 'ACTIVE' as const, expected_row_version: 1 }
const mutation = { user: account, revoked_sessions: 2, session_revoked: false }
const page = { items: [account], total: 101, limit: 25, offset: 0 }
const events = { items: [{ ...event, user_id: ID }], total: 1, limit: 25, offset: 0 }

afterEach(() => vi.unstubAllGlobals())

/** 実 credential や backend を使わない HTTP response。 */
function response(value: unknown, status = 200, headers: HeadersInit = {}): Response {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json', ...headers } })
}

describe('user management contract clients', () => {
  it.each([
    ['own account', () => loadMyAccount(), '/users/me/account', 'GET', account],
    ['exact user', () => loadUserAccount(ID), `/users/${ID}`, 'GET', account],
    ['users', () => loadUsers(), '/users?limit=25&offset=0', 'GET', page],
    ['own audit', () => loadMySecurityEvents(), '/users/me/security-events?limit=25&offset=0', 'GET', events],
    ['user audit', () => loadUserSecurityEvents(ID), `/users/${ID}/security-events?limit=25&offset=0`, 'GET', events],
    ['create', () => createUser({ email: 'new@example.test', display_name: 'New', system_role: 'USER', password: PASSWORD }, CSRF), '/users', 'POST', mutation],
    ['update', () => updateUser(ID, change, CSRF), `/users/${ID}`, 'PUT', mutation],
    ['password', () => changeMyPassword({ current_password: PASSWORD, new_password: PASSWORD, expected_row_version: 1 }, CSRF), '/users/me/password', 'POST', mutation],
    ['own revoke', () => revokeMySessions(1, CSRF), '/users/me/sessions/revoke', 'POST', mutation],
    ['user revoke', () => revokeUserSessions(ID, 1, CSRF), `/users/${ID}/sessions/revoke`, 'POST', mutation],
  ] as const)('connects %s through the common no-store boundary', async (_name, call, path, method, payload) => {
    // 全操作を列挙し、存在する DTO だけで接続済みと誤認しない。
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(response(payload))
    vi.stubGlobal('fetch', fetchMock)
    await expect(call()).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe(`/api/v1${path}`)
    expect(init?.method ?? 'GET').toBe(method)
    expect(init?.cache).toBe('no-store')
    expect(init?.credentials).toBe('same-origin')
    expect(new Headers(init?.headers).get('X-CSRF-Token')).toBe(method === 'GET' ? null : CSRF)
    expect(new Headers(init?.headers).get('Idempotency-Key')).toBeNull()
  })

  it('preserves server search and page identity without filtering the first page', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(response({ ...page, offset: 75 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(loadUsers('a+b@example.test', 25, 75)).resolves.toMatchObject({ total: 101, offset: 75 })
    const url = new URL(String(fetchMock.mock.calls[0]![0]), 'https://example.test')
    expect(url.searchParams.get('q')).toBe('a+b@example.test')
    expect(url.searchParams.get('offset')).toBe('75')
  })

  it('projects only allowed update fields and keeps the original version', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(response(mutation))
    vi.stubGlobal('fetch', fetchMock)
    await updateUser(ID, { ...change, expected_row_version: 9, ...{ password: PASSWORD, email: 'ignored@example.test' } }, CSRF)
    const body = String(fetchMock.mock.calls[0]![1]?.body)
    expect(JSON.parse(body)).toEqual({ ...change, expected_row_version: 9 })
    expect(body).not.toContain(PASSWORD)
  })

  it.each([
    { password_hash: 'must-not-be-exposed' }, { row_version: true }, { row_version: 0 },
    { row_version: Number.MAX_SAFE_INTEGER + 1 }, { user_id: 'not-a-uuid' }, { system_role: 'OWNER' },
    { status: 'DELETED' }, { created_at: '2026-02-30T12:00:00Z' }, { updated_at: '2026-09-09' },
  ])('rejects invalid account fields %j', async (patch) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(response({ ...account, ...patch })))
    await expect(loadMyAccount()).rejects.toThrow('User response did not match its contract')
  })

  it('rejects missing account fields and wrong target identities', async () => {
    const { email: _email, ...missing } = account
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(response(missing))
      .mockResolvedValueOnce(response({ ...account, user_id: '11111111-1111-1111-1111-111111111111' }))
      .mockResolvedValueOnce(response({ ...mutation, user: { ...account, user_id: '11111111-1111-1111-1111-111111111111' } }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(loadMyAccount()).rejects.toThrow()
    await expect(loadUserAccount(ID)).rejects.toThrow()
    await expect(updateUser(ID, change, CSRF)).rejects.toThrow()
  })

  it.each([
    { offset: 1 }, { limit: 50 }, { total: -1 }, { total: true }, { metadata: {} },
    { items: [{ ...event, user_id: '11111111-1111-1111-1111-111111111111' }] },
    { items: [{ ...event, user_id: ID, request_id: 'client-request' }] },
    { items: [{ ...event, user_id: ID, previous_status: 'UNKNOWN' }] },
  ])('rejects mismatched audit/page data %j', async (patch) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(response({ ...events, ...patch })))
    await expect(loadUserSecurityEvents(ID)).rejects.toThrow()
  })

  it.each([400, 401, 403, 404, 409, 422, 429, 503])('preserves %d without retrying or clearing the session', async (status) => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(response({ code: 'stable_code' }, status, { 'Retry-After': '30' }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(changeMyPassword({ current_password: PASSWORD, new_password: PASSWORD, expected_row_version: 1 }, CSRF))
      .rejects.toMatchObject({ status, code: 'stable_code', retryAfterSeconds: 30 })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('rejects malformed success, negative revocation and extra credential data', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response('not-json', { status: 200 }))
      .mockResolvedValueOnce(response({ ...mutation, revoked_sessions: -1 }))
      .mockResolvedValueOnce(response({ ...mutation, csrf_token: CSRF }))
    vi.stubGlobal('fetch', fetchMock)
    for (let attempt = 0; attempt < 3; attempt++) await expect(revokeMySessions(1, CSRF)).rejects.toThrow()
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('does not dispatch invalid versions, targets or pages', () => {
    const fetchMock = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetchMock)
    for (const value of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) expect(() => revokeMySessions(value, CSRF)).toThrow()
    expect(() => loadUserAccount('../me/account')).toThrow()
    expect(() => loadUsers('', 101)).toThrow()
    expect(() => loadUsers('x'.repeat(201))).toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('does not dispatch after abort or accept transport results that ignored abort', async () => {
    const controller = new AbortController()
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async () => {
      controller.abort()
      return response(mutation)
    })
    vi.stubGlobal('fetch', fetchMock)
    await expect(revokeMySessions(1, CSRF, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
    await expect(revokeMySessions(1, CSRF, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
