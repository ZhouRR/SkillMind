import type { UiTheme } from './theme'

/** 親画面の固定配色だけを静的 preview に渡し、文書の CSS は設定に使わない。 */
export interface PreviewTheme {
  mode: UiTheme
  background: string
  foreground: string
  raised: string
  border: string
}

export const LIGHT_PREVIEW_THEME: PreviewTheme = {
  mode: 'light', background: '#f7f5ef', foreground: '#24211b', raised: '#eeebe3', border: '#d5d0c4',
}

/** 配色 token は live root から一度に読み、iframe と Mermaid の版を揃える。 */
export function readPreviewTheme(): PreviewTheme {
  if (typeof document === 'undefined') return LIGHT_PREVIEW_THEME
  const mode = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'
  const style = getComputedStyle(document.documentElement)
  const fallback = mode === 'light' ? LIGHT_PREVIEW_THEME
    : { mode, background: '#28251f', foreground: '#ebe6dc', raised: '#302d25', border: '#494437' }
  const token = (name: string, value: string): string => style.getPropertyValue(name).trim() || value
  return { mode, background: token('--surface', fallback.background), foreground: token('--text', fallback.foreground),
    raised: token('--surface-raised', fallback.raised), border: token('--border', fallback.border) }
}

/** Markdown 既定配色だけを上書きし、セルの廃止色や打消線を保持する。 */
export function previewThemeCss(theme: PreviewTheme): string {
  return `:root{color-scheme:${theme.mode};color:${theme.foreground};background:${theme.background};--border:${theme.border};--raised:${theme.raised}}`
}
