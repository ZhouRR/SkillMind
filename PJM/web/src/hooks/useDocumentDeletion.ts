import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { ApiProblemError, deleteProjectDocument, loadProjectDocument, type ProjectDocumentRecord } from '../api'
import { DOCUMENT_REQUEST_POLICY, documentFailure, type DocumentFailure } from '../lib/documentFeedback'
import { useResourceMutation, useResourceQuery, type SessionEnded } from './useResourceRequest'

/** 現在目录の観察であり、原 DELETE や blob 清理の受理回执ではない。 */
type DocumentFacts = { status: 'present'; document: ProjectDocumentRecord } | { status: 'absent' }

/** 呼出元は actor/CSRF/Project の変更で owner を remount する。未知は同 owner 内で保持する。 */
export function useDocumentDeletion({ projectId, csrfToken, readOnly, onSessionEnded, onDeleted }: {
  projectId: string; csrfToken: string; readOnly: boolean; onSessionEnded: SessionEnded; onDeleted: () => void
}) {
  const [intent, setIntent] = useState<ProjectDocumentRecord | null>(null)
  const intentRef = useRef<ProjectDocumentRecord | null>(null)
  const [phase, setPhase] = useState<'idle' | 'sending' | 'unknown'>('idle')
  const phaseRef = useRef(phase)
  const [denied, setDenied] = useState<DocumentFailure | null>(null)
  const deniedRef = useRef<DocumentFailure | null>(null)
  const readOnlyRef = useRef(readOnly)
  readOnlyRef.current = readOnly
  const [ticket, setTicket] = useState<{ original: ProjectDocumentRecord } | null>(null)
  const ticketRef = useRef(ticket)
  const mutation = useResourceMutation(() => {
    deniedRef.current = { key: 'sessionExpired' }
    setDenied(deniedRef.current)
    onSessionEnded()
  }, DOCUMENT_REQUEST_POLICY)

  /** 一つの読取の拒否だけでも書込資格を閉じ、別の古い成功で解除しない。 */
  const observeFailure = useCallback((error: unknown) => {
    const reason = documentFailure(error, false)
    if (!['sessionExpired', 'denied', 'archived'].includes(reason.key)) return
    deniedRef.current = reason
    setDenied(reason)
    ticketRef.current = null
    mutation.interrupt()
    if (reason.key === 'sessionExpired') onSessionEnded()
  }, [mutation.interrupt, onSessionEnded])

  const loader = useCallback(async (signal: AbortSignal): Promise<DocumentFacts> => {
    if (!ticket || ticketRef.current !== ticket || deniedRef.current) throw new Error('No current document review')
    try {
      return { status: 'present', document: await loadProjectDocument(projectId, ticket.original.document_id, signal) }
    } catch (error: unknown) {
      if (!signal.aborted && ticketRef.current === ticket) {
        if (error instanceof ApiProblemError && error.status === 404 && error.code === 'document_not_found') {
          return { status: 'absent' }
        }
        observeFailure(error)
      }
      throw error
    }
  }, [projectId, ticket, observeFailure])
  const query = useResourceQuery(`${projectId}:${intent?.document_id ?? ''}`, loader,
    onSessionEnded, DOCUMENT_REQUEST_POLICY, !!ticket && !denied)
  const facts = ticket && ticketRef.current === ticket && !query.pending && !query.failure ? query.data : null

  useLayoutEffect(() => { if (readOnly) mutation.interrupt() }, [readOnly, mutation.interrupt])

  /** 呼出瞬間に門禁を閉じ、再描画前に別 ID の DELETE を開始させない。 */
  function canWrite(): boolean {
    return !readOnlyRef.current && !deniedRef.current && phaseRef.current === 'idle'
  }
  /** original は一覧更新や同名再 upload に置き換えない。 */
  function submit(document: ProjectDocumentRecord): boolean {
    if (!canWrite()) return false
    const original = { ...document }
    const accepted = mutation.submit(
      (signal) => deleteProjectDocument(projectId, original.document_id, csrfToken, signal),
      () => {
        phaseRef.current = 'idle'; intentRef.current = null
        setPhase('idle'); setIntent(null); onDeleted()
      },
      (failure) => {
        const unknown = failure.key === 'unknown'
        phaseRef.current = unknown ? 'unknown' : 'idle'
        setPhase(phaseRef.current)
        if (!unknown) { intentRef.current = null; setIntent(null) }
        if (['denied', 'archived'].includes(failure.key)) {
          deniedRef.current = failure; setDenied(failure)
        }
      },
    )
    if (accepted) {
      intentRef.current = original; setIntent(original)
      phaseRef.current = 'sending'; setPhase('sending')
      ticketRef.current = null; setTicket(null)
    }
    return accepted
  }
  /** 明示 GET だけを開始し、サーバーの原書込を再送しない。 */
  function checkOriginal(): void {
    if (phaseRef.current !== 'unknown' || !intentRef.current || deniedRef.current) return
    const next = { original: intentRef.current }
    ticketRef.current = next; setTicket(next)
  }
  /** 読取成功は人工解除の前提に限り、削除成功や自動再送の根拠にはしない。 */
  function release(): void {
    if (!facts || !ticket || ticketRef.current !== ticket || phaseRef.current !== 'unknown' || deniedRef.current) return
    mutation.acknowledge()
    phaseRef.current = 'idle'; intentRef.current = null; ticketRef.current = null
    setPhase('idle'); setIntent(null); setTicket(null)
    onDeleted()
  }
  return { intent, phase, denied, failure: mutation.failure, canWrite, submit, observeFailure,
    checkOriginal, release, facts, checking: !!ticket && query.pending, checkFailure: query.failure }
}
