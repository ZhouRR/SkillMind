import { useEffect, useRef, useState } from 'react'

import { loadRunHistory, type RunHistoryItemRecord } from '../api'
import { RunDeletionDialog } from '../components/RunDeletionDialog'
import { RunHistoryPanel, type RunHistoryState } from '../components/RunHistoryPanel'
import { PageHeader } from '../components/PageElements'
import { useMessages } from '../i18n'
import { routeHref } from '../lib/routing'

const HISTORY_PAGE_SIZE = 20

/** Project 全体の実行記録を探し、選択した記録を同じ履歴画面の詳細 へ渡す画面。 */
export function HistoryPage({ projectId, csrfToken, readOnly = false }: { projectId: string; csrfToken: string; readOnly?: boolean }) {
  const messages = useMessages()
  const [state, setState] = useState<RunHistoryState>({ status: 'loading' })
  const [trashed, setTrashed] = useState(false)
  const [deleting, setDeleting] = useState<{ id: string; action: 'TRASH' | 'RESTORE' | 'PURGE' } | null>(null)
  const [offset, setOffset] = useState(0)
  const [revision, setRevision] = useState(0)
  const controllerRef = useRef<AbortController | null>(null)

  useEffect(() => {
    controllerRef.current?.abort()
    if (!projectId) {
      setState({ status: 'error', message: messages.workspace.selectProjectFirst })
      return
    }
    const controller = new AbortController()
    controllerRef.current = controller
    setState({ status: 'loading' })
    void loadRunHistory(projectId, HISTORY_PAGE_SIZE, offset, controller.signal, [], undefined, trashed)
      .then((page) => { if (!controller.signal.aborted) setState({ status: 'ready', page }) })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setState({
            status: 'error',
            message: error instanceof Error ? error.message : messages.runHistory.loading,
          })
        }
      })
    return () => controller.abort()
  }, [messages.runHistory.loading, messages.workspace.selectProjectFirst, offset, projectId, revision, trashed])

  useEffect(() => {
    setOffset(0)
    setDeleting(null)
  }, [projectId, trashed])

  const openRun = (item: RunHistoryItemRecord): void => {
    window.location.hash = routeHref('history', projectId, { runId: item.run_id })
  }

  return (
    <>
      <PageHeader
        title={messages.routes.history.label}
        aside={<span className="scopeBadge">{messages.historyPage.scopeBadge}</span>}
      />
      <section className="panel historyPage" aria-label={messages.historyPage.aria}>
        <div className="panelHeader">
          <h2>{messages.routes.history.label}</h2>
          <div className="formRow historyManagementToolbar">
            <button className="secondaryButton compactButton" type="button" aria-pressed={!trashed} onClick={() => setTrashed(false)}>{messages.fileManagement.active}</button>
            <button className="secondaryButton compactButton" type="button" aria-pressed={trashed} onClick={() => setTrashed(true)}>{messages.fileManagement.trash}</button>
            <button className="secondaryButton compactButton" type="button" onClick={() => setRevision((current) => current + 1)}>
              {messages.runHistory.retry}
            </button>
          </div>
        </div>
        {trashed && <p className="hint">{messages.fileManagement.recycleHint}</p>}
        <RunHistoryPanel
          state={state}
          trashed={trashed}
          onDelete={readOnly ? undefined : (item) => setDeleting({ id: item.run_id, action: trashed ? 'RESTORE' : 'TRASH' })}
          onPurge={readOnly ? undefined : (item) => setDeleting({ id: item.run_id, action: 'PURGE' })}
          selectedRunId={null}
          onOpen={openRun}
          onPrevious={() => setOffset((current) => Math.max(0, current - HISTORY_PAGE_SIZE))}
          onNext={() => setOffset((current) => current + HISTORY_PAGE_SIZE)}
          onRefresh={() => setRevision((current) => current + 1)}
        />
      </section>
      {deleting && <RunDeletionDialog key={`${projectId}:${deleting.id}:${deleting.action}:${csrfToken}:${trashed}`} projectId={projectId} runId={deleting.id} csrfToken={csrfToken} action={deleting.action}
        onClose={() => { setDeleting(null); setRevision((r) => r + 1) }} onChanged={() => { setDeleting(null); setOffset(0); setRevision((r) => r + 1) }} />}
    </>
  )
}
