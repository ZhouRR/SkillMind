import { useState } from 'react'

import type { AuthSessionRecord, UserAccountRecord } from '../api'
import { PageHeader } from '../components/PageElements'
import { UserAccountPanel } from '../components/UserAccountPanel'
import { UserDirectory } from '../components/UserDirectory'
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
  /** 一覧・本人資料の再読取は通知し、編集中の原版や草稿は自動採用しない。 */
  const changed = (account: UserAccountRecord): void => {
    setRevision((value) => value + 1)
    onAccountChanged(account)
  }
  return <>
    <PageHeader title={messages.routes.accounts.label} description={messages.routes.accounts.description} />
    <p className="hint">{messages.account.draftMemoryOnly}</p>
    <div className="accountsLayout">
      <div data-account-own="">
        <UserAccountPanel userId={session.user.user_id} own session={session} revision={revision}
          onSessionEnded={onSessionEnded} onChanged={changed} />
      </div>
      {session.user.system_role === 'ADMIN' && <UserDirectory session={session} revision={revision}
        onSessionEnded={onSessionEnded} onChanged={changed} />}
    </div>
  </>
}
