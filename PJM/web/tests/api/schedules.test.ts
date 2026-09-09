import { afterEach, describe, expect, it, vi } from 'vitest'
import example from '../../../contracts/examples/task-schedule.v1.json'
import schema from '../../../contracts/task-schedule/v1.schema.json'

import {
  changeScheduleStatus,
  createSchedule,
  loadProjectSchedules,
  loadSchedule,
  loadSchedulePage,
  previewSchedule,
  updateSchedule,
} from '../../src/api/index'
import type { CreateScheduleInput, UpdateScheduleInput } from '../../src/api/index'

const CSRF = 's'.repeat(32)
const PROJECT_ID = '00000000-0000-4000-8000-000000000020'
const SCHEDULE_ID = '00000000-0000-4000-8000-000000000091'

/** 保存済み schedule の代表 response。 */
const SCHEDULE = {
  schedule_id: SCHEDULE_ID,
  project_id: PROJECT_ID,
  name: 'nightly',
  kind: 'CRON',
  status: 'ACTIVE',
  timezone: 'Asia/Tokyo',
  cron_expression: '0 3 * * *',
  run_at: null,
  end_at: null,
  max_runs: null,
  skill_version_id: '00000000-0000-4000-8000-000000000061',
  task_key: 'analyze',
  input: { ticket: 'T-1' },
  sources: { issues: 'integration:00000000-0000-4000-8000-000000000041' },
  next_run_at: '2026-07-27T18:00:00Z',
  last_run_at: null,
  last_run_id: null,
  last_outcome: null,
  last_error: null,
  run_count: 0,
  missed_count: 0,
  created_by: '00000000-0000-4000-8000-000000000030',
  row_version: 1,
  created_at: '2026-07-26T09:00:00Z',
  updated_at: '2026-07-26T09:00:00Z',
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

/** ページ情報も公開契約であり、省略した mock を成功例にしない。 */
function page(schedules: unknown[] = [SCHEDULE], extra: Record<string, unknown> = {}) {
  return { schedules, total: schedules.length, limit: 100, offset: 0, ...extra }
}

/** 送信する本文と fixture の保存値を一致させ、旧値の返却を成功例にしない。 */
function createInput(): CreateScheduleInput {
  return {
    name: SCHEDULE.name,
    definition: { kind: 'CRON', timezone: SCHEDULE.timezone, cron_expression: SCHEDULE.cron_expression },
    skill_version_id: SCHEDULE.skill_version_id,
    task_key: SCHEDULE.task_key,
    input: { ...SCHEDULE.input }, sources: { ...SCHEDULE.sources },
  }
}

/** PUT は元の版だけを追加し、task identity を変更要求へ混ぜない。 */
function updateInput(): UpdateScheduleInput {
  const { name, definition, input, sources } = createInput()
  return { name, definition, input, sources, expected_row_version: 1 }
}

afterEach(() => vi.unstubAllGlobals())

describe('Schedule API contract', () => {
  it('consumes the shared public example and requires every declared record field', async () => {
    vi.stubGlobal('fetch', jsonFetch(example, 200))
    expect(await loadSchedule(example.project_id, example.schedule_id)).toEqual(example)
    for (const key of schema.$defs.schedule.required) {
      const incomplete: Record<string, unknown> = { ...example }
      delete incomplete[key]
      vi.stubGlobal('fetch', jsonFetch(incomplete, 200))
      await expect(loadSchedule(example.project_id, example.schedule_id)).rejects.toThrow('Schedule response is invalid')
    }
  })
  it('lists project schedules and enforces the record contract', async () => {
    vi.stubGlobal('fetch', jsonFetch(page(), 200))

    const schedules = await loadProjectSchedules(PROJECT_ID)

    expect(schedules).toHaveLength(1)
    expect(schedules[0]?.cron_expression).toBe('0 3 * * *')
  })

  it('rejects a record whose status is outside the contract', async () => {
    // 契約外の値をそのまま画面へ流すと badge も操作 button も意味を失う。
    vi.stubGlobal('fetch', jsonFetch(page([{ ...SCHEDULE, status: 'RUNNING' }]), 200))

    await expect(loadProjectSchedules(PROJECT_ID)).rejects.toThrow('Schedule list is invalid')
  })

  it('sends the CSRF token on every mutation', async () => {
    const fetchMock = jsonFetch(SCHEDULE, 201)
    vi.stubGlobal('fetch', fetchMock)

    await createSchedule(
      PROJECT_ID,
      {
        name: 'nightly',
        definition: { kind: 'CRON', timezone: 'Asia/Tokyo', cron_expression: '0 3 * * *' },
        skill_version_id: SCHEDULE.skill_version_id,
        task_key: 'analyze',
        input: { ...SCHEDULE.input }, sources: { ...SCHEDULE.sources },
      },
      CSRF,
    )
    fetchMock.mockImplementation(jsonFetch({ ...SCHEDULE, row_version: 2 }, 200))
    await updateSchedule(
      PROJECT_ID,
      SCHEDULE_ID,
      {
        name: 'nightly',
        definition: { kind: 'CRON', timezone: 'Asia/Tokyo', cron_expression: '0 3 * * *' },
        expected_row_version: 1,
        input: { ...SCHEDULE.input }, sources: { ...SCHEDULE.sources },
      },
      CSRF,
    )
    fetchMock.mockImplementation(jsonFetch({ ...SCHEDULE, row_version: 2, status: 'PAUSED' }, 200))
    await changeScheduleStatus(PROJECT_ID, SCHEDULE_ID, 'PAUSED', 1, CSRF)

    for (const call of fetchMock.mock.calls) {
      const headers = call[1]?.headers as Record<string, string>
      expect(headers['X-CSRF-Token']).toBe(CSRF)
    }
    expect(fetchMock.mock.calls.map((call) => call[1]?.method)).toEqual(['POST', 'PUT', 'POST'])
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toEqual({ status: 'PAUSED', expected_row_version: 1 })
  })

  it('preserves server pagination and sends status and literal search terms', async () => {
    const fetchMock = jsonFetch(page([{ ...SCHEDULE, status: 'ARCHIVED' }], { limit: 25, offset: 100, total: 101 }), 200)
    vi.stubGlobal('fetch', fetchMock)
    const result = await loadSchedulePage(PROJECT_ID, { q: '  a%_b  ', status: 'ARCHIVED', limit: 25, offset: 100 })
    expect(result.total).toBe(101)
    expect(result.offset).toBe(100)
    expect(new URL(String(fetchMock.mock.calls[0]?.[0]), 'https://example.test').searchParams.toString())
      .toBe('limit=25&offset=100&q=a%25_b&status=ARCHIVED')
    expect(fetchMock.mock.calls[0]?.[1]?.cache).toBe('no-store')
  })

  it.each([{ limit: 0 }, { limit: 101 }, { offset: -1 }, { q: 'a'.repeat(201) }, { q: '\u0000' }])(
    'rejects invalid queries before HTTP %j', async (options) => {
      const fetchMock = jsonFetch(page(), 200)
      vi.stubGlobal('fetch', fetchMock)
      await expect(loadSchedulePage(PROJECT_ID, options)).rejects.toThrow('Schedule query is invalid')
      expect(fetchMock).not.toHaveBeenCalled()
    },
  )

  it('rejects a page outside the requested status', async () => {
    vi.stubGlobal('fetch', jsonFetch(page([], { limit: 25, offset: 100 }), 200))
    expect((await loadSchedulePage(PROJECT_ID, { limit: 25, offset: 100 })).schedules).toEqual([])
    vi.stubGlobal('fetch', jsonFetch(page([SCHEDULE], { limit: 25 }), 200))
    await expect(loadSchedulePage(PROJECT_ID, { status: 'ARCHIVED', limit: 25 })).rejects.toThrow('Schedule list is invalid')
  })

  it('reads beyond the first one hundred records for task summaries', async () => {
    const records = Array.from({ length: 101 }, (_, i) => ({ ...SCHEDULE,
      schedule_id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`,
    }))
    const fetchMock = jsonFetch(page(records.slice(0, 100), { total: 101 }), 200)
    fetchMock.mockImplementationOnce(jsonFetch(page(records.slice(0, 100), { total: 101 }), 200))
      .mockImplementationOnce(jsonFetch(page(records.slice(100), { total: 101, offset: 100 }), 200))
    vi.stubGlobal('fetch', fetchMock)
    expect(await loadProjectSchedules(PROJECT_ID)).toHaveLength(101)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[1]?.[0]).toContain('offset=100')
  })

  it.each([
    { total: -1 }, { total: 0 }, { total: 1.2 }, { total: true }, { total: Number.MAX_SAFE_INTEGER + 1 },
    { limit: 99 }, { offset: 1 }, { extra: true }, { schedules: [] },
    { schedules: [SCHEDULE, SCHEDULE], total: 2 },
    { schedules: [{ ...SCHEDULE, project_id: SCHEDULE_ID }] },
  ])('rejects incomplete or mismatched page facts %j', async (extra) => {
    vi.stubGlobal('fetch', jsonFetch(page([SCHEDULE], extra), 200))
    await expect(loadSchedulePage(PROJECT_ID, { limit: 100 })).rejects.toThrow('Schedule list is invalid')
  })

  it('rejects a legacy response missing pagination rather than defaulting it to a complete list', async () => {
    vi.stubGlobal('fetch', jsonFetch({ schedules: [SCHEDULE] }, 200))
    await expect(loadProjectSchedules(PROJECT_ID)).rejects.toThrow('Schedule list is invalid')
  })

  it.each([{ total: 3 }, { schedules: [SCHEDULE] }])('rejects changing pages instead of returning an incomplete summary %j', async (extra) => {
    const fetchMock = jsonFetch(page([SCHEDULE], { total: 2 }), 200)
    fetchMock.mockImplementationOnce(jsonFetch(page([SCHEDULE], { total: 2 }), 200))
      .mockImplementationOnce(jsonFetch(page([{ ...SCHEDULE, schedule_id: PROJECT_ID }], { total: 2, offset: 1, ...extra }), 200))
    vi.stubGlobal('fetch', fetchMock)
    await expect(loadProjectSchedules(PROJECT_ID)).rejects.toThrow('Schedule list changed while reading')
  })

  it.each([
    { run_count: -1 }, { missed_count: 0.5 }, { row_version: true }, { row_version: 0 },
    { run_count: Number.MAX_SAFE_INTEGER + 1 }, { max_runs: 0 }, { sources: { issues: 1 } },
    { created_at: '2027-02-29T03:00:00Z' }, { run_at: '2027-01-01T03:00:00' },
    { last_run_id: 'not-a-uuid' }, { skill_version_id: 'not-a-uuid' }, { created_by: null },
    { end_at: false }, { last_error: {} }, { last_outcome: 'PENDING' }, { worker_id: 'private' },
  ])('rejects malformed or private schedule fields %j', async (extra) => {
    vi.stubGlobal('fetch', jsonFetch({ ...SCHEDULE, ...extra }, 200))
    await expect(loadSchedule(PROJECT_ID, SCHEDULE_ID)).rejects.toThrow('Schedule response is invalid')
  })

  it('does not require last_run_id and last_outcome to describe the same occurrence', async () => {
    const record = { ...SCHEDULE, last_run_id: PROJECT_ID, last_run_at: '2026-07-26T09:00:00Z',
      last_outcome: 'SKIPPED_OVERLAP', run_count: 1 }
    vi.stubGlobal('fetch', jsonFetch(record, 200))
    expect(await loadSchedule(PROJECT_ID, SCHEDULE_ID)).toEqual(record)
  })

  it.each([{ schedule_id: PROJECT_ID }, { project_id: SCHEDULE_ID }])('rejects a different valid identity %j', async (extra) => {
    vi.stubGlobal('fetch', jsonFetch({ ...SCHEDULE, ...extra }, 200))
    await expect(loadSchedule(PROJECT_ID, SCHEDULE_ID)).rejects.toThrow('requested identity')
  })

  it.each([1, 3])('does not treat response row version %s as CAS from version 1', async (row_version) => {
    vi.stubGlobal('fetch', jsonFetch({ ...SCHEDULE, row_version, status: 'PAUSED' }, 200))
    await expect(changeScheduleStatus(PROJECT_ID, SCHEDULE_ID, 'PAUSED', 1, CSRF)).rejects.toThrow('requested identity or version')
  })

  it.each([0, 1.5, Number.NaN, Number.MAX_SAFE_INTEGER + 1])('rejects invalid outgoing CAS %s before HTTP', async (version) => {
    const fetchMock = jsonFetch(SCHEDULE, 200)
    vi.stubGlobal('fetch', fetchMock)
    await expect(changeScheduleStatus(PROJECT_ID, SCHEDULE_ID, 'PAUSED', version, CSRF)).rejects.toThrow('Invalid schedule version')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('does not accept an unchanged status as a successful pause', async () => {
    vi.stubGlobal('fetch', jsonFetch({ ...SCHEDULE, row_version: 2 }, 200))
    await expect(changeScheduleStatus(PROJECT_ID, SCHEDULE_ID, 'PAUSED', 1, CSRF)).rejects.toThrow('requested status')
  })

  it.each(['create', 'update'] as const)('does not accept a different saved intent after %s', async (operation) => {
    const changes = [
      { name: 'other' }, { input: { ticket: 'other' } }, { sources: {} },
      { kind: 'ONCE' }, { timezone: 'UTC' }, { cron_expression: '0 4 * * *' },
      { run_at: '2030-01-01T00:00:00Z' }, { end_at: '2031-01-01T00:00:00Z' }, { max_runs: 10 },
    ]
    for (const change of changes) {
      vi.stubGlobal('fetch', jsonFetch({ ...SCHEDULE, row_version: operation === 'create' ? 1 : 2, ...change }, operation === 'create' ? 201 : 200))
      const result = operation === 'create'
        ? createSchedule(PROJECT_ID, createInput(), CSRF)
        : updateSchedule(PROJECT_ID, SCHEDULE_ID, updateInput(), CSRF)
      await expect(result).rejects.toThrow(operation === 'create' ? 'requested creation' : 'requested update')
    }
  })

  it.each(['create', 'update'] as const)('treats omitted input and sources as empty objects on %s', async (operation) => {
    const input = operation === 'create' ? createInput() : updateInput()
    delete input.input
    delete input.sources
    const invoke = () => operation === 'create'
      ? createSchedule(PROJECT_ID, input as CreateScheduleInput, CSRF)
      : updateSchedule(PROJECT_ID, SCHEDULE_ID, input as UpdateScheduleInput, CSRF)
    const record = { ...SCHEDULE, row_version: operation === 'create' ? 1 : 2 }
    vi.stubGlobal('fetch', jsonFetch(record, operation === 'create' ? 201 : 200))
    await expect(invoke()).rejects.toThrow('did not match')
    vi.stubGlobal('fetch', jsonFetch({ ...record, input: {}, sources: {} }, operation === 'create' ? 201 : 200))
    expect((await invoke()).input).toEqual({})
  })

  it('matches Python name and cron whitespace without rewriting the transmitted strings', async () => {
    const input = createInput()
    input.name = '\u001c \u0085\ufeffnightly\ufeff\u3000\u001f'
    input.definition.cron_expression = '\u0085 0\u001c3\t*\n*\u3000*\u001f'
    const response = { ...SCHEDULE, name: '\ufeffnightly\ufeff' }
    const fetchMock = jsonFetch(response, 201)
    vi.stubGlobal('fetch', fetchMock)
    expect(await createSchedule(PROJECT_ID, input, CSRF)).toEqual(response)
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual(input)
  })

  it('compares ONCE UTC minute floor and offset-equivalent end_at without losing microseconds', async () => {
    const input = updateInput()
    input.definition = {
      kind: 'ONCE', timezone: 'Asia/Tokyo', cron_expression: '',
      run_at: '2030-01-01T12:34:59.999999+09:00',
      end_at: '2030-01-02T09:00:00.1234567+09:00', max_runs: 1,
    }
    const response = { ...SCHEDULE, kind: 'ONCE', cron_expression: null, row_version: 2,
      run_at: '2030-01-01T03:34:00Z', end_at: '2030-01-01T19:00:00.123456-05:00', max_runs: 1 }
    vi.stubGlobal('fetch', jsonFetch(response, 200))
    expect(await updateSchedule(PROJECT_ID, SCHEDULE_ID, input, CSRF)).toEqual(response)
    for (const change of [{ run_at: '2030-01-01T03:35:00Z' }, { end_at: '2030-01-02T00:00:00.123457Z' }]) {
      vi.stubGlobal('fetch', jsonFetch({ ...response, ...change }, 200))
      await expect(updateSchedule(PROJECT_ID, SCHEDULE_ID, input, CSRF)).rejects.toThrow('requested update')
    }
  })

  it('preserves object key equivalence but not array ordering or scalar types', async () => {
    const input = createInput()
    input.input = { nested: { first: 1, second: true }, choices: ['a', 'b'] }
    input.sources = { first: 'one', second: 'two' }
    const response = { ...SCHEDULE, input: { choices: ['a', 'b'], nested: { second: true, first: 1 } }, sources: { second: 'two', first: 'one' } }
    vi.stubGlobal('fetch', jsonFetch(response, 201))
    expect(await createSchedule(PROJECT_ID, input, CSRF)).toEqual(response)
    for (const change of [{ nested: { first: true, second: true }, choices: ['a', 'b'] }, { nested: { first: 1, second: true }, choices: ['b', 'a'] }]) {
      vi.stubGlobal('fetch', jsonFetch({ ...response, input: change }, 201))
      await expect(createSchedule(PROJECT_ID, input, CSRF)).rejects.toThrow('requested creation')
    }
  })

  it('accepts nested JSON, repeated references and literal __proto__ keys without altering the request', async () => {
    const shared = { flag: false, ratio: 0.25, empty: null }
    const input = createInput()
    input.input = { first: shared, second: shared, list: [shared, 'text', 0] }
    Object.defineProperty(input.input, '__proto__', { value: { literal: true }, enumerable: true })
    const response = { ...SCHEDULE, input: input.input }
    const fetchMock = jsonFetch(response, 201)
    vi.stubGlobal('fetch', fetchMock)
    const saved = await createSchedule(PROJECT_ID, input, CSRF)
    expect(saved.input).toEqual(JSON.parse(JSON.stringify(input.input)))
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual(input)
  })

  it.each([Number.NaN, Infinity, -Infinity, 0, -1, 1.5, 100_001, true, '1'])(
    'rejects invalid outgoing quota %s before create, update or preview HTTP', async (maximum) => {
      const fetchMock = jsonFetch(SCHEDULE, 201)
      vi.stubGlobal('fetch', fetchMock)
      const create = createInput()
      create.definition.max_runs = maximum as number
      const update = updateInput()
      update.definition = create.definition
      await expect(createSchedule(PROJECT_ID, create, CSRF)).rejects.toThrow('Schedule request is invalid')
      await expect(updateSchedule(PROJECT_ID, SCHEDULE_ID, update, CSRF)).rejects.toThrow('Schedule request is invalid')
      await expect(previewSchedule(PROJECT_ID, create.definition, CSRF)).rejects.toThrow('Schedule request is invalid')
      expect(fetchMock).not.toHaveBeenCalled()
    },
  )

  it.each([Number.NaN, Infinity, -Infinity, undefined, 1n, Symbol('non-json'), () => 1,
    new Date('2030-01-01T00:00:00Z'), [undefined], Array(1), Object.create({ inherited: 1 }), { toJSON: () => null }])(
    'rejects values JSON serialization would transform or discard: %s', async (value) => {
      const fetchMock = jsonFetch(SCHEDULE, 201)
      vi.stubGlobal('fetch', fetchMock)
      await expect(createSchedule(PROJECT_ID, { ...createInput(), input: { nested: value } }, CSRF)).rejects.toThrow('Schedule request is invalid')
      await expect(updateSchedule(PROJECT_ID, SCHEDULE_ID, { ...updateInput(), input: { nested: value } }, CSRF)).rejects.toThrow('Schedule request is invalid')
      expect(fetchMock).not.toHaveBeenCalled()
    },
  )

  it.each([null, [], { invalid: 1 }, { invalid: undefined }])('requires a JSON string map for sources: %j', async (sources) => {
    const fetchMock = jsonFetch(SCHEDULE, 201)
    vi.stubGlobal('fetch', fetchMock)
    await expect(createSchedule(PROJECT_ID, { ...createInput(), sources: sources as unknown as Record<string, string> }, CSRF)).rejects.toThrow('Schedule request is invalid')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects cycles and accessors before evaluating a getter or sending HTTP', async () => {
    const fetchMock = jsonFetch(SCHEDULE, 201)
    vi.stubGlobal('fetch', fetchMock)
    const cycle: Record<string, unknown> = {}
    cycle.self = cycle
    const getter = vi.fn(() => 'changed')
    const accessor = Object.defineProperty({}, 'hidden', { enumerable: true, get: getter })
    for (const input of [cycle, accessor]) {
      await expect(createSchedule(PROJECT_ID, { ...createInput(), input }, CSRF)).rejects.toThrow('Schedule request is invalid')
    }
    expect(getter).not.toHaveBeenCalled()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it.each(['create', 'update'] as const)('compares the frozen sent intent after caller mutation during %s await', async (operation) => {
    const input = operation === 'create' ? createInput() : updateInput()
    const fetchMock = vi.fn(async () => {
      input.name = 'changed'
      input.input!.ticket = 'changed'
      input.sources!.issues = 'changed'
      input.definition.cron_expression = '0 4 * * *'
      input.definition.max_runs = 100
      if ('expected_row_version' in input) input.expected_row_version = 5
      return new Response(JSON.stringify({ ...SCHEDULE, row_version: operation === 'create' ? 1 : 2 }), { status: operation === 'create' ? 201 : 200 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const saved = operation === 'create'
      ? await createSchedule(PROJECT_ID, input as CreateScheduleInput, CSRF)
      : await updateSchedule(PROJECT_ID, SCHEDULE_ID, input as UpdateScheduleInput, CSRF)
    expect(saved.name).toBe('nightly')
    expect(saved.input).toEqual(SCHEDULE.input)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not accept a response matching a draft mutated after the original request was sent', async () => {
    const input = createInput()
    input.input = { nested: { value: 1 } }
    vi.stubGlobal('fetch', vi.fn(async () => {
      const nested = input.input!.nested as { value: number }
      nested.value = 2
      return new Response(JSON.stringify({ ...SCHEDULE, input: input.input }), { status: 201 })
    }))
    await expect(createSchedule(PROJECT_ID, input, CSRF)).rejects.toThrow('requested creation')
  })

  it('retains the original ONCE shape when the preview caller mutates its definition during await', async () => {
    const definition: CreateScheduleInput['definition'] = { kind: 'ONCE', timezone: 'UTC', run_at: '2030-01-01T00:00:00Z' }
    vi.stubGlobal('fetch', vi.fn(async () => {
      definition.kind = 'CRON'
      return new Response(JSON.stringify({ occurrences: ['2030-01-01T00:00:00Z', '2030-01-02T00:00:00Z'] }), { status: 200 })
    }))
    await expect(previewSchedule(PROJECT_ID, definition, CSRF)).rejects.toThrow('Schedule preview is invalid')
  })

  it('accepts UUID case differences without changing the sent task identity', async () => {
    const projectId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    const skillVersionId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    const input = { ...createInput(), skill_version_id: skillVersionId.toUpperCase() }
    vi.stubGlobal('fetch', jsonFetch({ ...SCHEDULE, project_id: projectId.toUpperCase(), skill_version_id: skillVersionId }, 201))
    expect((await createSchedule(projectId, input, CSRF)).skill_version_id).toBe(skillVersionId)
  })

  it.each([undefined, true, 0, 1.5, Number.NaN, Number.MAX_SAFE_INTEGER + 1])('rejects invalid update CAS %s without sending HTTP', async (version) => {
    const fetchMock = jsonFetch({ ...SCHEDULE, row_version: 2 }, 200)
    vi.stubGlobal('fetch', fetchMock)
    await expect(updateSchedule(PROJECT_ID, SCHEDULE_ID, { ...updateInput(), expected_row_version: version as number }, CSRF)).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('counts name code points rather than UTF-16 units', async () => {
    const input = { ...createInput(), name: '😀'.repeat(200) }
    vi.stubGlobal('fetch', jsonFetch({ ...SCHEDULE, name: input.name }, 201))
    expect((await createSchedule(PROJECT_ID, input, CSRF)).name).toBe(input.name)
  })

  it.each([200, 202])('requires create HTTP 201 instead of %s', async (status) => {
    vi.stubGlobal('fetch', jsonFetch(SCHEDULE, status))
    await expect(createSchedule(PROJECT_ID, createInput(), CSRF)).rejects.toThrow('unexpected success status')
  })

  it.each([201, 202])('requires update and status HTTP 200 instead of %s', async (status) => {
    vi.stubGlobal('fetch', jsonFetch({ ...SCHEDULE, row_version: 2, status: 'PAUSED' }, status))
    await expect(updateSchedule(PROJECT_ID, SCHEDULE_ID, updateInput(), CSRF)).rejects.toThrow('unexpected success status')
    await expect(changeScheduleStatus(PROJECT_ID, SCHEDULE_ID, 'PAUSED', 1, CSRF)).rejects.toThrow('unexpected success status')
  })

  it('does not deliver a late response from a transport ignoring abort', async () => {
    const controller = new AbortController()
    vi.stubGlobal('fetch', vi.fn(async () => {
      controller.abort()
      return new Response(JSON.stringify(SCHEDULE), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    await expect(loadSchedule(PROJECT_ID, SCHEDULE_ID, controller.signal)).rejects.toThrow()
  })

  it.each([201, 202])('rejects unexpected GET success HTTP %s', async (status) => {
    vi.stubGlobal('fetch', jsonFetch(SCHEDULE, status))
    await expect(loadSchedule(PROJECT_ID, SCHEDULE_ID)).rejects.toThrow()
  })

  it('returns the previewed occurrences without creating anything', async () => {
    const fetchMock = jsonFetch(
      { occurrences: ['2026-07-27T18:00:00Z', '2026-07-28T18:00:00Z', '2026-07-29T18:00:00Z'] },
      200,
    )
    vi.stubGlobal('fetch', fetchMock)

    const occurrences = await previewSchedule(
      PROJECT_ID,
      { kind: 'CRON', timezone: 'Asia/Tokyo', cron_expression: '0 3 * * *' },
      CSRF,
    )

    // 上限以内の三件も正当。通常の五件と有限規則の一〜二件は別 test で許可する。
    expect(occurrences).toHaveLength(3)
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain('/schedules/preview')
  })

  it('rejects a preview response that is not a list of instants', async () => {
    vi.stubGlobal('fetch', jsonFetch({ occurrences: [1, 2, 3] }, 200))

    await expect(previewSchedule(
      PROJECT_ID,
      { kind: 'CRON', timezone: 'UTC', cron_expression: '0 3 * * *' },
      CSRF,
    )).rejects.toThrow('Schedule preview is invalid')
  })

  it.each([
    { occurrences: [] },
    { occurrences: ['2027-01-01T03:00:00'] },
    { occurrences: ['2027-02-29T03:00:00Z'] },
    { occurrences: ['2027-01-01T03:00:00Z', '2027-01-01T12:00:00+09:00'] },
    { occurrences: ['2027-01-02T03:00:00Z', '2027-01-01T03:00:00Z'] },
    { occurrences: ['2027-01-01T03:00:00Z'], extra: true },
    { occurrences: Array.from({ length: 6 }, (_, index) => `2027-01-0${index + 1}T03:00:00Z`) },
  ])('rejects ambiguous or malformed preview facts %j', async (body) => {
    vi.stubGlobal('fetch', jsonFetch(body, 200))
    await expect(previewSchedule(PROJECT_ID, { kind: 'CRON', timezone: 'UTC', cron_expression: '0 3 * * *' }, CSRF))
      .rejects.toThrow('Schedule preview is invalid')
  })

  it.each([201, 202])('does not accept unexpected preview HTTP %s', async (status) => {
    vi.stubGlobal('fetch', jsonFetch({ occurrences: ['2027-01-01T03:00:00Z'] }, status))
    await expect(previewSchedule(PROJECT_ID, { kind: 'CRON', timezone: 'UTC', cron_expression: '0 3 * * *' }, CSRF)).rejects.toThrow()
  })

  it.each([1, 2, 5])('accepts %s legal CRON candidates without inventing more', async (count) => {
    const occurrences = Array.from({ length: count }, (_, index) => `2027-01-0${index + 1}T03:00:00Z`)
    vi.stubGlobal('fetch', jsonFetch({ occurrences }, 200))
    expect(await previewSchedule(PROJECT_ID, { kind: 'CRON', timezone: 'UTC', cron_expression: '0 3 * * *', max_runs: count }, CSRF))
      .toEqual(occurrences)
  })

  it('requires exactly one ONCE candidate', async () => {
    vi.stubGlobal('fetch', jsonFetch({ occurrences: ['2027-01-01T03:00:00Z', '2027-01-02T03:00:00Z'] }, 200))
    await expect(previewSchedule(PROJECT_ID, { kind: 'ONCE', timezone: 'UTC', run_at: '2027-01-01T03:00:00Z' }, CSRF))
      .rejects.toThrow('Schedule preview is invalid')
  })
})
