import { useEffect, useRef, useState } from 'react'

import { loadRunHistory, type RunHistoryItemRecord } from '../api'
import { RunHistoryPanel, type RunHistoryState } from '../components/RunHistoryPanel'
import { PageHeader } from '../components/PageElements'
import { useMessages } from '../i18n'
import { routeHref } from '../lib/routing'

const HISTORY_PAGE_SIZE = 20

/** Project 全体の実行記録を探し、選択した記録を観測用 Workspace へ渡す画面。 */
export function HistoryPage({ projectId }: { projectId: string }) {
  const messages = useMessages()
  const [state, setState] = useState<RunHistoryState>({ status: 'loading' })
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
    setState((current) => current.status === 'ready' ? current : { status: 'loading' })
    void loadRunHistory(projectId, HISTORY_PAGE_SIZE, offset, controller.signal)
      .then((page) => setState({ status: 'ready', page }))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setState({
            status: 'error',
            message: error instanceof Error ? error.message : messages.runHistory.loading,
          })
        }
      })
    return () => controller.abort()
  }, [messages.runHistory.loading, messages.workspace.selectProjectFirst, offset, projectId, revision])

  useEffect(() => {
    setOffset(0)
  }, [projectId])

  const openRun = (item: RunHistoryItemRecord): void => {
    window.location.hash = routeHref('workspace', projectId, { runId: item.run_id })
  }

  return (
    <>
      <PageHeader
        title={messages.routes.history.label}
        description={messages.historyPage.description}
        aside={<span className="scopeBadge">{messages.historyPage.scopeBadge}</span>}
      />
      <section className="panel historyPage" aria-label={messages.historyPage.aria}>
        <div className="panelHeader">
          <div>
            <h2>{messages.routes.history.label}</h2>
            <p className="hint">{messages.historyPage.hint}</p>
          </div>
          <div className="formRow">
            <button className="secondaryButton compactButton" type="button" onClick={() => setRevision((current) => current + 1)}>
              {messages.runHistory.retry}
            </button>
            <a className="secondaryButton compactButton" href={routeHref('workspace', projectId)}>
              {messages.historyPage.openWorkspace}
            </a>
          </div>
        </div>
        <RunHistoryPanel
          state={state}
          selectedRunId={null}
          onOpen={openRun}
          onPrevious={() => setOffset((current) => Math.max(0, current - HISTORY_PAGE_SIZE))}
          onNext={() => setOffset((current) => current + HISTORY_PAGE_SIZE)}
          onRefresh={() => setRevision((current) => current + 1)}
        />
      </section>
    </>
  )
}
