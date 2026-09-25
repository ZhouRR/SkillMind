import { RunDuration } from './RunDuration'
import { useEffect, useState } from 'react'
import { loadRunHistory, type RunHistoryPageRecord, type RunStatus } from '../api'
import { useMessages } from '../i18n'
import { formatLocalTimestamp, runHistoryTitle } from '../lib/presentation'
import { routeHref } from '../lib/routing'
import { EmptyState, LoadingSkeleton, StatusBadge } from './PageElements'
import { WorkspaceReports } from './WorkspaceReports'

const FILTERS: Record<'pending' | 'running' | 'reports', readonly RunStatus[]> = {
  pending: ['WAITING_FOR_INPUT', 'WAITING_FOR_APPROVAL', 'WAITING_PERMISSION'],
  running: ['QUEUED', 'PREPARING', 'RUNNING', 'RETRY_PENDING'],
  reports: ['SUCCEEDED', 'FAILED', 'CANCELLED'],
}
const PAGE_SIZE = 10

/** 対応待ち・実行中の一覧と、タスク別の最新完了レポートを表示する。 */
export function WorkspaceQueue(props: Parameters<typeof WorkspaceReports>[0]) {
  const { projectId } = props
  const messages = useMessages()
  const [tab, setTab] = useState<keyof typeof FILTERS>('pending')
  const [offset, setOffset] = useState(0)
  const [revision, setRevision] = useState(0)
  const [page, setPage] = useState<RunHistoryPageRecord | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (!projectId || tab === 'reports') return
    const controller = new AbortController()
    setPage(null); setError(null)
    void loadRunHistory(projectId, PAGE_SIZE, offset, controller.signal, FILTERS[tab])
      .then((value) => { if (!controller.signal.aborted) setPage(value) })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : messages.runHistory.loading)
      })
    return () => controller.abort()
  }, [projectId, tab, offset, revision])
  useEffect(() => {
    const timer = window.setInterval(() => setRevision((value) => value + 1), 30000)
    return () => window.clearInterval(timer)
  }, [])
  return <section className="panel workspaceQueue">
    <div className="tabBar" role="tablist" aria-label={messages.workspace.queueTitle}>
      {(Object.keys(FILTERS) as (keyof typeof FILTERS)[]).map((key) => <button type="button" role="tab"
        className="tab" aria-selected={tab === key} key={key}
        onClick={() => { setTab(key); setOffset(0) }}>{messages.workspace.queue[key]}</button>)}
    </div>
    <div className="tabPanel" role="tabpanel">
      {tab === 'reports' ? <WorkspaceReports {...props} /> : <>
      {!projectId ? <EmptyState text={messages.workspace.selectProjectFirst} />
        : error ? <p className="error" role="alert">{error}</p>
        : page === null ? <LoadingSkeleton label={messages.runHistory.loading} rows={3} />
        : page.items.length === 0 ? <EmptyState text={messages.workspace.queueEmpty[tab]} />
        : <ul className="pendingList">{page.items.map((item) => <li key={item.run_id}>
          <a className="pendingItem" href={routeHref('history', projectId, { runId: item.run_id })}>
            <span className="pendingItemTitle"><strong>{runHistoryTitle(item, messages.elements.unnamedRunTitle)}</strong>
              <small>{formatLocalTimestamp(item.started_at ?? item.created_at)} · <RunDuration run={item} /></small>
            </span><StatusBadge status={item.status} />
          </a>
        </li>)}</ul>}
      <div className="pagination">
        {/* 一頁に収まる間は押せない前後 button を並べず、再読込だけを残す。 */}
        {(offset > 0 || page?.has_more) && <>
          <button className="secondaryButton compactButton" disabled={offset === 0} type="button"
            onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}>{messages.runHistory.previous}</button>
          <button className="secondaryButton compactButton" disabled={!page?.has_more} type="button"
            onClick={() => setOffset((value) => value + PAGE_SIZE)}>{messages.runHistory.next}</button>
        </>}
        <button className="secondaryButton compactButton" type="button"
          onClick={() => setRevision((value) => value + 1)}>{messages.runHistory.retry}</button>
      </div>
      </>}
    </div>
  </section>
}
