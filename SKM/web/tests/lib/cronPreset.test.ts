import { describe, expect, it } from 'vitest'
import { readCronPreset, writeCronPreset } from '../../src/lib/cronPreset'

describe('recurrence presets', () => {
  it('round-trips a fixed daily time and a weekly day without using browser timezone', () => {
    expect(readCronPreset('30 9 * * *')).toEqual({ mode: 'daily', time: '09:30', weekday: '1' })
    expect(writeCronPreset(readCronPreset('45 23 * * 5')!)).toBe('45 23 * * 5')
    expect(readCronPreset('0 0 * * 7')?.weekday).toBe('0')
  })
  it.each(['*/5 * * * *', '0 9 1 * *', '0 9 * * 1-5', '60 9 * * *', '0 24 * * *', ''])('keeps complex or invalid expressions in advanced mode: %s', (expression) => {
    expect(readCronPreset(expression)).toBeNull()
  })
})
