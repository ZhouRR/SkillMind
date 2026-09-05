import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  API_BASE,
  createProjectModule,
  deleteProjectModule,
  loadProjectModules,
  updateProjectModule,
} from '../../src/api/index'

const CSRF = 's'.repeat(32)
const PROJECT_ID = '00000000-0000-4000-8000-000000000020'
const MODULE_ID = '00000000-0000-4000-8000-000000000070'

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
  })
})
