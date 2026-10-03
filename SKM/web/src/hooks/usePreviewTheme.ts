import { useEffect, useState } from 'react'
import { previewThemeCss, readPreviewTheme, type PreviewTheme } from '../lib/previewTheme'

/** 文書と抜粋を表示したまま配色変更へ追随し、observer は unmount で解除する。 */
export function usePreviewTheme(): PreviewTheme {
  const [theme, setTheme] = useState(readPreviewTheme)
  useEffect(() => {
    const update = (): void => {
      const next = readPreviewTheme()
      setTheme((current) => previewThemeCss(current) === previewThemeCss(next) ? current : next)
    }
    const observer = new MutationObserver(update)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    update()
    return () => observer.disconnect()
  }, [])
  return theme
}
