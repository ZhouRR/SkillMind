import { useEffect, useRef, useState } from 'react'

import { loadPendingRunPage, type RunHistoryItemRecord } from '../api'
import { useMessages } from '../i18n'
import { formatLocalTimestamp, runHistoryTitle } from '../lib/presentation'
import { routeHref } from '../lib/routing'
import { EmptyState, LoadingSkeleton, StatusBadge } from './PageElements'

/** 「待你处理」の取得状態。 */
type PendingState =
  | { status: 'idle' | 'loading' }
  | { status: 'ready'; items: RunHistoryItemRecord[]; hasMore: boolean; error?: string }
  | { status: 'error'; message: string }

/** 一度に出す待機 Run の上限。溜まっているときは全件ではなく件数で示す。 */
const PENDING_LIMIT = 10

/** 利用者の応答・承認を待っている Run を一覧する。
 *
 * これが無いと、待機中の Run は「工作空间へ行って、たまたまその Run を選んだとき」にしか
 * 見えない。待機は Worker lease を解放するが独立した期限があるため、
 * 気付かれないまま期限切れになる前に、精確な Run への入口を示す。
 */
export function PendingActionsPanel({ projectId, revision = 0 }: { projectId: string; revision?: number }) {
  const messages = useMessages()
  const [storedState, setState] = useState<PendingState>({ status: 'idle' })
  const [retry, setRetry] = useState(0)
  const loadedProject = useRef(projectId)
  const controller = useRef<AbortController | null>(null)
  const state: PendingState = loadedProject.current === projectId ? storedState : { status: 'loading' }

  useEffect(() => {
    controller.current?.abort()
    if (!projectId) {
      setState({ status: 'idle' })
      return
    }
    const active = new AbortController()
    controller.current = active
    const sameProject = loadedProject.current === projectId
    loadedProject.current = projectId
    setState((previous) => sameProject && previous.status === 'ready' ? previous : { status: 'loading' })
    void loadPendingRunPage(projectId, PENDING_LIMIT, active.signal)
      .then((page) => {
        if (!active.signal.aborted) setState({ status: 'ready', items: page.items, hasMore: page.has_more })
      })
      .catch((error: unknown) => {
        if (active.signal.aborted) return
        const message = error instanceof Error ? error.message : messages.pending.loadFailed
        setState((previous) => previous.status === 'ready' ? { ...previous, error: message } : { status: 'error', message })
      })
    return () => active.abort()
  }, [projectId, revision, retry])

  useEffect(() => () => controller.current?.abort(), [])

  const count = state.status === 'ready' ? state.items.length : 0
  return (
    <section
      className={`panel pendingPanel${count > 0 ? ' pendingPanelActive' : ''}`}
      aria-label={messages.pending.title}
    >
      <div className="panelHeader">
        <h2>{messages.pending.title}{count > 0 && <span className="eventCount">{count}{state.status === 'ready' && state.hasMore ? '+' : ''}</span>}</h2>
        {projectId && <div className="homeActions"><button className="secondaryButton compactButton" type="button" onClick={() => setRetry((value) => value + 1)}>{messages.runHistory.retry}</button><a href={routeHref('workspace', projectId)}>{messages.uiAuditWorkspace.viewAllPending}</a></div>}
      </div>
      {!projectId && <EmptyState text={messages.pending.selectProjectFirst} />}
      {projectId && (state.status === 'loading' || state.status === 'idle') && (
        <LoadingSkeleton label={messages.pending.title} rows={2} />
      )}
      {state.status === 'error' && <p className="error" role="alert">{state.message}</p>}
      {state.status === 'ready' && state.error && <p className="error" role="alert">{state.error}</p>}
      {state.status === 'ready' && state.hasMore && <p className="hint">{messages.uiAuditWorkspace.pendingTruncated}</p>}
      {state.status === 'ready' && state.items.length === 0 && (
        <EmptyState text={messages.pending.empty} />
      )}
      {state.status === 'ready' && state.items.length > 0 && (
        <ul className="pendingList">
          {state.items.map((item) => (
            <li key={item.run_id}>
              <a className="pendingItem" href={routeHref('history', projectId, { runId: item.run_id })}>
                <span className="pendingItemTitle">
                  <strong>{runHistoryTitle(item, messages.elements.unnamedRunTitle)}</strong>
                  <small>{formatLocalTimestamp(item.created_at)}</small>
                </span>
                <StatusBadge status={item.status} />
              </a>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
