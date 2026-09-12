import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  clearInterpretationReceipt,
  loadInterpretationReceipt,
  saveInterpretationReceipt,
} from '../../src/lib/interpretationReceipt'

const ID = '00000000-0000-4000-8000-000000000061'

afterEach(() => vi.unstubAllGlobals())

/** 元要求だけを保存し、読めない記録を新しい実行へ置き換えないことを検証する。 */
describe('interpretation receipt', () => {
  it('stores only a UUID until a confirmed or explicit dismissal', () => {
    const rows = new Map<string, string>()
    vi.stubGlobal('sessionStorage', {
      getItem: (key: string) => rows.get(key) ?? null,
      setItem: (key: string, value: string) => rows.set(key, value),
      removeItem: (key: string) => rows.delete(key),
    })
    expect(loadInterpretationReceipt()).toBeNull()
    saveInterpretationReceipt(ID)
    expect([...rows.values()]).toEqual([ID])
    expect(loadInterpretationReceipt()).toBe(ID)
    clearInterpretationReceipt()
    expect(rows.size).toBe(0)
  })

  it('retains corrupt state and exposes storage failures', () => {
    const removeItem = vi.fn()
    vi.stubGlobal('sessionStorage', { getItem: () => '{broken}', removeItem })
    expect(loadInterpretationReceipt).toThrow('Invalid saved interpretation request')
    removeItem.mockClear()
    expect(removeItem).not.toHaveBeenCalled()
    expect(() => saveInterpretationReceipt('00000000-0000-0000-0000-000000000000')).toThrow()
    vi.stubGlobal('sessionStorage', { setItem: () => { throw new Error('unavailable') } })
    expect(() => saveInterpretationReceipt(ID)).toThrow('unavailable')
  })
})
