import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'

import { LanguageProvider } from '../../src/i18n'
import type { UiLanguage } from '../../src/lib/i18n/messages'
import { LoginPage } from '../../src/pages/LoginPage'
import '../../src/styles.css'

/** API は runner が全面 mock し、言語と実 component の mount だけを切り替える。 */
interface LoginTestContext {
  language: UiLanguage
  mounted: boolean
}

declare global {
  interface Window {
    /** 離頁と provider の変更を再現する fixture 専用入口。 */
    updateLoginTestContext?: (context: Partial<LoginTestContext>) => void
  }
}

/** 親を生かしたまま LoginPage を破棄し、禁止された遅い callback も可視化する。 */
function LoginHarness() {
  const [context, setContext] = useState<LoginTestContext>({ language: 'zh', mounted: true })
  const [accepted, setAccepted] = useState(0)
  useEffect(() => {
    window.updateLoginTestContext = (next) => setContext((current) => ({ ...current, ...next }))
    return () => { delete window.updateLoginTestContext }
  }, [])
  return (
    <LanguageProvider language={context.language}>
      {context.mounted && <LoginPage onAuthenticated={() => setAccepted((value) => value + 1)} />}
      <output data-testid="authenticated-count">{accepted}</output>
    </LanguageProvider>
  )
}

const root = document.getElementById('root')
if (!root) throw new Error('Missing browser fixture root')
createRoot(root).render(<StrictMode><LoginHarness /></StrictMode>)
