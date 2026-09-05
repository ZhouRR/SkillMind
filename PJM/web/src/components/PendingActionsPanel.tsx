import { useEffect, useRef, useState } from 'react'

import { loadPendingRuns, type RunHistoryItemRecord } from '../api'
import { useMessages } from '../i18n'
import { formatLocalTimestamp, runHistoryTitle } from '../lib/presentation'
import { routeHref } from '../lib/routing'
import { EmptyState, LoadingSkeleton, StatusBadge } from './PageElements'

/** 「待你处理」の取得状態。 */
type PendingState =
  | { status: 'idle' | 'loading' }
  | { status: 'ready'; items: RunHistoryItemRecord[] }
  | { status: 'error'; message: string }

/** 一度に出す待機 Run の上限。溜まっているときは全件ではなく件数で示す。 */
const PENDING_LIMIT = 10

/** 利用者の応答・承認を待っている Run を一覧する。
 *
 * これが無いと、待機中の Run は「工作空间へ行って、たまたまその Run を選んだとき」にしか
 * 見えない。Run は待機中は lease も wall timeout も持たず、いつまでも待ち続けるため、
 * 気付かれない待機は事実上の停止になる。
 */
export function PendingActionsPanel({ projectId }: { projectId: string }) {
  const messages = useMessages()
  const [state, setState] = useState<PendingState>({ status: 'idle' })
  const controller = useRef<AbortController | null>(null)

  useEffect(() => {
    controller.current?.abort()
    if (!projectId) {
      setState({ status: 'idle' })
      return
    }
    const active = new AbortController()
    controller.current = active
    setState({ status: 'loading' })
    void loadPendingRuns(projectId, PENDING_LIMIT, active.signal)
      .then((items) => {
        if (!active.signal.aborted) setState({ status: 'ready', items })
      })
      .catch((error: unknown) => {
        if (active.signal.aborted) return
        setState({
          status: 'error',
          message: error instanceof Error ? error.message : messages.pending.loadFailed,
        })
      })
    return () => active.abort()
  }, [projectId])

  useEffect(() => () => controller.current?.abort(), [])

  const count = state.status === 'ready' ? state.items.length : 0
  return (
    <section
      className={`panel pendingPanel${count > 0 ? ' pendingPanelActive' : ''}`}
      aria-label={messages.pending.title}
    >
      <div className="panelHeader">
        <h2>{messages.pending.title}{count > 0 && <span className="eventCount">{count}</span>}</h2>
      </div>
      {!projectId && <EmptyState text={messages.pending.selectProjectFirst} />}
      {projectId && state.status === 'loading' && (
        <LoadingSkeleton label={messages.pending.title} rows={2} />
      )}
      {state.status === 'error' && <p className="error" role="alert">{state.message}</p>}
      {state.status === 'ready' && state.items.length === 0 && (
        <EmptyState text={messages.pending.empty} />
      )}
      {state.status === 'ready' && state.items.length > 0 && (
        <ul className="pendingList">
          {state.items.map((item) => (
            <li key={item.run_id}>
              <a className="pendingItem" href={routeHref('workspace', projectId)}>
                <span className="pendingItemTitle">
                  <strong>{runHistoryTitle(item.result_summary, item.run_id, messages.elements.runFallbackTitle)}</strong>
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
