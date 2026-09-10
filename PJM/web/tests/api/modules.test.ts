import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  API_BASE,
  ApiProblemError,
  createProjectModule,
  deleteProjectModule,
  loadProjectModules,
  updateProjectModule,
} from '../../src/api/index'

const CSRF = 's'.repeat(32)
const PROJECT_ID = '00000000-0000-4000-8000-000000000020'
const MODULE_ID = '00000000-0000-4000-8000-000000000070'
const OTHER_ID = '00000000-0000-4000-8000-000000000071'

/** 保存済み module の代表 response。 */
const MODULE = {
  module_id: MODULE_ID,
  project_id: PROJECT_ID,
  name: '品质分析',
  description: '单票据品质分析模块',
  skills: [{
    skill_version_id: '00000000-0000-4000-8000-000000000061',
    skill_id: '00000000-0000-4000-8000-000000000060',
    skill_key: 'repository-review',
    skill_name: 'Repository Review',
    version: '1.0.0',
    sort_order: 0,
  }],
  created_at: '2026-07-12T10:00:00Z',
  updated_at: '2026-07-12T10:00:00Z',
} as const

const INPUT = {
  name: '品質を確認',
  description: '',
  skill_version_ids: [MODULE.skills[0].skill_version_id],
}

/** 同じ公開 consumer を三つの mutation の検証へ直接渡す。 */
const WRITES = [
  { name: 'create', status: 201, call: (signal?: AbortSignal) => createProjectModule(PROJECT_ID, INPUT, CSRF, signal) },
  { name: 'update', status: 200, call: (signal?: AbortSignal) => updateProjectModule(PROJECT_ID, MODULE_ID, INPUT, CSRF, signal) },
  { name: 'delete', status: 204, call: (signal?: AbortSignal) => deleteProjectModule(PROJECT_ID, MODULE_ID, CSRF, signal) },
] as const

/** JSON body を持つ fetch mock を作る。 */
function jsonFetch(body: unknown, status: number) {
  return vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() =>
    Promise.resolve(new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })),
  )
}

afterEach(() => vi.unstubAllGlobals())

describe('Project module API contract', () => {
  it('lists project modules and enforces the record contract', async () => {
    vi.stubGlobal('fetch', jsonFetch({ modules: [MODULE] }, 200))

    const modules = await loadProjectModules(PROJECT_ID)
    expect(modules[0]?.skills[0]?.skill_name).toBe('Repository Review')

    vi.stubGlobal('fetch', jsonFetch({ modules: [{ ...MODULE, skills: [{}] }] }, 200))
    await expect(loadProjectModules(PROJECT_ID))
      .rejects.toThrow('Module list response did not match its contract')
  })

  it('creates and updates modules with CSRF and the binding payload', async () => {
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(new Response(JSON.stringify(MODULE), { status: 201, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(MODULE), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    const input = {
      name: '品质分析',
      description: '',
      skill_version_ids: [MODULE.skills[0].skill_version_id],
    }
    await createProjectModule(PROJECT_ID, input, CSRF)
    await updateProjectModule(PROJECT_ID, MODULE_ID, input, CSRF)

    const [createCall, updateCall] = fetchMock.mock.calls
    expect(createCall?.[0]).toBe(`${API_BASE}/projects/${PROJECT_ID}/modules`)
    expect(createCall?.[1]?.method).toBe('POST')
    expect(updateCall?.[0]).toBe(`${API_BASE}/projects/${PROJECT_ID}/modules/${MODULE_ID}`)
    expect(updateCall?.[1]?.method).toBe('PUT')
    for (const call of fetchMock.mock.calls) {
      expect(call[1]?.credentials).toBe('same-origin')
      expect(new Headers(call[1]?.headers).get('X-CSRF-Token')).toBe(CSRF)
      expect(JSON.parse(String(call[1]?.body))).toMatchObject({ skill_version_ids: input.skill_version_ids })
    }
  })

  it('deletes a module with CSRF and no request body', async () => {
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() =>
      Promise.resolve(new Response(null, { status: 204 })),
    )
    vi.stubGlobal('fetch', fetchMock)

    await deleteProjectModule(PROJECT_ID, MODULE_ID, CSRF)

    const call = fetchMock.mock.calls[0]
    expect(call?.[0]).toBe(`${API_BASE}/projects/${PROJECT_ID}/modules/${MODULE_ID}`)
    expect(call?.[1]?.method).toBe('DELETE')
    expect(new Headers(call?.[1]?.headers).get('X-CSRF-Token')).toBe(CSRF)
    expect(call?.[1]?.body).toBeUndefined()
    expect(call?.[1]?.credentials).toBe('same-origin')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each(WRITES)('preserves stable business rejection for $name without retries', async ({ call }) => {
    for (const [status, code] of [
      [401, 'authentication_required'],
      [403, 'csrf_rejected'],
      [403, 'admin_required'],
      [404, 'project_not_found'],
      [404, 'module_not_found'],
      [409, 'project_archived'],
      [422, 'module_rejected'],
      [503, 'service_unavailable'],
    ] as const) {
      const detail = 'This operation is unavailable'
      const fetchMock = jsonFetch({ status, code, detail }, status)
      vi.stubGlobal('fetch', fetchMock)

      const failure = await call().catch((error: unknown) => error)
      expect(failure).toBeInstanceOf(ApiProblemError)
      expect(failure).toMatchObject({ status, code, message: detail })
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(fetchMock.mock.calls[0]?.[1]?.credentials).toBe('same-origin')
      expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('X-CSRF-Token')).toBe(CSRF)
    }
  })

  it.each(WRITES)('does not report other 2xx statuses as completed $name', async ({ call, status }) => {
    for (const unexpected of [200, 201, 202, 204, 205, 206].filter((value) => value !== status)) {
      const fetchMock = vi.fn().mockResolvedValue(new Response(
        unexpected === 204 || unexpected === 205 ? null : JSON.stringify(MODULE),
        { status: unexpected, headers: { 'Content-Type': 'application/json' } },
      ))
      vi.stubGlobal('fetch', fetchMock)
      const failure = await call().catch((error: unknown) => error)
      expect(failure).toBeInstanceOf(ApiProblemError)
      expect(failure).toMatchObject({ status: unexpected, code: undefined })
      expect(fetchMock).toHaveBeenCalledTimes(1)
    }
  })

  it.each(WRITES)('passes cancellation through $name without inferring rollback or retrying', async ({ call }) => {
    const controller = new AbortController()
    const failure = new DOMException('Synthetic abort', 'AbortError')
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>()
      .mockImplementation((_url, init) => new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(failure), { once: true })
      }))
    vi.stubGlobal('fetch', fetchMock)
    const request = call(controller.signal)
    const rejected = expect(request).rejects.toBe(failure)
    controller.abort()
    await rejected
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]?.[1]?.signal).toBe(controller.signal)
  })

  it.each(WRITES)('leaves transport failure for $name unknown and never retries', async ({ call }) => {
    const failure = new TypeError('Synthetic connection failure')
    const fetchMock = vi.fn().mockRejectedValue(failure)
    vi.stubGlobal('fetch', fetchMock)
    await expect(call()).rejects.toBe(failure)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each(WRITES)('preserves HTTP rejection when $name receives a proxy non-JSON body', async ({ call }) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('Synthetic gateway error', { status: 503 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(call()).rejects.toMatchObject({ status: 503, code: undefined, message: 'API returned 503' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each(WRITES.slice(0, 2))('rejects a malformed success body for $name without retrying', async ({ call, status }) => {
    for (const body of [null, {}, { ...MODULE, skills: [{}] }]) {
      const fetchMock = jsonFetch(body, status)
      vi.stubGlobal('fetch', fetchMock)
      await expect(call()).rejects.toThrow('Module response did not match its contract')
      expect(fetchMock).toHaveBeenCalledTimes(1)
    }
    const fetchMock = vi.fn().mockResolvedValue(new Response('not JSON', { status }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(call()).rejects.toMatchObject({ status, code: undefined, message: 'API returned a non-JSON response' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each(WRITES.slice(0, 2))('rejects a different Project receipt for $name', async ({ call, status }) => {
    const fetchMock = jsonFetch({ ...MODULE, project_id: OTHER_ID }, status)
    vi.stubGlobal('fetch', fetchMock)
    await expect(call()).rejects.toThrow('Module response did not match its contract')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('rejects a different module receipt for update without adopting its identity', async () => {
    const fetchMock = jsonFetch({ ...MODULE, module_id: OTHER_ID }, 200)
    vi.stubGlobal('fetch', fetchMock)
    await expect(updateProjectModule(PROJECT_ID, MODULE_ID, INPUT, CSRF))
      .rejects.toThrow('Module response did not match its contract')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('checks list status and Project identity without creating or deleting anything', async () => {
    const controller = new AbortController()
    const fetchMock = jsonFetch({ modules: [{ ...MODULE, project_id: OTHER_ID }] }, 200)
    vi.stubGlobal('fetch', fetchMock)
    await expect(loadProjectModules(PROJECT_ID, controller.signal))
      .rejects.toThrow('Module list response did not match its contract')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ signal: controller.signal, credentials: 'same-origin' })
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBeUndefined()
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).has('X-CSRF-Token')).toBe(false)

    vi.stubGlobal('fetch', jsonFetch({ modules: [MODULE] }, 202))
    await expect(loadProjectModules(PROJECT_ID)).rejects.toMatchObject({ status: 202, code: undefined })
  })
})
