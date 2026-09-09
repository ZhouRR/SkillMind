import { useEffect, useRef } from 'react'

import type { UserAccountRecord } from '../api'
import { useMessages } from '../i18n'
import { formatLocalTime } from '../lib/presentation'
import type { UserFailure } from '../lib/userFeedback'

/** Server の未知本文や password を描画せず、失敗時は読み上げ位置へ移す。 */
export function UserResponseNotice({ failure, cooldown = 0 }: { failure: UserFailure | null; cooldown?: number }) {
  const messages = useMessages().account
  const notice = useRef<HTMLDivElement>(null)
  useEffect(() => { if (failure) notice.current?.focus() }, [failure])
  if (!failure) return null
  return <div className="accountNotice error" ref={notice} role="alert" tabIndex={-1}>
    <p>{messages.failures[failure.key]}</p>
    {cooldown > 0 && <p>{messages.retryAfter(cooldown)}</p>}
  </div>
}

/** 許可された account field だけを、原版/最新版の双方に同じ形で表示する。 */
export function UserAccountFacts({ account }: { account: UserAccountRecord }) {
  const messages = useMessages().account
  return <dl className="accountFacts">
    <div><dt>{messages.fields.name}</dt><dd>{account.display_name}</dd></div>
    <div><dt>{messages.fields.email}</dt><dd>{account.email}</dd></div>
    <div><dt>{messages.fields.role}</dt><dd>{messages.roles[account.system_role]}</dd></div>
    <div><dt>{messages.fields.status}</dt><dd>{messages.statuses[account.status]}</dd></div>
    <div><dt>{messages.fields.userId}</dt><dd>{account.user_id}</dd></div>
    <div><dt>{messages.fields.version}</dt><dd>{account.row_version}</dd></div>
    <div><dt>{messages.fields.created}</dt><dd>{formatLocalTime(account.created_at)}</dd></div>
    <div><dt>{messages.fields.updated}</dt><dd>{formatLocalTime(account.updated_at)}</dd></div>
  </dl>
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
