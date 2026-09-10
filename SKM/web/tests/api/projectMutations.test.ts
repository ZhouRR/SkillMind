import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  archiveProject, createProject, deleteProject, loadProject, loadProjects,
  unarchiveProject, updateProject,
} from '../../src/api'
import { DEMO_PROJECT, demoSession } from '../fixtures'

const PROJECT = { ...DEMO_PROJECT, row_version: 7 }
const CSRF = demoSession('ADMIN').csrf_token
const CREATE = {
  key: PROJECT.key, name: PROJECT.name, description: PROJECT.description,
  retention_days: PROJECT.retention_days, settings: PROJECT.settings,
}
const WRITE_CASES = [
  ['update', (id: string, version: number, signal?: AbortSignal) => updateProject(
    id, { name: 'Updated', expected_row_version: version }, CSRF, signal,
  ), { ...PROJECT, row_version: 8, name: 'Updated' }],
  ['archive', (id: string, version: number, signal?: AbortSignal) => archiveProject(id, version, CSRF, signal),
    { ...PROJECT, row_version: 8, status: 'ARCHIVED' }],
  ['restore', (id: string, version: number, signal?: AbortSignal) => unarchiveProject(id, version, CSRF, signal),
    { ...PROJECT, row_version: 8 }],
] as const

afterEach(() => { vi.unstubAllGlobals() })

/** 送信値をそのまま検査できる mock HTTP response。 */
function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
}

describe('versioned project transport', () => {
  it.each(WRITE_CASES)('sends the original version and consumes only the %s response', async (_name, call, result) => {
    const transport = vi.fn<typeof fetch>().mockResolvedValue(json(result))
    vi.stubGlobal('fetch', transport)
    await expect(call(PROJECT.project_id.toUpperCase(), 7)).resolves.toEqual(result)
    expect(transport).toHaveBeenCalledTimes(1)
    const request = transport.mock.calls[0]?.[1]
    expect(request).toMatchObject({ cache: 'no-store', credentials: 'same-origin' })
    expect(new Headers(request?.headers).get('X-CSRF-Token')).toBe(CSRF)
    expect(JSON.parse(String(request?.body))).toMatchObject({ expected_row_version: 7 })
  })

  it.each(WRITE_CASES)('accepts only the original version for a %s no-op', async (_name, call, result) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(json({ ...result, row_version: 7 })))
    await expect(call(PROJECT.project_id, 7)).resolves.toMatchObject({ row_version: 7 })
  })

  it.each([0, -1, 1.5, 2147483648, NaN, Infinity])('does not send invalid original version %s', async (version) => {
    const transport = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', transport)
    for (const [, call] of WRITE_CASES) await expect(call(PROJECT.project_id, version)).rejects.toThrow('Invalid project version')
    await expect(deleteProject(PROJECT.project_id, version, CSRF)).rejects.toThrow('Invalid project version')
    expect(transport).not.toHaveBeenCalled()
  })

  it.each([undefined, null, true, '7', 0, -1, 1.5, 2147483648])('rejects an invalid or missing returned version %s', async (row_version) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockImplementation(async () => json({ ...PROJECT, row_version })))
    await expect(loadProject(PROJECT.project_id)).rejects.toThrow('contract')
    await expect(loadProjects()).rejects.toThrow('contract')
  })

  it.each(WRITE_CASES)('rejects an unrelated identity or version from %s', async (_name, call, result) => {
    for (const patch of [
      { project_id: 'ffffffff-ffff-4fff-8fff-ffffffffffff' }, { row_version: 6 }, { row_version: 9 },
    ]) {
      vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(json({ ...result, ...patch })))
      await expect(call(PROJECT.project_id, 7)).rejects.toThrow('requested')
    }
  })

  it('does not accept opposite lifecycle states', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(json(PROJECT)))
    await expect(archiveProject(PROJECT.project_id, 7, CSRF)).rejects.toThrow('requested status')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(json({ ...PROJECT, status: 'ARCHIVED' })))
    await expect(unarchiveProject(PROJECT.project_id, 7, CSRF)).rejects.toThrow('requested status')
  })

  it('does not accept unrelated metadata as the result of an update', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(json({ ...PROJECT, row_version: 8 })))
    await expect(updateProject(PROJECT.project_id, { name: 'Updated', expected_row_version: 7 }, CSRF))
      .rejects.toThrow('requested metadata')
  })

  it('does not send malformed target identities for any existing-project write', async () => {
    const transport = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', transport)
    for (const [, call] of WRITE_CASES) await expect(call('../projects', 7)).rejects.toThrow('Invalid project identity')
    await expect(deleteProject('../projects', 7, CSRF)).rejects.toThrow('Invalid project identity')
    expect(transport).not.toHaveBeenCalled()
  })

  it('requires version one and the requested key for creation', async () => {
    const created = { ...PROJECT, row_version: 1 }
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(json(created, 201)))
    await expect(createProject(CREATE, CSRF)).resolves.toEqual(created)
    for (const patch of [{ row_version: 7 }, { key: 'other' }, { status: 'ARCHIVED' }]) {
      vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(json({ ...created, ...patch }, 201)))
      await expect(createProject(CREATE, CSRF)).rejects.toThrow('requested creation')
    }
  })

  it('does not interpret an accepted or mismatched HTTP status as completed creation', async () => {
    for (const status of [200, 202]) {
      vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(json({ ...PROJECT, row_version: 1 }, status)))
      await expect(createProject(CREATE, CSRF)).rejects.toMatchObject({ status })
    }
  })

  it('puts the deletion version in query and requires exactly 204', async () => {
    const transport = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', transport)
    await expect(deleteProject(PROJECT.project_id, 7, CSRF)).resolves.toBeUndefined()
    expect(String(transport.mock.calls[0]?.[0])).toMatch(/\?expected_row_version=7$/u)
    expect(transport.mock.calls[0]?.[1]).toMatchObject({ method: 'DELETE', cache: 'no-store' })
    for (const status of [200, 202, 205]) {
      transport.mockResolvedValueOnce(new Response(null, { status }))
      await expect(deleteProject(PROJECT.project_id, 7, CSRF)).rejects.toMatchObject({ status })
    }
  })

  it.each(['project_version_conflict', 'project_version_exhausted'])('preserves %s without retry', async (code) => {
    const transport = vi.fn<typeof fetch>().mockImplementation(async () => json({ code, detail: 'Conflict.' }, 409))
    vi.stubGlobal('fetch', transport)
    for (const [, call] of WRITE_CASES) await expect(call(PROJECT.project_id, 7)).rejects.toMatchObject({ status: 409, code })
    expect(transport).toHaveBeenCalledTimes(3)
  })

  it('rejects ambiguous list identities and extra wrapper fields', async () => {
    for (const value of [
      { items: [PROJECT], extra: true },
      { items: [PROJECT, { ...PROJECT, project_id: PROJECT.project_id.toUpperCase() }] },
      { items: [PROJECT, { ...PROJECT, project_id: 'ffffffff-ffff-4fff-8fff-ffffffffffff' }] },
    ]) {
      vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(json(value)))
      await expect(loadProjects(true)).rejects.toThrow('Project list response')
    }
  })

  it('does not mix archived data into the active-only query', async () => {
    const archived = { ...PROJECT, status: 'ARCHIVED' }
    const transport = vi.fn<typeof fetch>().mockImplementation(async () => json({ items: [archived] }))
    vi.stubGlobal('fetch', transport)
    await expect(loadProjects()).rejects.toThrow('requested scope')
    await expect(loadProjects(true)).resolves.toEqual([archived])
  })

  it('stops every request before transport when already aborted', async () => {
    const controller = new AbortController()
    controller.abort()
    const transport = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', transport)
    for (const [, call] of WRITE_CASES) await expect(call(PROJECT.project_id, 7, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
    await expect(createProject(CREATE, CSRF, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
    await expect(deleteProject(PROJECT.project_id, 7, CSRF, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
    await expect(loadProjects(true, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
    expect(transport).not.toHaveBeenCalled()
  })

  it.each(WRITE_CASES)('does not accept late %s success from a transport that ignores abort', async (_name, call, result) => {
    const controller = new AbortController()
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockImplementation(async () => {
      controller.abort()
      return json(result)
    }))
    await expect(call(PROJECT.project_id, 7, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  })
})
