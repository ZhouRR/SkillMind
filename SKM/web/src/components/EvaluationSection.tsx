import { useEffect, useRef, useState, type FormEvent } from 'react'
import type { EvaluationRecord, EvaluationSubmissionReceipt, RunResultDetail } from '../api'
import type { SessionEnded } from '../hooks/useResourceRequest'
import { useEvaluationHistory } from '../hooks/useEvaluationHistory'
import { useEvaluationSubmission } from '../hooks/useEvaluationSubmission'
import { useMessages } from '../i18n'
import { evaluationInput, evaluationOriginal, evaluationReadBlocked, type EvaluationDraft, type EvaluationDraftError,
  type EvaluationFailure, type EvaluationScope } from '../lib/evaluationSubmission'
import { formatLocalTimestamp } from '../lib/presentation'
import { isNonNilUuid, sameUuid } from '../lib/validation'
import type { RunDetailState } from './RunResultPanel'
import '../styles/evaluations.css'
import { ModalDialog } from './PageElements'

/** 同じ所有者の detail 再読取では未決を保持し、別 Result へは移行しない。 */
export function RunEvaluations({ scope, state, csrfToken, readOnly, onSessionExpired, open, onClose, onOpen }: {
  scope: Omit<EvaluationScope, 'resultId'>; state: RunDetailState; csrfToken: string
  readOnly: boolean; onSessionExpired: SessionEnded
  open?: boolean; onClose?: () => void; onOpen?: () => void
}) {
  const messages = useMessages()
  const detail = 'detail' in state ? state.detail : undefined
  const valid = detail && sameUuid(detail.project_id, scope.projectId) && sameUuid(detail.run_id, scope.runId) ? detail : null
  const [saved, setSaved] = useState<{ source: typeof valid; result: RunResultDetail | null }>({ source: valid, result: valid?.result ?? null })
  if (valid && saved.source !== valid) setSaved({ source: valid, result: valid.result })
  const result = valid?.result ?? (state.status === 'ready' ? null : saved.result)
  if (!result) return null
  if (![scope.actorId, scope.projectId, scope.runId, result.result_id].every(isNonNilUuid)) {
    return <p className="error" role="alert">{messages.evaluation.failures.loadFailed}</p>
  }
  const failure: EvaluationFailure | null = state.status === 'error' && state.accessFailure
    ? { key: state.accessFailure.key } : readOnly ? { key: 'projectArchived' } : null
  return <EvaluationSection key={result.result_id.toLowerCase()}
    open={open} onClose={onClose} onOpen={onOpen}
    scope={{ ...scope, resultId: result.result_id }} result={result} csrfToken={csrfToken}
    writable={state.status === 'ready' && Boolean(valid?.result) && !readOnly}
    accessFailure={failure} onSessionExpired={onSessionExpired} />
}

/** 初回 form は評価でも承認でもない編集草稿として作る。 */
function emptyDraft(): EvaluationDraft { return { rating: 3, verdict: 'uncertain', comment: '', revisions: [] } }

/** 原要求受付記録、修訂 form、server cursor 履歴を一つの安定所有者の下に分離する。 */
export function EvaluationSection({ scope, result, csrfToken, writable, accessFailure, onSessionExpired, open = false, onClose = () => {}, onOpen }: {
  scope: EvaluationScope; result: RunResultDetail; csrfToken: string; writable: boolean
  accessFailure: EvaluationFailure | null; onSessionExpired: SessionEnded
  open?: boolean; onClose?: () => void; onOpen?: () => void
}) {
  const messages = useMessages()
  const labels = messages.evaluation
  const [draft, setDraft] = useState<EvaluationDraft>(emptyDraft)
  const [draftError, setDraftError] = useState<EvaluationDraftError | null>(null)
  const [savedReceipt, setSavedReceipt] = useState<EvaluationSubmissionReceipt | null>(null)
  const [lookupKey, setLookupKey] = useState('')
  const [lookupError, setLookupError] = useState(false)
  const nextRow = useRef(0)
  const submission = useEvaluationSubmission({ scope, csrfToken, resultData: result.data, writable,
    accessFailure, onSaved: setSavedReceipt, onSessionExpired })
  const history = useEvaluationHistory(scope.projectId, scope.runId, scope.resultId,
    submission.onReadSessionExpired, submission.rejectAccess)
  useEffect(() => {
    if (savedReceipt) history.remember(savedReceipt.evaluation)
  }, [savedReceipt, history.remember])
  const pending = submission.pending
  const locked = Boolean(pending) || !writable || Boolean(submission.denied)
  const denied = submission.denied
  const readable = !evaluationReadBlocked(denied)

  /** 全修訂の局所検証を通ってから一つの原要求を作る。 */
  function submit(event: FormEvent): void {
    event.preventDefault()
    if (locked) return
    const checked = evaluationInput(draft, result.data)
    setDraftError(checked.error)
    if (checked.input) {
      try { submission.start(checked.input) } catch { setDraftError({ key: 'invalidDraft' }) }
    }
  }

  /** 行 ID はローカル編集だけの識別子で、公開 revision 順序を入れ替えない。 */
  function updateRow(id: number, field: 'pointer' | 'value' | 'reason', value: string): void {
    if (locked) return
    setDraft((current) => ({ ...current, revisions: current.revisions.map((row) => row.id === id ? { ...row, [field]: value } : row) }))
  }

  /** 確認済み保存の後だけ新規 form を明示的に開始する。入力拒否なら草稿を残す。 */
  function nextDraft(): void {
    if (submission.clear()) {
      if (pending?.phase === 'confirmed') setDraft(emptyDraft())
      setDraftError(null); setLookupError(false); setLookupKey('')
    }
  }

  // 開閉は表示だけを変える。原要求・草稿・履歴の owner を drawer の外へ保つ。
  return <section className="evaluationSection">
    {!open && pending && <p className="evaluationNotice" role="status">{messages.runResult.manualEvaluation} · {labels.phase[pending.phase]}
      {onOpen && <button className="secondaryButton compactButton" type="button" onClick={onOpen}>{labels.originalRequest}</button>}</p>}
    {!open && denied && <p className="error" role="alert">{labels.failures[denied.key]}</p>}
    {!open && history.failure && !denied && <p className="error" role="alert">{labels.failures[history.failure.key]}</p>}
    <ModalDialog drawer wide open={open} title={messages.runResult.manualEvaluation} onClose={onClose}>
    <div className="resultCollapseBody">
      <p className="hint">{labels.hint}</p>
      {denied && <p className="error" role="alert">{labels.failures[denied.key]}</p>}
      {pending && <section className="evaluationSubmission" aria-label={labels.originalRequest}>
        <h4>{labels.originalRequest}</h4>
        <label>{labels.submissionKey}<input readOnly value={pending.request.key} onFocus={(event) => event.currentTarget.select()} /></label>
        <p role="status">{labels.phase[pending.phase]}</p>
        {pending.uncertain && pending.phase !== 'confirmed' && <p className="hint">{labels.unknownHint}</p>}
        {pending.request.body === null && <p className="hint">{labels.lookupOnly}</p>}
        {pending.failure && pending.failure.key !== denied?.key && <p className="error" role="alert">{labels.failures[pending.failure.key]}</p>}
        <div className="formRow">
          {pending.request.body === null && <button className="secondaryButton" type="button" onClick={submission.closeLookup}>{labels.closeLookup}</button>}
          {pending.phase === 'sending' || pending.phase === 'checking'
            ? <button className="secondaryButton" type="button" onClick={submission.cancel}>{labels.cancel}</button>
            : pending.phase !== 'confirmed' && <>
              <button className="secondaryButton" type="button" disabled={!readable} onClick={submission.confirm}>{labels.confirm}</button>
              {pending.phase === 'unknown' && pending.request.body !== null && <button className="secondaryButton" type="button"
                disabled={!writable || Boolean(denied)} onClick={submission.resend}>{labels.resend}</button>}
            </>}
          {(pending.phase === 'confirmed' || pending.phase === 'refused' && !pending.uncertain
            && ['invalidRequest', 'invalidRevision'].includes(pending.failure?.key ?? ''))
            && <button className="secondaryButton" type="button" onClick={nextDraft}>
              {pending.phase === 'confirmed' ? labels.newEvaluation : labels.editRejected}</button>}
        </div>
      </section>}
      <form className="evaluationForm" onSubmit={submit}>
        <fieldset disabled={locked}>
          <legend>{labels.draft}</legend>
          <div className="formRow">
            <label>{messages.runResult.ratingLabel}<select value={draft.rating} onChange={(event) => setDraft({ ...draft, rating: Number(event.target.value) })}>
              {[1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
            <label>{messages.runResult.verdictLabel}<select value={draft.verdict}
              onChange={(event) => setDraft({ ...draft, verdict: event.target.value as EvaluationDraft['verdict'] })}>
              {(['accurate', 'partially_accurate', 'inaccurate', 'uncertain'] as const).map((value) => <option key={value} value={value}>{messages.enums.verdict[value]}</option>)}
            </select></label>
          </div>
          {/* UTF-16 入力欄は 2 符号単位まで許可し、公開上限は送信前に Unicode 符号位置で検証する。 */}
          <label>{messages.runResult.commentLabel}<textarea aria-label={messages.runResult.commentLabel} maxLength={8000} value={draft.comment}
            onChange={(event) => setDraft({ ...draft, comment: event.target.value })} /></label>
          <p className="hint">{labels.revisionHint}</p>
          {draft.revisions.map((row, index) => {
            const original = evaluationOriginal(result.data, row.pointer)
            return <fieldset className="evaluationRevisionEditor" key={row.id}>
              <legend>{labels.revisionNumber(index + 1)}</legend>
              <label>{messages.runResult.jsonPointerLabel}<input maxLength={1024} required value={row.pointer}
                onChange={(event) => updateRow(row.id, 'pointer', event.target.value)} placeholder="/summary" /></label>
              {original.found && <div className="evaluationOriginal"><span>{messages.runResult.aiOriginal}</span><pre>{displayValue(original.value)}</pre></div>}
              <label>{messages.runResult.suggestedValueLabel}<textarea aria-label={messages.runResult.suggestedValueLabel} required value={row.value}
                onChange={(event) => updateRow(row.id, 'value', event.target.value)} /></label>
              <label>{messages.runResult.revisionReasonLabel}<textarea aria-label={messages.runResult.revisionReasonLabel} required maxLength={2000} value={row.reason}
                onChange={(event) => updateRow(row.id, 'reason', event.target.value)} /></label>
              <button className="secondaryButton" type="button" onClick={() => setDraft({ ...draft, revisions: draft.revisions.filter((item) => item.id !== row.id) })}>{labels.removeRevision}</button>
            </fieldset>
          })}
          <button className="secondaryButton" type="button" disabled={draft.revisions.length >= 100}
            onClick={() => setDraft((current) => current.revisions.length >= 100 ? current : { ...current,
              revisions: [...current.revisions, { id: nextRow.current++, pointer: '', value: '', reason: '' }] })}>{messages.runResult.addRevision}</button>
          {draftError && <p className="error" role="alert">
            {draftError.row !== undefined && `${labels.revisionNumber(draftError.row + 1)}: `}{labels.draftErrors[draftError.key]}</p>}
          <button className="secondaryButton" type="submit">{messages.runResult.addEvaluation}</button>
        </fieldset>
      </form>
      {!pending && <form className="evaluationLookup" onSubmit={(event) => { event.preventDefault(); setLookupError(!submission.lookup(lookupKey.trim())) }}>
        <label>{labels.lookupLabel}<input value={lookupKey} onChange={(event) => setLookupKey(event.target.value)} autoComplete="off" spellCheck={false} /></label>
        <p className="hint">{labels.lookupHint}</p>
        <button className="secondaryButton" type="submit" disabled={!readable}>{labels.confirm}</button>
        {lookupError && <p className="error" role="alert">{labels.invalidKey}</p>}
      </form>}
      <section className="evaluationHistory" aria-label={labels.history}>
        <h4>{labels.history}</h4><p className="hint">{labels.historyHint}</p>
        {history.pending && <p role="status">{messages.runResult.loadingEvaluations}</p>}
        {history.failure && <p className="error" role="alert">{labels.failures[history.failure.key]}</p>}
        {readable && <>
          {!history.pending && !history.failure && history.items.length === 0 && <p>{messages.runResult.noEvaluations}</p>}
          <ol className="evaluationList">{history.items.map((item) => <EvaluationItem item={item} key={item.evaluation_id} />)}</ol>
        </>}
        <div className="formRow">
          <button className="secondaryButton" type="button" disabled={history.pending || !readable} onClick={history.refresh}>{labels.refreshHistory}</button>
          {history.nextCursor && <button className="secondaryButton" type="button" disabled={history.pending || Boolean(history.failure) || !readable} onClick={history.more}>{labels.loadMore}</button>}
        </div>
      </section>
    </div>
    </ModalDialog>
  </section>
}

/** AI 原値と人の提案を分けて表示し、採用済みや最新の正式判断とは呼ばない。 */
function EvaluationItem({ item }: { item: EvaluationRecord }) {
  const messages = useMessages()
  return <li className="evaluationItem" data-evaluation-id={item.evaluation_id}>
    <div><strong>{item.rating}/5 · {messages.enums.verdict[item.verdict]}</strong><time>{formatLocalTimestamp(item.created_at)}</time></div>
    <code>{item.user_id}</code>
    {item.comment && <p>{item.comment}</p>}
    {item.revisions.map((revision) => <div className="evaluationRevision" key={revision.pointer}>
      <code>{revision.pointer}</code>
      <div><div><span>{messages.runResult.aiOriginal}</span><pre>{displayValue(revision.original_value)}</pre></div>
        <div><span>{messages.runResult.humanSuggestion}</span><pre>{displayValue(revision.suggested_value)}</pre></div></div>
      <small>{revision.reason}</small>
    </div>)}
  </li>
}

/** JSON null/文字列/構造の区別を保持し、HTML として解釈しない。 */
function displayValue(value: unknown): string { return JSON.stringify(value, null, 2) ?? '—' }
