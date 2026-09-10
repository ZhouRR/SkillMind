import { useState } from 'react'
import { useMessages } from '../i18n'
import type { useDocumentUpload } from '../hooks/useDocumentUpload'
import { uploadCounts } from '../lib/documentUpload'

/** 原 key を選択/コピー可能に保ち、失った File の再送 UI を作らない。 */
export function DocumentUploadStatus({ upload, canRead }: {
  upload: ReturnType<typeof useDocumentUpload>
  canRead: boolean
}) {
  const messages = useMessages()
  const labels = messages.documentsPanel.upload
  const failures = messages.documentsPanel.failures
  const counts = uploadCounts(upload.batch)
  if (counts.total === 0 && !upload.recovery && !upload.notice && !upload.checkFailure && !upload.checking) return null
  return <section className="documentUploadStatus" aria-label={labels.title}>
    <h3>{labels.title}</h3>
    {counts.total > 0 && <>
      <p role="status">{labels.summary(counts.published, counts.refused, counts.unknown, counts.queued, counts.closed)}</p>
      <ul className="documentUploadItems">
        {upload.batch.items.map((item) => <li key={item.original.uploadKey}>
          <strong>{item.original.label}</strong><p>{labels.phase[item.phase]}</p>
          <details className="detailDisclosure"><summary>{messages.elements.technicalDetails}</summary>
            <label>{labels.key}<input readOnly value={item.original.uploadKey}
              onFocus={(event) => event.currentTarget.select()} /></label>
            {item.document && <p>{labels.documentId}: {item.document.document_id}<br />{item.document.name}</p>}
          </details>
          {item.failure && <p className="error" role="alert">{failures[item.failure.key]}</p>}
        </li>)}
      </ul>
    </>}
    {counts.sending > 0 && <button type="button" className="secondaryButton" onClick={upload.cancelUpload}>
      {labels.cancelUpload}</button>}
    {counts.unknown > 0 && <>
      <p>{labels.unknownHint}</p>
      <button type="button" className="secondaryButton" onClick={upload.checkOriginal}
        disabled={upload.checking || upload.closing || upload.readDenied || !canRead}>{labels.checkOriginal}</button>
    </>}
    {upload.checking && upload.recovery?.phase !== 'checking' && <>
      <p role="status">{labels.checking}</p>
      <button type="button" className="secondaryButton" onClick={upload.cancelCheck}>{labels.cancelCheck}</button>
    </>}
    {upload.pendingReceipt && <p role="status">{labels.pending}</p>}
    {upload.checkFailure && <p className="error" role="alert">{failures[upload.checkFailure.key]}</p>}
    {upload.notice && <p className="error" role="alert">{failures[upload.notice.key]}</p>}
    {upload.batch.paused && counts.unknown === 0 && counts.sending === 0 && <button type="button"
      className="secondaryButton" disabled={!upload.canContinue()} onClick={upload.continueBatch}>
      {counts.queued > 0 ? labels.continueRemaining : labels.finishReview}</button>}
    {upload.recovery && <section className="documentUploadRecoveryResult panel" aria-label={labels.recoveryTitle}>
      <h4>{labels.recoveryTitle}</h4>
      <label>{labels.key}<input readOnly value={upload.recovery.original.uploadKey}
        onFocus={(event) => event.currentTarget.select()} /></label>
      <p className="hint">{labels.recoveryOnly}</p>
      {upload.recovery.phase === 'checking' && <>
        <p role="status">{labels.checking}</p>
        <button type="button" className="secondaryButton" onClick={upload.cancelCheck}>{labels.cancelCheck}</button>
      </>}
      {upload.recovery.phase === 'cancelled' && <p role="status">{labels.recoveryCancelled}</p>}
      {upload.recovery.record?.state === 'PENDING' && <p role="status">{labels.recoveryPending}</p>}
      {upload.recovery.record?.state === 'PUBLISHED' && <>
        <p role="status">{labels.phase.published}</p>
        <p>{labels.documentId}: {upload.recovery.record.document.document_id}<br />{upload.recovery.record.document.name}</p>
      </>}
      {upload.recovery.failure && <p className="error" role="alert">{failures[upload.recovery.failure.key]}</p>}
      <button type="button" className="secondaryButton" onClick={upload.closeRecovery}>{labels.closeRecovery}</button>
    </section>}
  </section>
}

/** 過去の要求の照会入口。進行中の batch 状態とは分け、結果は主画面へ表示する。 */
export function DocumentUploadRecovery({ upload, canRead }: {
  upload: ReturnType<typeof useDocumentUpload>
  canRead: boolean
}) {
  const labels = useMessages().documentsPanel.upload
  const [key, setKey] = useState('')
  return <details className="uploadRecovery"><summary>{labels.recover}</summary>
    <p className="hint">{labels.recoveryHint}</p>
    <form onSubmit={(event) => { event.preventDefault(); upload.recover(key) }}>
      <label>{labels.recoveryKey}<input value={key} onChange={(event) => setKey(event.currentTarget.value)}
        autoComplete="off" spellCheck={false} disabled={!upload.canRecover() || !canRead} /></label>
      <button type="submit" className="secondaryButton" disabled={!upload.canRecover() || !canRead}>
        {labels.recover}</button>
    </form></details>
}
