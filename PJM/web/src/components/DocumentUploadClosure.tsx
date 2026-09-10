import { useState } from 'react'
import { useMessages } from '../i18n'
import type { useDocumentUpload } from '../hooks/useDocumentUpload'
import type { useDocumentUploadClosure } from '../hooks/useDocumentUploadClosure'

/** 停止と読取を分けて表示し、削除や返金の完了という表示を作らない。 */
export function DocumentUploadClosure({ upload, closure }: {
  upload: ReturnType<typeof useDocumentUpload>
  closure: ReturnType<typeof useDocumentUploadClosure>
}) {
  const messages = useMessages().documentsPanel
  const labels = messages.closure
  const [key, setKey] = useState('')
  const original = upload.batch.items.find((item) => item.phase === 'unknown')?.original
  const state = closure.state
  const recovery = closure.recovery
  const candidate = upload.recovery?.phase === 'settled' && upload.recovery.record?.state === 'PENDING'
    && !upload.recovery.failure ? upload.recovery.original : null
  return <section className="documentUploadClosure" aria-label={labels.title}>
    <h3>{labels.title}</h3><p className="hint">{labels.scope}</p>
    {original && !closure.locked() && <div className="buttonRow">
      <button type="button" className="secondaryButton" onClick={() => closure.prepare(original)}
        disabled={!closure.writable()}>{labels.prepare}</button>
      <button type="button" className="secondaryButton" onClick={() => closure.check(original)}
        disabled={!closure.readable()}>{labels.check}</button>
    </div>}
    {candidate && !closure.locked() && <div className="documentUploadClosureCandidate">
      <p>{labels.recoveredHint}</p>
      <button type="button" className="secondaryButton" onClick={() => closure.prepareRecovery(candidate)}
        disabled={!closure.writable() || upload.isLocked()}>{labels.prepareRecovered}</button>
    </div>}
    {state && <section className="documentUploadClosureIntent panel" aria-label={labels.original}>
      <h4>{state.original.label}</h4>
      <label>{messages.upload.key}<input readOnly value={state.original.uploadKey}
        onFocus={(event) => event.currentTarget.select()} /></label>
      <p role="status">{labels.phase[state.phase]}</p>
      {state.phase === 'confirming' && <>
        <p>{labels.confirmHint}</p><div className="buttonRow">
          <button type="button" className="dangerButton" onClick={closure.submit} disabled={!closure.writable()}>{labels.confirm}</button>
          <button type="button" className="secondaryButton" onClick={closure.cancelPreparation}>{labels.cancelPreparation}</button>
        </div>
      </>}
      {state.phase === 'sending' && <button type="button" className="secondaryButton" onClick={closure.cancel}>{labels.cancelWait}</button>}
      {state.phase === 'unknown' && <>
        <p>{labels.unknownHint}</p>
        <button type="button" className="secondaryButton" onClick={() => closure.check(state.original)}
          disabled={closure.checking || !closure.readable()}>{labels.check}</button>
      </>}
      {closure.checking && <><p role="status">{labels.checking}</p>
        <button type="button" className="secondaryButton" onClick={closure.cancelCheck}>{labels.cancelCheck}</button></>}
      {state.receipt && <p>{labels.documentId}: {state.receipt.document_id}<br />{labels.closedAt}: {state.receipt.closed_at}</p>}
      {state.failure && <p role="alert" className="error">{labels.failures[state.failure.key]}</p>}
      {state.kind === 'recovery' && ['closed', 'refused'].includes(state.phase) && <button type="button"
        className="secondaryButton" onClick={closure.finishRecovery}>{labels.finishRecovery}</button>}
    </section>}
    <details className="documentUploadClosureRecovery"><summary>{labels.recover}</summary>
      <p className="hint">{labels.recoveryHint}</p>
      <form onSubmit={(event) => { event.preventDefault(); recovery.lookup(key) }}>
        <label>{messages.upload.recoveryKey}<input value={key} onChange={(event) => setKey(event.currentTarget.value)}
          autoComplete="off" spellCheck={false} disabled={!closure.readable()} /></label>
        <button type="submit" className="secondaryButton" disabled={!closure.readable()}>{labels.recover}</button>
      </form>
      {recovery.failure && <p role="alert" className="error">{labels.failures[recovery.failure.key]}</p>}
      {recovery.key && <section className="documentUploadClosureRecoveryResult panel" aria-label={labels.recoveryTitle}>
        <label>{messages.upload.key}<input readOnly value={recovery.key} onFocus={(event) => event.currentTarget.select()} /></label>
        {recovery.pending && <p role="status">{labels.checking}</p>}
        {recovery.receipt && <><p role="status">{labels.phase.closed}</p>
          <p>{labels.documentId}: {recovery.receipt.document_id}<br />{labels.closedAt}: {recovery.receipt.closed_at}</p></>}
        <button type="button" className="secondaryButton" onClick={recovery.close}>{labels.closeRecovery}</button>
      </section>}
    </details>
  </section>
}
