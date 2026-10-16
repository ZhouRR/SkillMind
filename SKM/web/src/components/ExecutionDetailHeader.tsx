import type { RunDetailRecord, RunRecord } from '../api'
import { useMessages } from '../i18n'
import { formatLocalTimestamp } from '../lib/presentation'
import { routeHref } from '../lib/routing'
import { LoadingSkeleton, StatusBadge } from './PageElements'
import { RunDuration } from './RunDuration'

/** 実行の識別・状態・時刻を一つの見出しへ集約し、読取/取消の handler は親に残す。 */
export function ExecutionDetailHeader({ projectId, run, detail, title, loading, cancelling, onRetry, onRefresh, onCancel }: {
  projectId: string; run: RunRecord | null; detail?: RunDetailRecord; title: string | null
  loading: boolean; cancelling: boolean; onRetry: () => void; onRefresh: () => void; onCancel: () => void
}) {
  const messages = useMessages()
  const active = run && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(run.status)
  return <header className="executionHeader">
    <nav className="executionBreadcrumb" aria-label={messages.workspace.executionDetail}>
      <a href={routeHref('history', projectId)}><span aria-hidden="true">‹</span>{messages.routes.history.label}</a>
      <span aria-hidden="true">/</span><span>{messages.workspace.executionDetail}</span>
    </nav>
    <div className="executionTitleRow">
      <div className="executionIdentity"><h1>{title ?? messages.workspace.executionDetail}</h1>{run && <StatusBadge status={run.status} />}</div>
      {active && <div className="executionControls">
        <button className="secondaryButton compactButton" type="button" onClick={onRefresh}>{messages.workspace.refreshDb}</button>
        <button className="secondaryButton compactButton" disabled={cancelling} type="button" onClick={onCancel}>
          {cancelling ? messages.workspace.cancelling : messages.workspace.cancelRun}
        </button>
      </div>}
    </div>
    {run ? <div className="executionMetadata">
      <time dateTime={run.created_at}>{formatLocalTimestamp(run.created_at)}</time>
      <RunDuration run={detail ?? run} />
      <dl className="runFacts"><div><dt>{messages.workspace.runIdLabel}</dt><dd title={run.run_id}><code>{run.run_id.slice(0, 8)}</code></dd></div></dl>
    </div> : loading ? <LoadingSkeleton label={messages.runHistory.loading} rows={1} />
      : <button className="secondaryButton compactButton" type="button" onClick={onRetry}>{messages.runHistory.retry}</button>}
  </header>
}
