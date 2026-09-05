import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  changeScheduleStatus,
  createSchedule,
  loadProjectSchedules,
  previewSchedule,
  updateSchedule,
} from '../../src/api/index'

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

afterEach(() => vi.unstubAllGlobals())

describe('Schedule API contract', () => {
  it('lists project schedules and enforces the record contract', async () => {
    vi.stubGlobal('fetch', jsonFetch({ schedules: [SCHEDULE] }, 200))

    const schedules = await loadProjectSchedules(PROJECT_ID)

    expect(schedules).toHaveLength(1)
    expect(schedules[0]?.cron_expression).toBe('0 3 * * *')
  })

  it('rejects a record whose status is outside the contract', async () => {
    // 契約外の値をそのまま画面へ流すと badge も操作 button も意味を失う。
    vi.stubGlobal('fetch', jsonFetch({ schedules: [{ ...SCHEDULE, status: 'RUNNING' }] }, 200))

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
      },
      CSRF,
    )
    await updateSchedule(
      PROJECT_ID,
      SCHEDULE_ID,
      {
        name: 'nightly',
        definition: { kind: 'CRON', timezone: 'Asia/Tokyo', cron_expression: '0 3 * * *' },
        expected_row_version: 1,
      },
      CSRF,
    )
    await changeScheduleStatus(PROJECT_ID, SCHEDULE_ID, 'PAUSED', CSRF)

    for (const call of fetchMock.mock.calls) {
      const headers = call[1]?.headers as Record<string, string>
      expect(headers['X-CSRF-Token']).toBe(CSRF)
    }
    expect(fetchMock.mock.calls.map((call) => call[1]?.method)).toEqual(['POST', 'PUT', 'POST'])
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

    // docs/07 §8.3 は保存前に少なくとも三次の提示を求める。
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
})
