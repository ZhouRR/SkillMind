/** Theme は端末だけの表示設定。認証・業務 preference と混ぜない。 */
export type UiTheme = 'dark' | 'light'
export const THEME_STORAGE_KEY = 'skillmind.theme'

/** 不明値と初回は OS 設定に依存せず夜間へ戻す。 */
export function resolveTheme(value: string | null): UiTheme { return value === 'light' ? 'light' : 'dark' }

/** Storage が禁止されても画面を開けるよう、保存失敗は表示設定内で閉じる。 */
export function readTheme(): UiTheme {
  try { return resolveTheme(window.localStorage.getItem(THEME_STORAGE_KEY)) } catch { return 'dark' }
}

/** 保存値は二値だけに限定する。本文・actor・token は保存しない。 */
export function saveTheme(theme: UiTheme): void {
  try { window.localStorage.setItem(THEME_STORAGE_KEY, theme) } catch { /* 現在の表示は維持する。 */ }
}

/** CSS とブラウザ chrome を同時更新し、業務 subtree を再 mount しない。 */
export function applyTheme(theme: UiTheme): void {
  document.documentElement.dataset.theme = theme
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#1b1915' : '#f7f5ef')
}
