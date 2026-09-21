import { RunDuration } from './RunDuration'
import type { RunHistoryItemRecord, RunHistoryPageRecord } from '../api'
import { useMessages } from '../i18n'
import { normalizeSelectedSources } from '../lib/agentStream'
import { formatLocalTimestamp, runHistoryTitle } from '../lib/presentation'
import { EmptyState, LoadingSkeleton, StatusBadge } from './PageElements'
import { ActionMenu } from './ActionMenu'

/** Project Run history 読み込みの排他的 UI state。 */
export type RunHistoryState =
  | { status: 'loading' }
  | { status: 'ready'; page: RunHistoryPageRecord }
  | { status: 'error'; message: string }

/** Project 内の Run history、pagination、再表示 action を提供する。 */
export function RunHistoryPanel({ state, selectedRunId, onOpen, onPrevious, onNext, onRefresh, onDelete, onPurge, trashed = false }: {
  state: RunHistoryState
  selectedRunId: string | null
  onOpen: (item: RunHistoryItemRecord) => void
  onPrevious: () => void
  onNext: () => void
  onRefresh: () => void
  onDelete?: (item: RunHistoryItemRecord) => void
  onPurge?: (item: RunHistoryItemRecord) => void
  trashed?: boolean
}) {
  const messages = useMessages()
  if (state.status === 'loading') {
    return <LoadingSkeleton label={messages.runHistory.loading} rows={3} />
  }
  if (state.status === 'error') {
    return <div><p className="error" role="alert">{state.message}</p><button className="secondaryButton" type="button" onClick={onRefresh}>{messages.runHistory.retry}</button></div>
  }
  const empty = state.page.items.length === 0
  return (
    <>
      {empty && <EmptyState text={state.page.offset > 0 ? messages.runHistory.emptyPage
        : trashed ? messages.runHistory.emptyTrash : messages.runHistory.empty} />}
      <div className="historyList">
        {state.page.items.map((item) => (
          <article className={`historyItem${selectedRunId === item.run_id ? ' historySelected' : ''}${onDelete || (trashed && onPurge) ? ' historyHasActions' : ''}`} key={item.run_id}>
            <button type="button" onClick={() => onOpen(item)}>
              <div className="historyPrimary">
                <span><strong>{runHistoryTitle(item, messages.elements.unnamedRunTitle)}</strong><small>{formatLocalTimestamp(item.started_at ?? item.created_at)} · <RunDuration run={item} /></small></span>
                <StatusBadge status={item.status} />
              </div>
              {item.task_title && item.result_summary && <p className="historySummary">{item.result_summary}</p>}
              {!item.result_summary && <p>{messages.runHistory.noSummary}</p>}
              <div className="historyMeta">
                <span>{sourceLabel(item, messages.runHistory.sourceUnavailable)}</span>
              </div>
            </button>
            {(onDelete || (trashed && onPurge)) && <div className="historyDelete"><ActionMenu label={messages.common.moreActions(runHistoryTitle(item, messages.elements.unnamedRunTitle))} items={[
              ...(onDelete ? [{ id: trashed ? 'restore' : 'trash', label: trashed ? messages.fileManagement.restore : messages.fileManagement.trashAction,
                onSelect: () => onDelete(item), disabled: !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(item.status), danger: !trashed }] : []),
              ...(trashed && onPurge ? [{ id: 'purge', label: messages.fileManagement.purge, onSelect: () => onPurge(item), danger: true, separatorBefore: Boolean(onDelete) }] : []),
            ]} /></div>}
            <details className="detailDisclosure historyTechnical"><summary>{messages.elements.technicalDetails}</summary>
              <p><code>{item.run_id}</code></p>
              {item.result_confidence !== null && <p>{messages.runResult.reading.confidenceHint} {Math.round(item.result_confidence * 100)}%</p>}
            </details>
          </article>
        ))}
      </div>
      {(!empty || state.page.offset > 0) && <div className="historyPagination">
        <button className="secondaryButton compactButton" disabled={state.page.offset === 0} type="button" onClick={onPrevious}>{messages.runHistory.previous}</button>
        {!empty && <span>{state.page.offset + 1}–{state.page.offset + state.page.items.length}</span>}
        <button className="secondaryButton compactButton" disabled={!state.page.has_more} type="button" onClick={onNext}>{messages.runHistory.next}</button>
      </div>}
    </>
  )
}

/** History item の選択 source を、歴史 flat snapshot と現行 object snapshot の双方から短く表示する。 */
function sourceLabel(item: RunHistoryItemRecord, unavailable: string): string {
  const providers = Object.values(normalizeSelectedSources(item.selected_sources))
  return providers.join(' / ') || unavailable
}
