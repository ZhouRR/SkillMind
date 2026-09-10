import { useEffect, useState } from 'react'
import { useMessages } from '../i18n'
import { applyTheme, readTheme, resolveTheme, saveTheme, THEME_STORAGE_KEY, type UiTheme } from '../lib/theme'

/** Login/ナビゲーションの表示だけを更新する。切替でフォームや進行中 request を破棄しない。 */
export function ThemeToggle() {
  const labels = useMessages().theme
  const [theme, setTheme] = useState<UiTheme>(() => typeof document === 'undefined' ? 'dark'
    : document.documentElement.dataset.theme ? resolveTheme(document.documentElement.dataset.theme) : readTheme())
  useEffect(() => {
    applyTheme(theme)
  }, [theme])
  useEffect(() => {
    const synchronize = (event: StorageEvent): void => {
      if (event.key === THEME_STORAGE_KEY || event.key === null) setTheme(resolveTheme(event.newValue))
    }
    window.addEventListener('storage', synchronize)
    return () => window.removeEventListener('storage', synchronize)
  }, [])
  return <div className="themeToggle" role="group" aria-label={labels.label}>
    {(['light', 'dark'] as const).map((value) => <button type="button" key={value}
      aria-pressed={theme === value} onClick={() => { setTheme(value); saveTheme(value) }}>
      <svg viewBox="0 0 24 24" aria-hidden="true">{value === 'light'
        ? <><circle cx="12" cy="12" r="4" /><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5" /></>
        : <path d="M20.5 14.5A9 9 0 0 1 9.5 3.5a9 9 0 1 0 11 11Z" />}</svg>
      {labels[value]}
    </button>)}
  </div>
}
