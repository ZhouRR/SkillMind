import { RunDuration } from './RunDuration'
import { useEffect, useId, useState } from 'react'
import { loadRunHistory, type RunHistoryPageRecord, type RunStatus } from '../api'
import { useMessages } from '../i18n'
import { formatLocalTimestamp, runHistoryTitle } from '../lib/presentation'
import { routeHref } from '../lib/routing'
import { EmptyState, LoadingSkeleton, StatusBadge } from './PageElements'
import { TabButton } from './TabButton'
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
  const id = useId()
  const [tab, setTab] = useState<keyof typeof FILTERS>('pending')
  const [reportsVisited, setReportsVisited] = useState(false)
  const [offset, setOffset] = useState(0)
  const [revision, setRevision] = useState(0)
  const [loaded, setLoaded] = useState<{ key: string; page: RunHistoryPageRecord } | null>(null)
  const [error, setError] = useState<{ key: string; message: string } | null>(null)
  const [updating, setUpdating] = useState(false)
  const requestKey = JSON.stringify([projectId, tab, offset])
  const page = loaded?.key === requestKey ? loaded.page : null
  const failure = error?.key === requestKey ? error.message : null
  useEffect(() => {
    if (!projectId || tab === 'reports') return
    const controller = new AbortController()
    // 同じ頁の更新では既存 DOM と focus を保ち、別対象の内容は key で直ちに隔離する。
    setUpdating(true); setError(null)
    void loadRunHistory(projectId, PAGE_SIZE, offset, controller.signal, FILTERS[tab])
      .then((value) => { if (!controller.signal.aborted) setLoaded({ key: requestKey, page: value }) })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) setError({ key: requestKey, message: caught instanceof Error ? caught.message : messages.runHistory.loading })
      }).finally(() => { if (!controller.signal.aborted) setUpdating(false) })
    return () => controller.abort()
  }, [projectId, tab, offset, revision, requestKey])
  useEffect(() => {
    const timer = window.setInterval(() => { if (document.visibilityState !== 'hidden') setRevision((value) => value + 1) }, 30000)
    return () => window.clearInterval(timer)
  }, [])
  return <section className="panel workspaceQueue">
    <div className="tabBar" role="tablist" aria-label={messages.workspace.queueTitle}>
      {(Object.keys(FILTERS) as (keyof typeof FILTERS)[]).map((key) => <TabButton key={key}
        id={`${id}-${key}-tab`} panelId={`${id}-${key}-panel`} selected={tab === key}
        onSelect={() => { setTab(key); setOffset(0); if (key === 'reports') setReportsVisited(true) }}>{messages.workspace.queue[key]}</TabButton>)}
    </div>
    {(Object.keys(FILTERS) as (keyof typeof FILTERS)[]).map((key) => <div key={key} className="tabPanel" role="tabpanel"
      id={`${id}-${key}-panel`} aria-labelledby={`${id}-${key}-tab`} hidden={tab !== key}>
      {key === 'reports' ? reportsVisited && <WorkspaceReports {...props} /> : tab === key && <>
      {updating && page && <p className="hint" role="status">{messages.uiAuditWorkspace.updating}</p>}
      {failure && <p className="error" role="alert">{failure}</p>}
      {!projectId ? <EmptyState text={messages.workspace.selectProjectFirst} />
        : page === null ? !failure && <LoadingSkeleton label={messages.runHistory.loading} rows={3} />
        : page.items.length === 0 ? <EmptyState text={messages.workspace.queueEmpty[key]} />
        : <ul className="pendingList">{page.items.map((item) => <li key={item.run_id}>
          <a className="pendingItem" href={routeHref('history', projectId, { runId: item.run_id })}>
            <span className="pendingItemTitle"><strong>{runHistoryTitle(item, messages.elements.unnamedRunTitle)}</strong>
              <small>{formatLocalTimestamp(item.started_at ?? item.created_at)} · <RunDuration run={item} /></small>
            </span><StatusBadge status={item.status} />
          </a>
        </li>)}</ul>}
      {projectId && <div className="pagination">
        {(offset > 0 || page?.has_more) && <>
          <button className="secondaryButton compactButton" disabled={offset === 0} type="button"
            onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}>{messages.runHistory.previous}</button>
          <button className="secondaryButton compactButton" disabled={!page?.has_more} type="button"
            onClick={() => setOffset((value) => value + PAGE_SIZE)}>{messages.runHistory.next}</button>
        </>}
        <button className="secondaryButton compactButton" type="button" disabled={updating}
          onClick={() => setRevision((value) => value + 1)}>{messages.runHistory.retry}</button>
      </div>}
      </>}
    </div>)}
  </section>
}
