import { useCallback, useEffect, useState } from 'react'

import { loadRunDetail, loadRunHistory, type PublishedTaskRecord, type RunStatus } from '../api'
import { useResourceQuery, type SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { INTERACTION_REQUEST_POLICY } from '../lib/interactionResponse'
import { formatLocalTimestamp } from '../lib/presentation'
import { routeHref } from '../lib/routing'
import { EmptyState, LoadingSkeleton, StatusBadge } from './PageElements'
import { RunResultPanel } from './RunResultPanel'

const FINISHED: readonly RunStatus[] = ['SUCCEEDED', 'FAILED', 'CANCELLED']

/** Module の精確 Task ごとに直近の終端 Run を読み、旧成功結果へ差し替えず表示する。 */
export function WorkspaceReports({ projectId, tasks, actorId, csrfToken, projectReadOnly, onSessionExpired }: {
  projectId: string; tasks: PublishedTaskRecord[]; actorId: string; csrfToken: string;
  projectReadOnly: boolean; onSessionExpired: SessionEnded
}) {
  const messages = useMessages()
  const [selected, setSelected] = useState('')
  const task = tasks.find((item) => item.task_id === selected)
    ?? [...tasks].filter((item) => item.last_run && FINISHED.includes(item.last_run.status))
      .sort((a, b) => b.last_run!.created_at.localeCompare(a.last_run!.created_at))[0] ?? tasks[0]
  const taskId = task?.task_id ?? ''
  const loader = useCallback(async (signal: AbortSignal) => {
    const page = await loadRunHistory(projectId, 1, 0, signal, FINISHED, taskId)
    const latest = page.items[0]
    if (!latest) return null
    if (latest.project_id !== projectId || latest.task_id !== taskId || !FINISHED.includes(latest.status)) {
      throw new Error('Latest task run does not match the selected scope')
    }
    const detail = await loadRunDetail(projectId, latest.run_id, signal)
    if (detail.task_id !== taskId || !FINISHED.includes(detail.status)) {
      throw new Error('Latest task detail does not match the selected scope')
    }
    return { latest, detail }
  }, [projectId, taskId])
  const query = useResourceQuery(JSON.stringify([actorId, csrfToken, projectId, taskId]), loader,
    onSessionExpired, INTERACTION_REQUEST_POLICY, Boolean(projectId && taskId))
  useEffect(() => {
    const timer = window.setInterval(query.refresh, 30000)
    return () => window.clearInterval(timer)
  }, [query.refresh])
  const detail = query.data?.detail
  return <div className="workspaceReports">
    <div className="reportToolbar">
      <label>{messages.workspace.taskLabel}
        <select value={taskId} disabled={!tasks.length} onChange={(event) => setSelected(event.target.value)}>
          {!tasks.length && <option value="">{messages.workspace.noModuleTasks}</option>}
          {tasks.map((item) => <option key={item.task_id} value={item.task_id}>{item.title} · v{item.version}</option>)}
        </select>
      </label>
      <button className="secondaryButton compactButton" type="button" disabled={query.pending || !taskId}
        onClick={query.refresh}>{messages.runHistory.retry}</button>
    </div>
    {!taskId ? <EmptyState text={messages.workspace.noModuleTasks} />
      : !detail && query.failure ? <p className="error" role="alert">{messages.interactionResponse.failures[query.failure.key]}</p>
        : !detail && query.pending ? <LoadingSkeleton label={messages.runHistory.loading} rows={3} />
          : !detail ? <EmptyState text={messages.workspace.latestReportEmpty} />
            : <>
              <div className="reportRunMeta">
                <span>{messages.workspace.latestReport} · <time dateTime={query.data!.latest.created_at}>
                  {formatLocalTimestamp(query.data!.latest.created_at)}</time></span>
                <StatusBadge status={detail.status} />
                <a className="secondaryButton compactButton" href={routeHref('history', projectId, { runId: detail.run_id })}>
                  {messages.workspace.executionDetail}</a>
              </div>
              <RunResultPanel state={query.failure
                ? { status: 'error', detail, message: messages.interactionResponse.failures[query.failure.key] }
                : { status: 'ready', detail }} actorId={actorId} csrfToken={csrfToken}
                projectId={projectId} runId={detail.run_id} projectReadOnly={projectReadOnly} reportOnly
                onSessionExpired={onSessionExpired} />
            </>}
  </div>
}
