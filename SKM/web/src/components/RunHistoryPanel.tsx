import type { RunHistoryItemRecord, RunHistoryPageRecord } from '../api'
import { useMessages } from '../i18n'
import { normalizeSelectedSources } from '../lib/agentStream'
import { formatLocalTimestamp, runHistoryTitle } from '../lib/presentation'
import { EmptyState, LoadingSkeleton, StatusBadge } from './PageElements'

/** Project Run history 読み込みの排他的 UI state。 */
export type RunHistoryState =
  | { status: 'loading' }
  | { status: 'ready'; page: RunHistoryPageRecord }
  | { status: 'error'; message: string }

/** Project 内の Run history、pagination、再表示 action を提供する。 */
export function RunHistoryPanel({ state, selectedRunId, onOpen, onPrevious, onNext, onRefresh }: {
  state: RunHistoryState
  selectedRunId: string | null
  onOpen: (item: RunHistoryItemRecord) => void
  onPrevious: () => void
  onNext: () => void
  onRefresh: () => void
}) {
  const messages = useMessages()
  if (state.status === 'loading') {
    return <LoadingSkeleton label={messages.runHistory.loading} rows={3} />
  }
  if (state.status === 'error') {
    return <div><p className="error" role="alert">{state.message}</p><button className="secondaryButton" type="button" onClick={onRefresh}>{messages.runHistory.retry}</button></div>
  }
  if (state.page.items.length === 0) return <EmptyState text={messages.runHistory.empty} />
  return (
    <>
      <div className="historyList">
        {state.page.items.map((item) => (
          <article className={`historyItem${selectedRunId === item.run_id ? ' historySelected' : ''}`} key={item.run_id}>
            <button type="button" onClick={() => onOpen(item)}>
              <div className="historyPrimary">
                <span><strong>{runHistoryTitle(item.result_summary, item.run_id, messages.elements.runFallbackTitle)}</strong><small>{formatLocalTimestamp(item.created_at)}</small></span>
                <StatusBadge status={item.status} />
              </div>
              {!item.result_summary && <p>{messages.runHistory.noSummary}</p>}
              <div className="historyMeta">
                <span>{sourceLabel(item, messages.runHistory.sourceUnavailable)}</span>
                <span>{item.result_confidence === null ? '—' : `${Math.round(item.result_confidence * 100)}%`}</span>
                {/* 完全な UUID は一覧では雑音になるため短縮表示し、全文は title(hover)へ退避する。 */}
                <code title={item.run_id}>{item.run_id.slice(0, 8)}</code>
              </div>
            </button>
          </article>
        ))}
      </div>
      <div className="historyPagination">
        <button className="secondaryButton compactButton" disabled={state.page.offset === 0} type="button" onClick={onPrevious}>{messages.runHistory.previous}</button>
        <span>{state.page.offset + 1}–{state.page.offset + state.page.items.length}</span>
        <button className="secondaryButton compactButton" disabled={!state.page.has_more} type="button" onClick={onNext}>{messages.runHistory.next}</button>
      </div>
    </>
  )
}

/** History item の選択 source を、歴史 flat snapshot と現行 object snapshot の双方から短く表示する。 */
function sourceLabel(item: RunHistoryItemRecord, unavailable: string): string {
  const providers = Object.values(normalizeSelectedSources(item.selected_sources))
  return providers.join(' / ') || unavailable
}
