import { useMessages } from '../i18n'
import { routeHref } from '../lib/routing'
import type { PendingRunSubmission } from '../lib/runSubmission'

/** 原要求の確認を草稿の編集と区別する。本文を再表示/永続化せず、元 task と操作の違いを示す。 */
export function RunSubmissionPanel({ pending, acknowledgePrevious, onAcknowledge, onRetry }: {
  pending: PendingRunSubmission
  acknowledgePrevious: boolean
  onAcknowledge: (value: boolean) => void
  onRetry: () => void
}) {
  const { submission: messages } = useMessages().workspace
  const sending = pending.phase === 'sending'
  return (
    <section className="submissionNotice" aria-label={messages.title}>
      <div role="status" aria-live="polite">
        <h3>{messages.title}</h3>
        <p>{messages.originalTask(pending.request.taskTitle)}</p>
        <p>{messages.phase[pending.phase]}</p>
        {pending.detail && <p className="error">{pending.detail}</p>}
        {!sending && pending.mayHaveCreated && <p>{messages.mayHaveCreated}</p>}
      </div>
      <p className="hint">{messages.memoryOnly}</p>
      <div className="formRow">
        {pending.phase !== 'conflict' && (
          <button className="secondaryButton" type="button" disabled={sending} onClick={onRetry}>
            {messages.retry}
          </button>
        )}
        <a href={routeHref('history', pending.request.projectId)} target="_blank" rel="noopener noreferrer">
          {messages.history}
        </a>
      </div>
      {!sending && (
        <label className="submissionAcknowledgement">
          <input type="checkbox" checked={acknowledgePrevious} onChange={(event) => onAcknowledge(event.target.checked)} />
          <span>{messages.acknowledgeNew}</span>
        </label>
      )}
    </section>
  )
}
