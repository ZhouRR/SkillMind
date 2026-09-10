import { useEffect, useRef } from 'react'

import type { UserAccountRecord } from '../api'
import { useMessages } from '../i18n'
import { formatLocalTimestamp } from '../lib/presentation'
import type { UserFailure } from '../lib/userFeedback'

/** Server の未知本文や password を描画せず、失敗時は読み上げ位置へ移す。 */
export function UserResponseNotice({ failure, cooldown = 0 }: { failure: UserFailure | null; cooldown?: number }) {
  const messages = useMessages().account
  const notice = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!failure) return
    // 遅延した失敗も管理区分の折畳みに埋めず、元 editor の状態を保ったまま開く。
    for (let parent = notice.current?.parentElement; parent; parent = parent.parentElement) {
      if (parent instanceof HTMLDetailsElement) parent.open = true
    }
    notice.current?.focus()
  }, [failure])
  if (!failure) return null
  return <div className="accountNotice error" ref={notice} role="alert" tabIndex={-1}>
    <p>{messages.failures[failure.key]}</p>
    {cooldown > 0 && <p>{messages.retryAfter(cooldown)}</p>}
  </div>
}

/** 許可された account field だけを、原版/最新版の双方に同じ形で表示する。 */
export function UserAccountFacts({ account, compact = false }: { account: UserAccountRecord; compact?: boolean }) {
  const catalog = useMessages()
  const messages = catalog.account
  const technical = <dl className="accountFacts accountTechnicalFacts">
    <div><dt>{messages.fields.userId}</dt><dd>{account.user_id}</dd></div>
    <div><dt>{messages.fields.version}</dt><dd>{account.row_version}</dd></div>
    <div><dt>{messages.fields.created}</dt><dd><time dateTime={account.created_at} title={account.created_at}>{formatLocalTimestamp(account.created_at)}</time></dd></div>
    <div><dt>{messages.fields.updated}</dt><dd><time dateTime={account.updated_at} title={account.updated_at}>{formatLocalTimestamp(account.updated_at)}</time></dd></div>
  </dl>
  return <><dl className="accountFacts">
    <div><dt>{messages.fields.name}</dt><dd>{account.display_name}</dd></div>
    <div><dt>{messages.fields.email}</dt><dd>{account.email}</dd></div>
    <div><dt>{messages.fields.role}</dt><dd>{messages.roles[account.system_role]}</dd></div>
    <div><dt>{messages.fields.status}</dt><dd>{messages.statuses[account.status]}</dd></div>
  </dl>{compact ? <details className="detailDisclosure accountTechnical"><summary>{catalog.elements.technicalDetails}</summary>
    {technical}<p className="accountVersion">{messages.versionUsed(account.row_version)}</p>
  </details> : technical}</>
}

/** 一覧と作成確認で同じ公開項目を使い、同 email を成功証跡と解釈しない。 */
export function UserSummaryList({ users, disabled, onSelect }: {
  users: UserAccountRecord[]
  disabled: boolean
  onSelect: (userId: string) => void
}) {
  const messages = useMessages().account
  if (users.length === 0) return <p>{messages.emptyUsers}</p>
  return <ul className="accountUserList" tabIndex={0} aria-label={messages.manageUsers}>
    {users.map((account) => <li className="accountUserCard" key={account.user_id}>
      <div><strong>{account.display_name}</strong><p>{account.email}</p>
        <p>{messages.roles[account.system_role]} · {messages.statuses[account.status]}</p>
      </div>
      <button type="button" className="secondaryButton" disabled={disabled}
        aria-label={`${messages.edit}: ${account.email}`} onClick={() => onSelect(account.user_id)}>{messages.edit}</button>
    </li>)}
  </ul>
}

/** Server の page を移動し、動的な total を固定 snapshot として扱わない。 */
export function UserPager({ offset, count, total, limit, pending, onChange }: {
  offset: number; count: number; total: number; limit: number; pending: boolean
  onChange: (offset: number) => void
}) {
  const messages = useMessages().account
  return <div className="accountPager">
    <p role="status">{messages.page(offset, count, total)}</p>
    <div className="inlineActions">
      <button className="secondaryButton" type="button" disabled={pending || offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>{messages.previous}</button>
      <button className="secondaryButton" type="button" disabled={pending || offset + limit >= total} onClick={() => onChange(offset + limit)}>{messages.next}</button>
    </div>
  </div>
}
