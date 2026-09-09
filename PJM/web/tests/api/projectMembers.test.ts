import { afterEach, describe, expect, it, vi } from 'vitest'

import { addProjectMember, loadProjectMembers, removeProjectMember, type ProjectMemberRecord } from '../../src/api'
import { DEMO_PROJECT } from '../fixtures'

/** 公開済み架空 membership。関係の ACTIVE は account の ACTIVE を意味しない。 */
const member: ProjectMemberRecord = {
  user_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', email: 'member@example.invalid',
  display_name: 'Example member', status: 'ACTIVE', joined_at: '2026-09-01T01:02:03Z',
}
const projectId = DEMO_PROJECT.project_id

/** 実 credential、Server や DB に接続しない JSON response。 */
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => vi.unstubAllGlobals())

describe('project membership client boundaries', () => {
  it('reads all membership states with no-store without inventing an account status', async () => {
    const items = [member, { ...member, user_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', status: 'REMOVED' }]
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({ items }))
    vi.stubGlobal('fetch', fetcher)
    expect(await loadProjectMembers(projectId)).toEqual(items)
    expect(fetcher.mock.calls[0]![1]).toMatchObject({ cache: 'no-store', credentials: 'same-origin' })
  })

  it('adds the exact user and validates the returned active identity', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(member))
    vi.stubGlobal('fetch', fetcher)
    await expect(addProjectMember(projectId, member.user_id.toUpperCase(), 'fixture-csrf')).resolves.toEqual(member)
    expect(fetcher.mock.calls[0]![0]).toContain(member.user_id.toUpperCase())
    expect(fetcher.mock.calls[0]![1]).toMatchObject({ method: 'PUT', cache: 'no-store' })
    expect(new Headers(fetcher.mock.calls[0]![1]?.headers).get('X-CSRF-Token')).toBe('fixture-csrf')
  })

  it.each([
    { ...member, user_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' },
    { ...member, status: 'REMOVED' },
    { ...member, user_id: 'wrong' },
    { ...member, display_name: '' },
    { ...member, joined_at: 'yesterday' },
    { ...member, email: 'invalid' },
    { ...member, password_hash: 'must-not-pass-contract' },
  ])('rejects an invalid addition response without treating it as success', async (value) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(json(value)))
    await expect(addProjectMember(projectId, member.user_id, 'fixture-csrf')).rejects.toThrow()
  })

  it.each([
    { items: [member], extra: true },
    { items: [member, { ...member, user_id: member.user_id.toUpperCase() }] },
    { items: [{ ...member, status: 'DISABLED' }] },
    { items: null },
  ])('rejects incompatible member lists and duplicate identities', async (value) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(json(value)))
    await expect(loadProjectMembers(projectId)).rejects.toThrow()
  })

  it.each(['', 'not-a-uuid', '../other'])('refuses invalid identities before any HTTP: %s', async (invalid) => {
    const fetcher = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetcher)
    await expect(loadProjectMembers(invalid)).rejects.toThrow()
    await expect(addProjectMember(projectId, invalid, 'fixture-csrf')).rejects.toThrow()
    await expect(removeProjectMember(invalid, member.user_id, 'fixture-csrf')).rejects.toThrow()
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('requires the documented 204 receipt for removal and preserves CSRF', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetcher)
    await expect(removeProjectMember(projectId, member.user_id, 'fixture-csrf')).resolves.toBeUndefined()
    expect(fetcher.mock.calls[0]![1]).toMatchObject({ method: 'DELETE', cache: 'no-store' })
    expect(new Headers(fetcher.mock.calls[0]![1]?.headers).get('X-CSRF-Token')).toBe('fixture-csrf')
    fetcher.mockResolvedValueOnce(json({}, 202))
    await expect(removeProjectMember(projectId, member.user_id, 'fixture-csrf')).rejects.toMatchObject({ status: 202 })
  })

  it.each(['read', 'add', 'remove'])('does not dispatch an already aborted %s', async (operation) => {
    const controller = new AbortController()
    controller.abort()
    const fetcher = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetcher)
    const result = operation === 'read' ? loadProjectMembers(projectId, controller.signal)
      : operation === 'add' ? addProjectMember(projectId, member.user_id, 'fixture-csrf', controller.signal)
        : removeProjectMember(projectId, member.user_id, 'fixture-csrf', controller.signal)
    await expect(result).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetcher).not.toHaveBeenCalled()
  })

  it.each(['read', 'add', 'remove'])('discards a late %s even when transport ignores abort', async (operation) => {
    const controller = new AbortController()
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockImplementation(async () => {
      controller.abort()
      return operation === 'remove' ? new Response(null, { status: 204 }) : json(operation === 'add' ? member : { items: [member] })
    }))
    const result = operation === 'read' ? loadProjectMembers(projectId, controller.signal)
      : operation === 'add' ? addProjectMember(projectId, member.user_id, 'fixture-csrf', controller.signal)
        : removeProjectMember(projectId, member.user_id, 'fixture-csrf', controller.signal)
    await expect(result).rejects.toMatchObject({ name: 'AbortError' })
  })
})
