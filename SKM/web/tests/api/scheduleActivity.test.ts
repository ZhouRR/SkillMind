import { afterEach, describe, expect, it, vi } from 'vitest'

import pending from '../../../contracts/examples/task-schedule-activity.v1.json'
import empty from '../../../contracts/examples/task-schedule-activity-empty.v1.json'
import legacy from '../../../contracts/examples/task-schedule-activity-legacy.v1.json'
import schema from '../../../contracts/task-schedule/v1.schema.json'
import { ApiProblemError, loadScheduleActivity } from '../../src/api'
import { apiTimestampMicroseconds } from '../../src/lib/validation'

/** 本番 HTTP 境界に JSON response を渡し、未知 field を型 assertion で隠さない。 */
function mockResponse(body: unknown, status = 200) {
  const fetch = vi.fn(async () => new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  }))
  vi.stubGlobal('fetch', fetch)
  return fetch
}

/** 同じ原 Project/Schedule の読取だけを呼ぶ。 */
function read(signal?: AbortSignal) {
  return loadScheduleActivity(pending.project_id, pending.schedule_id, signal)
}

afterEach(() => vi.unstubAllGlobals())

describe('read-only schedule activity contract', () => {
  it.each([pending, empty, legacy])('accepts a shared $tracking example without inferring Run history', async (example) => {
    const fetch = mockResponse(example)
    expect(await read()).toEqual(example)
    expect(fetch).toHaveBeenCalledExactlyOnceWith(
      expect.stringContaining(`/projects/${pending.project_id}/schedules/${pending.schedule_id}/activity`),
      expect.objectContaining({ cache: 'no-store', credentials: 'same-origin' }),
    )
  })
  it('requires every root and nested field from the declared schema', async () => {
    for (const field of schema.$defs.activity.required) {
      const value: Record<string, unknown> = structuredClone(pending)
      delete value[field]; mockResponse(value)
      await expect(read()).rejects.toThrow('Schedule activity is invalid')
    }
    for (const field of schema.$defs.activity_pending.required) {
      const value: Record<string, unknown> = { ...pending.pending }
      delete value[field]; mockResponse({ ...pending, pending: value })
      await expect(read()).rejects.toThrow('Schedule activity is invalid')
    }
  })
  it.each([
    { schedule_id: '00000000-0000-4000-8000-000000000099' },
    { project_id: '00000000-0000-4000-8000-000000000099' },
    { row_version: true }, { configuration_version: 0 }, { automatic_attempt_limit: 0 },
    { automatic_attempt_limit: 3.5 }, { row_version: Number.MAX_SAFE_INTEGER + 1 },
    { tracking: 'UNKNOWN' }, { tracking: ['TRACKED'] }, { tracking: 'LEGACY_UNAVAILABLE' },
    { checked_at: '2035-01-01T12:00:00' }, { checked_at: '2035-02-29T12:00:00Z' },
    { checked_at: '0000-01-01T12:00:00Z' }, { worker_id: 'private-worker' },
    { snapshot_json: { private: 'not public' } }, { pending: [] },
  ])('rejects malformed or cross-scope root facts %#', async (change) => {
    mockResponse({ ...pending, ...change })
    await expect(read()).rejects.toThrow('Schedule activity is invalid')
  })
  it.each([
    { occurrence_id: 'invalid' }, { attempt_count: true }, { attempt_count: -1 },
    { configuration_version: 3 }, { configuration_version: 0.5 },
    { lease_expires_at: null }, { lease_expires_at: '2035-01-01T12:00:00' },
    { occurrence_at: '2035-02-29T12:00:00Z' },
    { created_at: 'invalid' }, { updated_at: 'invalid' },
    { worker_id: 'private' }, { lease_token_hash: 'private' },
    { idempotency_key: 'private' }, { snapshot_json: {} }, { run_id: null },
  ])('rejects malformed or internal pending facts %#', async (change) => {
    mockResponse({ ...pending, pending: { ...pending.pending, ...change } })
    await expect(read()).rejects.toThrow('Schedule activity is invalid')
  })
  it('accepts UUID case differences and attempts exceeding a changed policy limit', async () => {
    const example = { ...pending, automatic_attempt_limit: 1 }
    mockResponse(example)
    expect(await loadScheduleActivity(pending.project_id.toUpperCase(), pending.schedule_id.toUpperCase())).toEqual(example)
  })
  it('does not cache a recovery behind the unchanged parent row version', async () => {
    mockResponse(pending); const first = await read()
    const takeover = { ...pending, pending: { ...pending.pending, attempt_count: 4, lease_expires_at: '2035-01-01T12:06:00Z' } }
    const fetch = mockResponse(takeover)
    expect(await read()).toEqual(takeover)
    expect(takeover.row_version).toBe(first.row_version)
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it.each([201, 202, 206])('does not treat HTTP %s as an authoritative activity read', async (status) => {
    mockResponse(pending, status)
    await expect(read()).rejects.toThrow()
  })
  it.each([401, 403, 404, 409, 500])('keeps HTTP %s failure rather than inventing an empty observation', async (status) => {
    mockResponse({ title: 'Unavailable', status, detail: 'Synthetic failure', code: 'schedule_activity_unavailable' }, status)
    await expect(read()).rejects.toBeInstanceOf(ApiProblemError)
  })
  it('rejects invalid request identities before any transport', async () => {
    const fetch = mockResponse(pending)
    await expect(loadScheduleActivity('invalid', pending.schedule_id)).rejects.toThrow()
    await expect(loadScheduleActivity(pending.project_id, '../activity')).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
  })
  it('honors pre-abort and an abort-ignoring late response', async () => {
    const controller = new AbortController()
    const fetch = mockResponse(pending)
    controller.abort(); await expect(read(controller.signal)).rejects.toThrow()
    expect(fetch).not.toHaveBeenCalled()
    const late = new AbortController()
    let finish!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => { finish = resolve })))
    const operation = read(late.signal)
    late.abort(); finish(new Response(JSON.stringify(pending), { headers: { 'Content-Type': 'application/json' } }))
    await expect(operation).rejects.toThrow()
  })
})

describe('shared exact API timestamp comparison', () => {
  it('preserves microseconds, equivalent offsets, and pre-epoch instants', () => {
    expect(apiTimestampMicroseconds('2035-01-01T12:00:00.000001Z') - apiTimestampMicroseconds('2035-01-01T21:00:00+09:00')).toBe(1n)
    expect(apiTimestampMicroseconds('1969-12-31T23:59:59.999999Z')).toBe(-1n)
    expect(apiTimestampMicroseconds('2035-01-01T12:00:00.1234567Z')).toBe(apiTimestampMicroseconds('2035-01-01T12:00:00.123456Z'))
  })
})
