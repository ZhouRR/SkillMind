import { describe, expect, it } from 'vitest'

import { formatByteSize, formatLocalTimestamp, runHistoryTitle } from '../../src/lib/presentation'

/** 見出し組み立ての検証用に catalog の代替書式を模した固定関数。 */
const fallbackTitle = (shortId: string): string => `执行 ${shortId}`

describe('runHistoryTitle', () => {
  it('uses the generic result summary', () => {
    /** 業務 input field を推測せず、公開 read model の summary だけを見出しに使う。 */
    expect(runHistoryTitle('Result summary', '0000', fallbackTitle)).toBe('Result summary')
  })

  it('falls back to a short run id before a result exists', () => {
    /** 実行中でも input を表示せず、catalog の書式で短い Run ID 見出しを組み立てる。 */
    expect(runHistoryTitle(null, 'abcdef01-2345', fallbackTitle)).toBe('执行 abcdef01')
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
