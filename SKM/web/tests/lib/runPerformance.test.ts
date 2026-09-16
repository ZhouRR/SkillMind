import { afterEach, expect, it, vi } from 'vitest'
import { observeRunDetail, observeRunPaint } from '../../src/lib/runPerformance'

afterEach(() => vi.unstubAllGlobals())

it('keeps payload and identifiers out of observation and preserves display when telemetry fails', () => {
  const measure = vi.fn()
  vi.stubGlobal('performance', { now: () => 20, clearMeasures: vi.fn(), measure })
  const detail = { run_id: 'private-id', summary: 'private content' }
  observeRunDetail(detail, 10)
  expect(JSON.stringify(measure.mock.calls)).not.toContain('private')
  expect(measure.mock.calls[0]?.[1].detail.json_bytes).toBeGreaterThan(0)
  vi.stubGlobal('performance', { now: () => { throw new Error('unavailable') } })
  expect(() => observeRunDetail(detail, 10)).not.toThrow()
  expect(() => observeRunPaint(detail)()).not.toThrow()
})
