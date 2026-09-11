import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { ApiProblemError, deleteProjectDocument, loadProjectDocument, type ProjectDocumentRecord } from '../api'
import { DOCUMENT_REQUEST_POLICY, documentFailure, type DocumentFailure } from '../lib/documentFeedback'
import { useResourceMutation, useResourceQuery, type SessionEnded } from './useResourceRequest'

/** 現在の一覧の観察であり、原 DELETE や blob 清理の受付記録ではない。 */
type DocumentFacts = { status: 'present'; document: ProjectDocumentRecord } | { status: 'absent' }

/** 帰档は書込だけを閉じ、認証・所属拒否は原 ID の読取も閉じる。 */
function blocksRead(failure: DocumentFailure | null): boolean {
  return failure?.key === 'sessionExpired' || failure?.key === 'denied'
}

/** 呼出元は actor/CSRF/Project の変更で owner を remount する。未知は同 owner 内で保持する。 */
export function useDocumentDeletion({ projectId, csrfToken, readOnly, onSessionEnded, onDeleted }: {
  projectId: string; csrfToken: string; readOnly: boolean; onSessionEnded: SessionEnded; onDeleted: () => void
}) {
  const mounted = useRef(false)
  const expiryNotified = useRef(false)
  const [intent, setIntent] = useState<ProjectDocumentRecord | null>(null)
  const intentRef = useRef<ProjectDocumentRecord | null>(null)
  const settled = useRef<((deleted: boolean) => void) | undefined>(undefined)
  const [phase, setPhase] = useState<'idle' | 'sending' | 'unknown'>('idle')
  const phaseRef = useRef(phase)
  const [denied, setDenied] = useState<DocumentFailure | null>(null)
  const deniedRef = useRef<DocumentFailure | null>(null)
  const readOnlyRef = useRef(readOnly)
  readOnlyRef.current = readOnly
  const [ticket, setTicket] = useState<{ original: ProjectDocumentRecord } | null>(null)
  const ticketRef = useRef(ticket)
  const mutation = useResourceMutation(() => {
    // 当該 DELETE の確定 401 と、別の読取拒否による待機中断を区別する。
    phaseRef.current = 'idle'; intentRef.current = null
    setPhase('idle'); setIntent(null)
    observeDenial({ key: 'sessionExpired' })
    settled.current?.(false)
  }, DOCUMENT_REQUEST_POLICY)

  useLayoutEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; ticketRef.current = null }
  }, [])

  /** 一つの読取の拒否だけでも書込資格を閉じ、別の古い成功で解除しない。 */
  const observeDenial = useCallback((reason: DocumentFailure) => {
    if (!mounted.current || !['sessionExpired', 'denied', 'archived'].includes(reason.key)) return
    // より弱い帰档通知で、先に確認した会話・所属の拒否を戻さない。
    if (deniedRef.current?.key === 'sessionExpired'
      || deniedRef.current?.key === 'denied' && reason.key === 'archived') return
    deniedRef.current = reason
    setDenied(reason)
    ticketRef.current = null; setTicket(null)
    mutation.interrupt()
    if (reason.key === 'sessionExpired' && !expiryNotified.current) {
      expiryNotified.current = true
      onSessionEnded()
    }
  }, [mutation.interrupt, onSessionEnded])
  /** 他の文書操作からの資格拒否も同じ sticky gate へ通知する。 */
  const observeFailure = useCallback((error: unknown) => {
    observeDenial(documentFailure(error, false))
  }, [observeDenial])

  const loader = useCallback(async (signal: AbortSignal): Promise<DocumentFacts> => {
    if (!ticket || ticketRef.current !== ticket || blocksRead(deniedRef.current)) throw new Error('No current document review')
    try {
      return { status: 'present', document: await loadProjectDocument(projectId, ticket.original.document_id, signal) }
    } catch (error: unknown) {
      if (error instanceof ApiProblemError && error.status === 404 && error.code === 'document_not_found') {
        return { status: 'absent' }
      }
      throw error
    }
  }, [projectId, ticket])
  // 資格と失効の通知は共有 query の現在世代・絶対期限の判定後に一度だけ行う。
  const query = useResourceQuery(`${projectId}:${intent?.document_id ?? ''}`, loader,
    () => undefined, DOCUMENT_REQUEST_POLICY, !!ticket && !blocksRead(denied), (error) => {
      if (ticket && ticketRef.current === ticket) observeFailure(error)
    })
  const facts = ticket && ticketRef.current === ticket && !query.pending && !query.failure ? query.data : null

  useLayoutEffect(() => { if (readOnly) mutation.interrupt() }, [readOnly, mutation.interrupt])

  /** 呼出瞬間に門禁を閉じ、再描画前に別 ID の DELETE を開始させない。 */
  function canWrite(): boolean {
    return mounted.current && !readOnlyRef.current && !deniedRef.current && phaseRef.current === 'idle'
  }
  /** 帰档中の upload 受付記録の読取は許可するが、別の未知 DELETE を上書きさせない。 */
  function canRead(): boolean {
    return mounted.current && !blocksRead(deniedRef.current) && phaseRef.current === 'idle'
  }
  /** original は一覧更新や同名再 upload に置き換えない。 */
  function submit(document: ProjectDocumentRecord, onSettled?: (deleted: boolean) => void): boolean {
    if (!canWrite()) return false
    const original = { ...document }
    const accepted = mutation.submit(
      (signal) => deleteProjectDocument(projectId, original.document_id, csrfToken, signal),
      () => {
        phaseRef.current = 'idle'; intentRef.current = null
        setPhase('idle'); setIntent(null); onDeleted()
        onSettled?.(true)
      },
      (failure) => {
        const unknown = failure.key === 'unknown'
        phaseRef.current = unknown ? 'unknown' : 'idle'
        setPhase(phaseRef.current)
        if (!unknown) { intentRef.current = null; setIntent(null) }
        observeDenial(failure)
        onSettled?.(false)
      },
    )
    if (accepted) {
      settled.current = onSettled
      intentRef.current = original; setIntent(original)
      phaseRef.current = 'sending'; setPhase('sending')
      ticketRef.current = null; setTicket(null)
    }
    return accepted
  }
  /** 明示 GET だけを開始し、サーバーの原書込を再送しない。 */
  function checkOriginal(): void {
    if (!mounted.current || phaseRef.current !== 'unknown' || !intentRef.current || blocksRead(deniedRef.current)
      || ticketRef.current && (ticketRef.current !== ticket || query.pending)) return
    const next = { original: intentRef.current }
    ticketRef.current = next; setTicket(next)
  }
  /** 読取成功は人工解除の前提に限り、削除成功や自動再送の根拠にはしない。 */
  function release(): void {
    if (!mounted.current || !facts || !ticket || ticketRef.current !== ticket
      || phaseRef.current !== 'unknown' || blocksRead(deniedRef.current)) return
    mutation.acknowledge()
    phaseRef.current = 'idle'; intentRef.current = null; ticketRef.current = null
    setPhase('idle'); setIntent(null); setTicket(null)
    onDeleted()
  }
  return { intent, phase, denied, readDenied: blocksRead(denied), failure: mutation.failure, canWrite, canRead, submit, observeFailure, observeDenial,
    checkOriginal, release, facts, checking: !!ticket && query.pending, checkFailure: ticket ? query.failure : null }
}
