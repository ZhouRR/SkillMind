import { useEffect, useState } from 'react'

import type { AuthSessionRecord } from '../../src/api'
import { LanguageProvider } from '../../src/i18n'
import type { UiLanguage } from '../../src/lib/i18n/messages'
import { AccountsPage } from '../../src/pages/AccountsPage'
import { LoginPage } from '../../src/pages/LoginPage'

/** API は全面 mock し、実画面に渡す会話と mount の境界だけを操作する。 */
interface AccountsTestContext {
  session: AuthSessionRecord
  language: UiLanguage
  projectId: string
  mounted: boolean
}

declare global {
  interface Window {
    /** Production entry から参照しない browser 専用 lifecycle 入口。 */
    updateAccountsTestContext?: (context: Partial<AccountsTestContext>) => void
  }
}

/** 親を保持して遅い成功/失効 callback を観測し、実 App は別 mode でも検証する。 */
export function AccountsHarness() {
  const parameters = new URLSearchParams(location.search)
  const [context, setContext] = useState<AccountsTestContext>({
    language: (parameters.get('language') ?? 'en') as UiLanguage,
    mounted: true,
    projectId: '',
    session: {
      user: {
        user_id: '00000000-0000-4000-8000-000000000001',
        organization_id: '00000000-0000-4000-8000-000000000002',
        email: 'reader@example.com', display_name: 'Browser reader',
        system_role: parameters.get('role') === 'ADMIN' ? 'ADMIN' : 'USER',
      },
      csrf_token: 'c'.repeat(32), absolute_expires_at: '2099-01-01T00:00:00Z',
    },
  })
  const [ended, setEnded] = useState(0)
  const [changed, setChanged] = useState(0)
  useEffect(() => {
    window.updateAccountsTestContext = (next) => setContext((current) => ({ ...current, ...next }))
    return () => { delete window.updateAccountsTestContext }
  }, [])
  return <LanguageProvider language={context.language}>
    <main className="shell accountsPage">
      {context.mounted && (ended ? <LoginPage onAuthenticated={() => undefined} /> : <AccountsPage
        key={`${context.session.user.user_id}:${context.session.csrf_token}:${context.projectId}`}
        session={context.session}
        onSessionEnded={() => setEnded((value) => value + 1)}
        onAccountChanged={() => setChanged((value) => value + 1)}
      />)}
      <output data-testid="session-ended-count">{ended}</output>
      <output data-testid="account-changed-count">{changed}</output>
    </main>
  </LanguageProvider>
}
