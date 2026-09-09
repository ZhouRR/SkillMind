import { describe, expect, it } from 'vitest'

import {
  formatScheduleTimestamp, localScheduleInstants, preservedScheduleDefinition, scheduleDefinition,
  scheduleTimeDraft, selectedScheduleInstant,
  type ScheduleTimeDraft,
} from '../../src/lib/scheduleTime'
import { scheduleFixture } from '../fixtures/schedule'

const DRAFT: ScheduleTimeDraft = {
  kind: 'CRON', timezone: 'Asia/Tokyo', cronExpression: '0 3 * * *',
  runAt: '', runAtChoice: '', endAt: '', endAtChoice: '', maxRuns: '',
}

describe('schedule time interpretation', () => {
  it.each([
    ['UTC', '2027-01-02T03:04', '2027-01-02T03:04:00.000Z', 'UTC+00:00'],
    ['Asia/Tokyo', '2027-01-02T03:04', '2027-01-01T18:04:00.000Z', 'UTC+09:00'],
    ['Asia/Kathmandu', '2027-01-02T03:04', '2027-01-01T21:19:00.000Z', 'UTC+05:45'],
  ])('resolves the explicit input zone %s', (zone, input, instant, offset) => {
    expect(localScheduleInstants(input!, zone!)).toEqual([{ instant, offset }])
  })

  it.each(['', '2027-02-29T12:00', '2026-04-31T12:00', '2027-01-01T24:00',
    '0000-01-01T00:00', '2027-01-01', '2027-01-01T01:00Z'])('rejects invalid local input %s', (value) => {
    expect(localScheduleInstants(value, 'UTC')).toEqual([])
  })

  it.each([
    ['America/New_York', '2027-03-14T02:30'],
    ['Australia/Lord_Howe', '2027-10-03T02:15'],
    ['Invalid/Zone', '2027-01-01T00:00'],
  ])('does not normalize an absent local instant in %s', (zone, value) => {
    expect(localScheduleInstants(value!, zone!)).toEqual([])
  })

  it('exposes both DST fold offsets without choosing the first one', () => {
    const options = localScheduleInstants('2027-11-07T01:30', 'America/New_York')
    expect(options).toEqual([
      { instant: '2027-11-07T05:30:00.000Z', offset: 'UTC-04:00' },
      { instant: '2027-11-07T06:30:00.000Z', offset: 'UTC-05:00' },
    ])
    expect(selectedScheduleInstant(options, '')).toBeNull()
    expect(selectedScheduleInstant(options, options[1]!.instant)).toBe(options[1]!.instant)
    expect(selectedScheduleInstant(options, '2027-11-08T06:30:00Z')).toBeNull()
  })

  it('detects the half-hour fold too', () => {
    expect(localScheduleInstants('2027-04-04T01:45', 'Australia/Lord_Howe').map((item) => item.offset))
      .toEqual(['UTC+11:00', 'UTC+10:30'])
  })

  it.each([
    ['2027-11-07T05:30:00Z', 'America/New_York', '2027-11-07 01:30 UTC-04:00 · America/New_York'],
    ['2027-11-07T06:30:00Z', 'America/New_York', '2027-11-07 01:30 UTC-05:00 · America/New_York'],
    ['2027-01-01T18:04:00+00:00', 'Asia/Tokyo', '2027-01-02 03:04 UTC+09:00 · Asia/Tokyo'],
    ['2027-01-01T18:04:00', 'UTC', '—'],
    ['2027-01-01T18:04:00Z', 'Invalid/Zone', '—'],
  ])('formats the actual rule zone and offset of %s', (instant, zone, label) => {
    expect(formatScheduleTimestamp(instant!, zone!)).toBe(label)
  })

  it('does not reinterpret resolved input when the rule timezone changes', () => {
    const candidates = localScheduleInstants('2027-01-02T03:04', 'Asia/Tokyo')
    const draft = { ...DRAFT, kind: 'ONCE' as const, runAt: '2027-01-02T03:04' }
    expect(scheduleDefinition(draft, candidates, [])?.run_at).toBe(candidates[0]!.instant)
    expect(scheduleDefinition({ ...draft, timezone: 'UTC' }, candidates, [])?.run_at).toBe(candidates[0]!.instant)
  })

  it.each([{ timezone: 'Invalid/Zone' }, { cronExpression: '' }, { maxRuns: '1.5' },
    { maxRuns: '0' }, { maxRuns: '100001' }, { kind: 'ONCE' as const }, { endAt: '2027-11-07T01:30' }])
  ('rejects an incomplete definition %j', (change) => {
    expect(scheduleDefinition({ ...DRAFT, ...change }, [], [])).toBeNull()
  })

  it('requires an explicit fold choice for a nonempty end time too', () => {
    const options = localScheduleInstants('2027-11-07T01:30', 'America/New_York')
    const draft = { ...DRAFT, endAt: '2027-11-07T01:30', maxRuns: '2' }
    expect(scheduleDefinition(draft, [], options)).toBeNull()
    expect(scheduleDefinition({ ...draft, endAtChoice: options[1]!.instant }, [], options))
      .toEqual({ kind: 'CRON', timezone: 'Asia/Tokyo', cron_expression: '0 3 * * *',
        run_at: null, end_at: options[1]!.instant, max_runs: 2 })
  })
})

describe('saved schedule initialization', () => {
  it.each(['2027-11-07T05:30:00Z', '2027-11-07T06:30:00Z'])('retains the original fold choice %s', (instant) => {
    const record = scheduleFixture({ kind: 'ONCE', cron_expression: null, run_at: instant })
    const draft = scheduleTimeDraft(record, 'America/New_York')
    expect(draft.runAt).toBe('2027-11-07T01:30')
    const options = localScheduleInstants(draft.runAt, 'America/New_York')
    expect(Date.parse(selectedScheduleInstant(options, draft.runAtChoice)!)).toBe(Date.parse(instant))
    const resolved = scheduleDefinition(draft, options, [])
    expect(preservedScheduleDefinition(draft, draft, record, resolved)?.run_at).toBe(instant)
  })

  it('does not truncate saved end seconds or microseconds through a minute input', () => {
    const record = scheduleFixture({ end_at: '2027-11-07T06:30:32.123456Z' })
    const initial = scheduleTimeDraft(record, 'America/New_York')
    expect(initial.endAt).toBe('2027-11-07T01:30')
    const options = localScheduleInstants(initial.endAt, 'America/New_York')
    const changed = { ...initial, timezone: 'UTC' }
    expect(preservedScheduleDefinition(changed, initial, record, scheduleDefinition(changed, [], options))?.end_at)
      .toBe(record.end_at)
    const newChoice = { ...initial, endAtChoice: options[0]!.instant }
    expect(preservedScheduleDefinition(newChoice, initial, record, scheduleDefinition(newChoice, [], options))?.end_at)
      .toBe('2027-11-07T05:30:00.000Z')
  })

  it('preserves an unchanged rule without silently normalizing its spacing', () => {
    const record = scheduleFixture({ cron_expression: '0  3 * * *', max_runs: 42 })
    const initial = scheduleTimeDraft(record, 'UTC')
    expect(initial.maxRuns).toBe('42')
    expect(preservedScheduleDefinition(initial, initial, record, scheduleDefinition(initial, [], [])))
      .toEqual({ kind: record.kind, timezone: record.timezone, cron_expression: record.cron_expression,
        run_at: null, end_at: null, max_runs: 42 })
  })
})
