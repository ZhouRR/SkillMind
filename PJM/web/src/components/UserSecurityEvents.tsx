import { useCallback, useState } from 'react'

import { loadMySecurityEvents, loadUserSecurityEvents } from '../api'
import { useUserQuery, type SessionEnded } from '../hooks/useUserRequest'
import { useMessages } from '../i18n'
import { formatLocalTime } from '../lib/presentation'
import { sameUser } from '../lib/userFeedback'
import { UserPager, UserResponseNotice } from './UserAccountElements'

/** 自分/選択中一人の公開安全履歴。先頭 page の filter で全履歴を代用しない。 */
export function UserSecurityEvents({ userId, own, revision, onSessionEnded }: {
  userId: string; own: boolean; revision: number; onSessionEnded: SessionEnded
}) {
  const messages = useMessages().account
  const [offset, setOffset] = useState(0)
  const limit = 25
  const loader = useCallback(async (signal: AbortSignal) => {
    const page = own
      ? await loadMySecurityEvents(limit, offset, signal)
      : await loadUserSecurityEvents(userId, limit, offset, signal)
    if (page.items.some((event) => !sameUser(event.user_id, userId))) throw new Error('Unexpected security event target')
    return page
  }, [own, userId, offset])
  const query = useUserQuery(`${userId}:${own}:${offset}:${revision}`, loader, onSessionEnded)
  return <section className="panel accountEvents" aria-busy={query.pending}>
    <div className="panelHeader"><h3>{messages.securityEvents}</h3>
      <button className="secondaryButton" type="button" disabled={query.pending} onClick={query.refresh}>{messages.refresh}</button>
    </div>
    <p className="hint">{messages.auditHint}</p>
    <UserResponseNotice failure={query.failure} />
    {query.pending && <p role="status">{messages.busy}</p>}
    {query.data && <>
      {query.data.items.length === 0 && <p>{messages.emptyEvents}</p>}
      <ul className="accountEventList">
        {query.data.items.map((event) => <li key={event.event_id}>
          <details>
            <summary>{messages.eventActions[event.action]} · <time dateTime={event.created_at}>{formatLocalTime(event.created_at)}</time> · {messages.fields.version} {event.row_version}</summary>
            <dl className="accountFacts">
              <div><dt>{messages.eventActor}</dt><dd>{event.actor_id}</dd></div>
              <div><dt>{messages.eventRequest}</dt><dd>{event.request_id}</dd></div>
              <div><dt>{messages.eventPrevious}</dt><dd>{event.previous_role === null ? '—' : messages.roles[event.previous_role]} / {event.previous_status === null ? '—' : messages.statuses[event.previous_status]}</dd></div>
              <div><dt>{messages.eventResult}</dt><dd>{messages.roles[event.system_role]} / {messages.statuses[event.status]}</dd></div>
            </dl>
            <p>{messages.revokedCount(event.revoked_sessions)}</p>
          </details>
        </li>)}
      </ul>
      <UserPager offset={offset} limit={limit} count={query.data.items.length} total={query.data.total} pending={query.pending} onChange={setOffset} />
    </>}
  </section>
}
