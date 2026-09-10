import { afterEach, describe, expect, it, vi } from 'vitest'

import themeInitializer from '../../public/theme-init.js?raw'
import { applyTheme, readTheme, resolveTheme, saveTheme, THEME_STORAGE_KEY } from '../../src/lib/theme'

// 自作の静的 boot script を browser の二つの境界だけで実行し、Node 型への依存を増やさない。
const initialize = new Function('document', 'localStorage', themeInitializer)

afterEach(() => vi.unstubAllGlobals())

describe('browser theme', () => {
  it.each([null, '', 'system', 'DARK', 'dark'])('defaults %s to dark regardless of OS preference', (value) => {
    expect(resolveTheme(value)).toBe('dark')
  })

  it('persists only the selected display preference', () => {
    const getItem = vi.fn(() => 'light')
    const setItem = vi.fn()
    vi.stubGlobal('window', { localStorage: { getItem, setItem } })
    expect(readTheme()).toBe('light')
    expect(getItem).toHaveBeenCalledWith(THEME_STORAGE_KEY)
    saveTheme('dark')
    expect(setItem).toHaveBeenCalledExactlyOnceWith(THEME_STORAGE_KEY, 'dark')
  })

  it('keeps storage denial inside the display preference boundary', () => {
    vi.stubGlobal('window', { get localStorage() { throw new Error('Storage denied') } })
    expect(readTheme()).toBe('dark')
    expect(() => saveTheme('light')).not.toThrow()
  })

  it.each(['dark', 'light'] as const)('applies %s without touching the application tree', (theme) => {
    const dataset = {}
    const setAttribute = vi.fn()
    vi.stubGlobal('document', { documentElement: { dataset }, querySelector: () => ({ setAttribute }) })
    applyTheme(theme)
    expect(dataset).toEqual({ theme })
    expect(setAttribute).toHaveBeenCalledWith('content', theme === 'dark' ? '#1b1915' : '#f7f5ef')
  })

  it.each([null, 'light', 'dark', 'invalid'])('boot script agrees with the runtime for %s', (value) => {
    const dataset = {}
    const getItem = vi.fn(() => value)
    initialize({ documentElement: { dataset } }, { getItem })
    expect(getItem).toHaveBeenCalledWith(THEME_STORAGE_KEY)
    expect(dataset).toEqual({ theme: resolveTheme(value) })
  })

  it('boot script still applies dark when storage is unavailable', () => {
    const dataset = {}
    initialize({ documentElement: { dataset } }, undefined)
    expect(dataset).toEqual({ theme: 'dark' })
  })
})
