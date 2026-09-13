import { describe, expect, it } from 'vitest'

import { formatByteSize, formatLocalTimestamp, runHistoryTitle } from '../../src/lib/presentation'

describe('runHistoryTitle', () => {
  it('keeps the frozen task name with and without a result', () => {
    expect(runHistoryTitle({ task_title: '仕様書 RV 審査', result_summary: null }, 'タスク実行'))
      .toBe('仕様書 RV 審査')
    expect(runHistoryTitle({ task_title: '仕様書 RV 審査', result_summary: 'Completed' }, 'タスク実行'))
      .toBe('仕様書 RV 審査')
  })

  it('supports old API records without putting identifiers in titles', () => {
    expect(runHistoryTitle({ result_summary: 'Result summary' }, '任务执行')).toBe('Result summary')
    expect(runHistoryTitle({ result_summary: null }, '任务执行')).toBe('任务执行')
    expect(runHistoryTitle({ task_title: ' ', result_summary: ' ' }, 'Task execution')).toBe('Task execution')
  })
})

describe('formatByteSize', () => {
  it('formats bytes, kilobytes and megabytes with a single unit', () => {
    expect(formatByteSize(512)).toBe('512 B')
    expect(formatByteSize(2048)).toBe('2.0 KB')
    expect(formatByteSize(5 * 1024 * 1024)).toBe('5.0 MB')
  })
})

describe('formatLocalTimestamp', () => {
  it('keeps an unparsable value verbatim', () => {
    expect(formatLocalTimestamp('not-a-date')).toBe('not-a-date')
  })

  it('converts a valid ISO timestamp to a locale string', () => {
    const formatted = formatLocalTimestamp('2026-07-11T10:00:00Z')
    expect(formatted).not.toBe('2026-07-11T10:00:00Z')
    expect(formatted.length).toBeGreaterThan(0)
  })
})
