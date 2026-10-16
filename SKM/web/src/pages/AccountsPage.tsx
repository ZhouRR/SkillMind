import { useId, useState } from 'react'

import type { AuthSessionRecord, UserAccountRecord } from '../api'
import { ApiKeyPanel } from '../components/ApiKeyPanel'
import { PageHeader } from '../components/PageElements'
import { UserAccountPanel } from '../components/UserAccountPanel'
import { UserDirectory } from '../components/UserDirectory'
import { TabButton } from '../components/TabButton'
import type { SessionEnded } from '../hooks/useUserRequest'
import { useMessages } from '../i18n'

/** Organization の本人安全と管理入口を Project 非依存で提供する。 */
export function AccountsPage({ session, onSessionEnded, onAccountChanged }: {
  session: AuthSessionRecord
  onSessionEnded: SessionEnded
  onAccountChanged: (account: UserAccountRecord) => void
}) {
  return <AccountsContent key={`${session.user.user_id}:${session.csrf_token}`} session={session}
    onSessionEnded={onSessionEnded} onAccountChanged={onAccountChanged} />
}

/** Actor が変われば全草稿を破棄し、同じ actor の変更は精確な再読取へ渡す。 */
function AccountsContent({ session, onSessionEnded, onAccountChanged }: {
  session: AuthSessionRecord
  onSessionEnded: SessionEnded
  onAccountChanged: (account: UserAccountRecord) => void
}) {
  const messages = useMessages()
  const [revision, setRevision] = useState(0)
  const [tab, setTab] = useState<'own' | 'keys' | 'users'>('own')
  const tabId = useId()
  const admin = session.user.system_role === 'ADMIN'
  /** 一覧・本人資料の再読取は通知し、編集中の原版や草稿は自動採用しない。 */
  const changed = (account: UserAccountRecord): void => {
    setRevision((value) => value + 1)
    onAccountChanged(account)
  }
  return <>
    <PageHeader title={messages.routes.accounts.label} />
    <div className="tabBar" role="tablist" aria-label={messages.routes.accounts.label}>
      {(['own', ...(admin ? ['keys', 'users'] as const : [])] as const).map((item) =>
        <TabButton key={item} id={`${tabId}-${item}`} panelId={`${tabId}-${item}-panel`}
          selected={tab === item} onSelect={() => setTab(item)}>
          {item === 'own' ? messages.account.myAccount : item === 'keys' ? messages.apiKeys.title : messages.account.manageUsers}
        </TabButton>)}
    </div>
    <div className="accountsLayout">
      {/* 非表示 panel も保持し、tab 往復で草稿・未知要求・編集対象を破棄しない。 */}
      <div data-account-own="" role="tabpanel" id={`${tabId}-own-panel`} aria-labelledby={`${tabId}-own`} hidden={tab !== 'own'}>
        <UserAccountPanel userId={session.user.user_id} own session={session} revision={revision}
          onSessionEnded={onSessionEnded} onChanged={changed} />
      </div>
      {admin && <section className="panel accountDirectorySection" role="tabpanel" id={`${tabId}-keys-panel`} aria-labelledby={`${tabId}-keys`} hidden={tab !== 'keys'}>
        <ApiKeyPanel session={session} onSessionEnded={onSessionEnded} />
      </section>}
      {admin && <section className="accountDirectorySection" role="tabpanel" id={`${tabId}-users-panel`} aria-labelledby={`${tabId}-users`} hidden={tab !== 'users'}>
        <UserDirectory session={session} revision={revision} onSessionEnded={onSessionEnded} onChanged={changed} />
      </section>}
    </div>
  </>
}
