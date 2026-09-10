import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { closeDocumentUpload, loadDocumentUploadClosure, type DocumentUploadClosureReceipt } from '../api'
import type { DocumentFailure } from '../lib/documentFeedback'
import type { OriginalDocumentUpload } from '../lib/documentUpload'
import { DOCUMENT_UPLOAD_CLOSURE_POLICY, documentUploadClosureFailure, type DocumentUploadClosureFailure } from '../lib/documentUploadClosure'
import { isNonNilUuid } from '../lib/validation'
import { useResourceMutation, useResourceQuery } from './useResourceRequest'

/** actor/session/Project の変更は Panel の key remount で分離する。 */
interface ClosureOptions {
  actorId: string
  projectId: string
  csrfToken: string
  readOnly: boolean
  canRead: () => boolean
  canWrite: () => boolean
  claim: (original: OriginalDocumentUpload) => boolean
  claimRecovery: (original: OriginalDocumentUpload) => boolean
  release: (original: OriginalDocumentUpload) => void
  accept: (original: OriginalDocumentUpload, receipt: DocumentUploadClosureReceipt) => boolean
  acceptRecovery: (original: OriginalDocumentUpload, receipt: DocumentUploadClosureReceipt) => boolean
  beforeAction: () => void
  onDenied: (failure: DocumentFailure) => void
}

/** 未知 POST は原項目を所有したまま、停止 GET でしか確定しない。 */
interface ClosureState {
  original: OriginalDocumentUpload
  kind: 'batch' | 'recovery'
  phase: 'confirming' | 'sending' | 'unknown' | 'closed' | 'refused'
  uncertainWrite: boolean
  receipt: DocumentUploadClosureReceipt | null
  failure: DocumentUploadClosureFailure | null
}

/** 原停止の書込/確認と、batch に触れない手入力の照会を分離する。 */
export function useDocumentUploadClosure(options: ClosureOptions) {
  const controls = useRef(options); controls.current = options
  const mounted = useRef(false)
  const denied = useRef(false)
  const expired = useRef(false)
  const [state, setState] = useState<ClosureState | null>(null)
  const current = useRef(state)
  const [ticket, setTicket] = useState<{ original: OriginalDocumentUpload } | null>(null)
  const activeTicket = useRef(ticket)

  /** callback と同 tick の操作にも現在の停止 intent を見せる。 */
  function update(next: ClosureState | null): void { current.current = next; setState(next) }
  /** 共通 hook が 401 の通常 callback を省く場合も停止結果を取り残さない。 */
  const mutation = useResourceMutation(() => fail({ key: 'sessionExpired' }), DOCUMENT_UPLOAD_CLOSURE_POLICY)
  /** 資格の副作用は現在要求と deadline の判定を通った後だけ呼ぶ。 */
  function observe(failure: DocumentUploadClosureFailure): void {
    if (!mounted.current || !['sessionExpired', 'denied', 'archived'].includes(failure.key)) return
    if (failure.key !== 'archived') {
      denied.current = true
      query.refresh(); activeTicket.current = null; setTicket(null)
    }
    mutation.interrupt()
    if (failure.key === 'sessionExpired') {
      if (expired.current) return
      expired.current = true
    }
    controls.current.onDenied({ key: failure.key as 'sessionExpired' | 'denied' | 'archived' })
  }
  const loader = useCallback((signal: AbortSignal) => {
    if (!ticket || activeTicket.current !== ticket) throw new Error('No original publication closure check')
    return loadDocumentUploadClosure(ticket.original.projectId, ticket.original.uploadKey, signal)
  }, [ticket])
  const query = useResourceQuery(`${options.actorId}:${options.projectId}:${ticket?.original.uploadKey ?? ''}`,
    loader, () => undefined, DOCUMENT_UPLOAD_CLOSURE_POLICY, !!ticket && !denied.current,
    (error) => { if (activeTicket.current === ticket && ticket) observe(documentUploadClosureFailure(error, false)) })

  useLayoutEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; activeTicket.current = null }
  }, [])
  useLayoutEffect(() => {
    if (options.readOnly || !controls.current.canRead()) mutation.interrupt()
    if (!controls.current.canRead() && activeTicket.current) cancelCheck()
  })
  useLayoutEffect(() => {
    if (!ticket || activeTicket.current !== ticket || query.pending) return
    const original = ticket.original
    activeTicket.current = null; setTicket(null)
    if (!readable()) return
    if (query.failure) {
      const observed = current.current
      if (!observed || observed.original !== original) return
      update({ ...observed, phase: observed.uncertainWrite ? 'unknown' : 'refused', failure: query.failure })
      if (!observed.uncertainWrite) controls.current.release(original)
    } else if (query.data) accept(original, query.data)
  }, [ticket, query.pending, query.failure, query.data])

  /** 停止準備は元 unknown のみ認領し、未送信や公開済み項目には適用しない。 */
  function prepare(original: OriginalDocumentUpload): void {
    if (!writable() || locked() || !controls.current.claim(original)) return
    update({ original, kind: 'batch', phase: 'confirming', uncertainWrite: false, receipt: null, failure: null })
  }
  /** 再ログイン後も現在の PENDING 原 key だけを認領し、現在 batch を上書きしない。 */
  function prepareRecovery(original: OriginalDocumentUpload): void {
    if (!writable() || locked() || !controls.current.claimRecovery(original)) return
    update({ original, kind: 'recovery', phase: 'confirming', uncertainWrite: false, receipt: null, failure: null })
  }
  /** POST 前の確認画面だけは副作用なしで閉じられる。 */
  function cancelPreparation(): void {
    if (!mounted.current || current.current?.phase !== 'confirming') return
    controls.current.release(current.current.original); update(null)
  }
  /** 確認された同じ原 key に一回だけ停止を書き込む。 */
  function submit(): void {
    const observed = current.current
    if (!observed || observed.phase !== 'confirming' || !writable()) return
    recovery.close(); controls.current.beforeAction()
    const original = observed.original
    const accepted = mutation.submit((signal) => closeDocumentUpload(original.projectId, original.uploadKey,
      controls.current.csrfToken, signal), (receipt) => accept(original, receipt), fail)
    if (accepted) update({ ...observed, phase: 'sending', uncertainWrite: true, failure: null })
  }
  /** 初回の明確な拒否でのみ子門禁を返し、upload 自体の未知は解除しない。 */
  function fail(failure: DocumentUploadClosureFailure): void {
    const observed = current.current
    if (!mounted.current || !observed || observed.phase !== 'sending') return
    const uncertain = failure.key === 'unknown'
    update({ ...observed, phase: uncertain ? 'unknown' : 'refused', uncertainWrite: uncertain, failure })
    if (!uncertain) controls.current.release(observed.original)
    observe(failure)
  }
  /** 受付記録の採用にも現在の資格と元 item の所有権を再確認する。 */
  function accept(original: OriginalDocumentUpload, receipt: DocumentUploadClosureReceipt): void {
    const observed = current.current
    if (!mounted.current || !observed || observed.original !== original) return
    const adopt = observed.kind === 'batch' ? controls.current.accept : controls.current.acceptRecovery
    if (!readable() || !adopt(original, receipt)) {
      update({ ...observed, phase: 'unknown', uncertainWrite: true, failure: { key: 'unknown' } }); return
    }
    update({ ...observed, phase: 'closed', receipt, failure: null, uncertainWrite: false })
    mutation.acknowledge()
  }
  /** GET だけで元停止を確認する。原 upload POST の再送は一切しない。 */
  function check(original: OriginalDocumentUpload): void {
    if (!readable() || activeTicket.current || ['confirming', 'sending'].includes(current.current?.phase ?? '')) return
    const observed = current.current
    const retained = observed?.phase === 'unknown' && observed.uncertainWrite
    if (retained && observed.original !== original) return
    recovery.close(); controls.current.beforeAction()
    if (!retained && !controls.current.claim(original)) return
    update({ original, kind: retained ? observed.kind : 'batch', phase: 'unknown', uncertainWrite: retained, receipt: null, failure: null })
    const next = { original }; activeTicket.current = next; setTicket(next)
  }
  /** GET の取消も同期 abort とし、未知 POST の門禁は保持する。 */
  function cancelCheck(): void {
    if (!mounted.current) return
    query.refresh(); activeTicket.current = null; setTicket(null)
    const observed = current.current
    if (observed?.phase === 'unknown' && !observed.uncertainWrite) {
      controls.current.release(observed.original)
      update({ ...observed, phase: 'refused', failure: { key: 'loadFailed' } })
    }
  }
  /** 帰档は読取を残すが、現在の資格拒否は全要求を閉じる。 */
  function readable(): boolean { return mounted.current && !denied.current && controls.current.canRead() }
  /** 共有 write gate は upload/delete の同期状態を含む。 */
  function writable(): boolean { return readable() && !controls.current.readOnly && controls.current.canWrite() }
  /** 未知は終了ボタンで捨てず、確認前/送信中も並行 write を禁止する。 */
  function locked(): boolean {
    return ['confirming', 'sending', 'unknown'].includes(current.current?.phase ?? '')
      || current.current?.kind === 'recovery' && current.current.phase === 'closed'
  }
  /** 独立停止の確定後だけ終了でき、未知の原要求を捨てる出口にはしない。 */
  function finishRecovery(): void {
    const observed = current.current
    if (!mounted.current || observed?.kind !== 'recovery' || !['closed', 'refused'].includes(observed.phase)) return
    recovery.close(); update(null)
    mutation.acknowledge()
  }

  const recovery = useDocumentUploadClosureLookup({ actorId: options.actorId, projectId: options.projectId,
    canRead: readable, onFailure: observe })
  return { state, prepare, prepareRecovery, finishRecovery, cancelPreparation, submit, check, cancelCheck, cancel: mutation.interrupt,
    checking: !!ticket, locked, readable, writable, recovery }
}

/** 手入力の停止受付記録は表示だけに使い、batch の accept port へ渡さない。 */
function useDocumentUploadClosureLookup(options: { actorId: string; projectId: string;
  canRead: () => boolean; onFailure: (failure: DocumentUploadClosureFailure) => void }) {
  const controls = useRef(options); controls.current = options
  const mounted = useRef(false)
  const [ticket, setTicket] = useState<{ key: string } | null>(null)
  const active = useRef(ticket)
  const [failure, setFailure] = useState<DocumentUploadClosureFailure | null>(null)
  const loader = useCallback((signal: AbortSignal) => {
    if (!ticket || active.current !== ticket) throw new Error('No read-only closure lookup')
    return loadDocumentUploadClosure(options.projectId, ticket.key, signal)
  }, [ticket, options.projectId])
  const query = useResourceQuery(`${options.actorId}:${options.projectId}:${ticket?.key ?? ''}`, loader,
    () => undefined, DOCUMENT_UPLOAD_CLOSURE_POLICY, !!ticket,
    (error) => { if (ticket && active.current === ticket) controls.current.onFailure(documentUploadClosureFailure(error, false)) })
  useLayoutEffect(() => { mounted.current = true; return () => { mounted.current = false; active.current = null } }, [])
  useLayoutEffect(() => { if (!controls.current.canRead() && active.current) close() })
  /** 換 key/終了の瞬間に旧応答を閉じ、遅い 401 で現在の会話を失効させない。 */
  function close(): void {
    if (!mounted.current) return
    query.refresh(); active.current = null; setTicket(null); setFailure(null)
  }
  /** 同じ key の同期連打は一度だけ読み、別 key は前の GET を取消す。 */
  function lookup(value: string): void {
    if (!mounted.current || !controls.current.canRead()) return
    const key = value.trim().toLowerCase()
    if (!isNonNilUuid(key)) { setFailure({ key: 'invalidKey' }); return }
    if (active.current?.key === key && query.pending) return
    close()
    const next = { key }; active.current = next; setTicket(next)
  }
  return { key: ticket?.key ?? null, lookup, close, pending: !!ticket && query.pending,
    receipt: ticket && !query.pending && !query.failure ? query.data : null, failure: failure ?? (ticket ? query.failure : null) }
}
