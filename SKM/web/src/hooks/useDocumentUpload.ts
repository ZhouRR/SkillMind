import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { loadDocumentUpload, uploadProjectDocument, type DocumentUploadRecord, type DocumentUploadClosureReceipt } from '../api'
import { documentUploadFailure, type DocumentFailure, type DocumentUploadFailure } from '../lib/documentFeedback'
import { DOCUMENT_UPLOAD_POLICY, freezeDocumentUpload, uploadBatchLocked, uploadIsUncertain,
  type DocumentUploadBatch, type DocumentUploadItem, type DocumentUploadRecovery, type OriginalDocumentUpload } from '../lib/documentUpload'
import { isNonNilUuid, sameUuid } from '../lib/validation'
import { useResourceMutation, useResourceQuery } from './useResourceRequest'

/** 所有者変更は呼出元の key remount で分離し、同 owner 内の write/read を同期して守る。 */
interface UploadOptions {
  actorId: string
  projectId: string
  csrfToken: string
  readOnly: boolean
  canWrite: () => boolean
  canRead: () => boolean
  onDenied: (failure: DocumentFailure) => void
  onPublished: () => void
  beforeBatchAction?: () => void
}

/** GET の世代は key が同じでも別にし、取消/再読取の瞬間に前の結果を閉じる。 */
interface UploadCheck {
  readonly original: OriginalDocumentUpload
  readonly purpose: 'batch' | 'recovery'
}

/** 原 multipart は一度だけ送信し、未知は人工 GET と明示継続でのみ進める。 */
export function useDocumentUpload(options: UploadOptions) {
  const controls = useRef(options)
  controls.current = options
  const mounted = useRef(false)
  const [batch, setBatch] = useState<DocumentUploadBatch>({ items: [], paused: false })
  const currentBatch = useRef(batch)
  const [denied, setDenied] = useState<DocumentFailure | null>(null)
  const deniedRef = useRef(denied)
  const expiryNotified = useRef(false)
  const [notice, setNotice] = useState<DocumentUploadFailure | null>(null)
  const [ticket, setTicket] = useState<UploadCheck | null>(null)
  const currentTicket = useRef(ticket)
  const [recovery, setRecovery] = useState<DocumentUploadRecovery | null>(null)
  const currentRecovery = useRef(recovery)
  const closureClaim = useRef<OriginalDocumentUpload | null>(null)
  const [closing, setClosing] = useState(false)

  /** 手動照会の表示を更新しても、原 batch の File/結果/継続門禁には触れない。 */
  const updateRecovery = useCallback((next: DocumentUploadRecovery | null) => {
    currentRecovery.current = next
    setRecovery(next)
  }, [])

  /** 帰档は書込拒否に限り、原要求の読取を失効と同一視しない。 */
  function readDenied(): boolean {
    return deniedRef.current?.key === 'sessionExpired' || deniedRef.current?.key === 'denied'
  }

  /** React render より前に次のクリック/遅延 callback から現在 batch を見えるようにする。 */
  const update = useCallback((next: DocumentUploadBatch) => {
    currentBatch.current = next
    setBatch(next)
  }, [])

  /** Server 401 では共通 hook が failure callback を省くため、原 item の結果もここで保つ。 */
  const mutation = useResourceMutation(() => fail({ key: 'sessionExpired' }), DOCUMENT_UPLOAD_POLICY)

  /** 一つの読取の資格拒否も sticky とし、一覧の成功で元に戻さない。 */
  const deny = useCallback((reason: DocumentFailure) => {
    if (reason.key !== 'sessionExpired' && reason.key !== 'denied' && reason.key !== 'archived') return
    const previous = deniedRef.current
    if (previous?.key === 'sessionExpired' && reason.key !== 'sessionExpired'
      || previous?.key === 'denied' && reason.key === 'archived') return
    deniedRef.current = reason
    setDenied(reason)
    if (reason.key !== 'archived') {
      currentTicket.current = null
      setTicket(null)
      const observed = currentRecovery.current
      if (observed?.phase === 'checking') updateRecovery({ ...observed, phase: 'settled', failure: { key: reason.key } })
    }
    mutation.interrupt()
    if (reason.key === 'sessionExpired') {
      if (expiryNotified.current) return
      expiryNotified.current = true
    } else if (previous?.key === reason.key) return
    controls.current.onDenied(reason)
  }, [mutation.interrupt, updateRecovery])

  const loader = useCallback(async (signal: AbortSignal): Promise<DocumentUploadRecord> => {
    if (!ticket || currentTicket.current !== ticket || readDenied()) throw new Error('No current upload check')
    const record = await loadDocumentUpload(ticket.original.projectId, ticket.original.uploadKey, signal)
    if (record.state === 'PUBLISHED' && (!sameUuid(record.document.uploaded_by, ticket.original.actorId)
      || ticket.original.body && record.document.size !== ticket.original.body.file.size)) {
      throw new Error('Upload receipt does not match the original actor or file')
    }
    return record
  }, [ticket])
  const query = useResourceQuery(`${options.actorId}:${options.projectId}:${ticket?.original.uploadKey ?? ''}`,
    loader, () => undefined, DOCUMENT_UPLOAD_POLICY, !!ticket && !readDenied(), (error) => {
      if (!ticket || currentTicket.current !== ticket) return
      const failure = documentUploadFailure(error, false)
      if (failure.key === 'sessionExpired' || failure.key === 'denied' || failure.key === 'archived') deny({ key: failure.key })
    })

  useLayoutEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; currentTicket.current = null }
  }, [])
  useLayoutEffect(() => {
    if (options.readOnly) mutation.interrupt()
  }, [options.readOnly, mutation.interrupt])

  useLayoutEffect(() => {
    if (!ticket || currentTicket.current !== ticket || query.pending || ticket.purpose !== 'recovery') return
    if (!controls.current.canRead() || readDenied()) { cancelCheck(); return }
    updateRecovery({ original: ticket.original, phase: 'settled', record: query.failure ? null : query.data,
      failure: query.failure })
    currentTicket.current = null
    setTicket(null)
  }, [ticket, query.pending, query.failure, query.data, updateRecovery])

  useLayoutEffect(() => {
    if (!ticket || currentTicket.current !== ticket || ticket.purpose !== 'batch' || query.pending || query.failure
      || query.data?.state !== 'PUBLISHED' || closureClaim.current || readDenied() || !controls.current.canRead()) return
    const record = query.data
    update({ paused: true, items: currentBatch.current.items.map((item) => item.original === ticket.original
      ? { ...item, phase: 'published', document: record.document, failure: null } : item) })
    currentTicket.current = null
    setTicket(null)
    controls.current.onPublished()
  }, [ticket, query.pending, query.failure, query.data, update])

  /** batch 全体の同期 gate。確定後の pause も明示継続までは新規選択を禁止する。 */
  function canStart(): boolean {
    return mounted.current && !deniedRef.current && !controls.current.readOnly
      && !closureClaim.current && controls.current.canWrite() && !uploadBatchLocked(currentBatch.current)
  }

  /** 元 item だけを更新し、一覧/同名情報を原結果の証拠に混ぜない。 */
  function settle(original: OriginalDocumentUpload, changes: Partial<DocumentUploadItem>, paused: boolean): void {
    update({ paused, items: currentBatch.current.items.map((item) => item.original === original ? { ...item, ...changes } : item) })
  }

  /** 既知拒否だけは次へ進める。未知や資格拒否は後続 file を未送信のまま残す。 */
  function fail(failure: DocumentUploadFailure): void {
    const item = currentBatch.current.items.find((entry) => entry.phase === 'sending')
    if (!item || !mounted.current) return
    const qualification = failure.key === 'sessionExpired' || failure.key === 'denied' || failure.key === 'archived'
    const unknown = uploadIsUncertain(failure)
    settle(item.original, { phase: unknown ? 'unknown' : 'refused', failure }, unknown || qualification)
    if (qualification) { deny({ key: failure.key as 'sessionExpired' | 'denied' | 'archived' }); return }
    if (!unknown) sendNext()
  }

  /** 共通 mutation の callback 後にのみ次の file を送り、timeout の独自実装を作らない。 */
  function sendNext(): void {
    if (!mounted.current || deniedRef.current || currentBatch.current.paused) return
    const item = currentBatch.current.items.find((entry) => entry.phase === 'queued')
    if (!item) return
    if (controls.current.readOnly || !controls.current.canWrite()) {
      update({ ...currentBatch.current, paused: true }); return
    }
    const original = item.original
    if (!original.body) throw new Error('Recovered uploads cannot be sent')
    const body = original.body
    const accepted = mutation.submit(
      (signal) => uploadProjectDocument(original.projectId, original.uploadKey, body, controls.current.csrfToken, signal),
      (document) => {
        if (deniedRef.current || controls.current.readOnly || !controls.current.canWrite()) {
          settle(original, { phase: 'unknown', failure: { key: 'uploadUnknown' } }, true); return
        }
        if (!sameUuid(document.uploaded_by, original.actorId)) { fail({ key: 'uploadUnknown' }); return }
        settle(original, { phase: 'published', document, failure: null }, false)
        controls.current.onPublished()
        sendNext()
      }, fail,
    )
    if (accepted) settle(original, { phase: 'sending' }, false)
    else update({ ...currentBatch.current, paused: true })
  }

  /** 選択時に全原 request を固定し、二度目の change が同 tick でも batch を上書きしない。 */
  function start(files: File[], targetFolder = ''): boolean {
    if (!files.length || !canStart()) return false
    let items: DocumentUploadItem[]
    try {
      items = files.map((file) => ({ original: freezeDocumentUpload(options.actorId, options.projectId, file, targetFolder),
        phase: 'queued', document: null, failure: null }))
    } catch {
      setNotice({ key: 'uploadPreparationFailed' }); return false
    }
    setNotice(null)
    controls.current.beforeBatchAction?.()
    closeRecovery()
    update({ items, paused: false })
    sendNext()
    return true
  }

  /** 未知の原 key を再読取するだけで POST は行わず、旧 GET は同期して無効にする。 */
  function checkOriginal(): void {
    if (!mounted.current || closureClaim.current || readDenied() || !controls.current.canRead()
      || currentTicket.current && (currentTicket.current !== ticket || query.pending)) return
    const item = currentBatch.current.items.find((entry) => entry.phase === 'unknown')
    if (!item) return
    controls.current.beforeBatchAction?.()
    const next: UploadCheck = { original: item.original, purpose: 'batch' }
    currentTicket.current = next; setTicket(next)
  }

  /** 手入力は独立した照会で、原 batch が未知でもその門禁を変更しない。 */
  function recover(key: string): void {
    if (!canRecover()) return
    const uploadKey = key.trim().toLowerCase()
    if (!isNonNilUuid(uploadKey)) { setNotice({ key: 'uploadInvalidKey' }); return }
    if (currentTicket.current?.purpose === 'recovery' && currentTicket.current.original.uploadKey === uploadKey) return
    if (currentTicket.current?.purpose === 'batch') cancelCheck()
    closeRecovery()
    const original = Object.freeze({ actorId: controls.current.actorId, projectId: controls.current.projectId,
      uploadKey, body: null, label: uploadKey })
    const next: UploadCheck = { original, purpose: 'recovery' }
    currentTicket.current = next; setTicket(next)
    updateRecovery({ original, phase: 'checking', record: null, failure: null })
    setNotice(null)
  }

  /** 原 batch の照合と同時には読まないが、手入力の再照会は次の key で置換できる。 */
  function canRecover(): boolean {
    return mounted.current && !readDenied() && controls.current.canRead()
      && (currentTicket.current?.purpose !== 'batch' || currentTicket.current === ticket && !query.pending)
  }

  /** 手動照会だけを終了する。未知 POST の acknowledge や File の破棄は行わない。 */
  function closeRecovery(): void {
    if (currentTicket.current?.purpose === 'recovery') {
      query.refresh()
      currentTicket.current = null; setTicket(null)
    }
    updateRecovery(null)
    setNotice(null)
  }

  /** GET の取消は原 POST の結果を一切変えず、遅れて来た成功/401 も捨てる。 */
  function cancelCheck(): void {
    query.refresh()
    if (currentTicket.current?.purpose === 'recovery') {
      const observed = currentRecovery.current
      if (observed) updateRecovery({ ...observed, phase: 'cancelled', record: null, failure: null })
    }
    currentTicket.current = null; setTicket(null)
  }

  /** 確認済み batch の人による継続。残りが無ければ読み取り専用でも次の照合を許可する。 */
  function canContinue(): boolean {
    const current = currentBatch.current
    return mounted.current && current.paused && !closureClaim.current && !readDenied() && !currentTicket.current
      && !current.items.some((item) => item.phase === 'unknown' || item.phase === 'sending')
      && (current.items.some((item) => item.phase === 'queued')
        ? !deniedRef.current && !controls.current.readOnly && controls.current.canWrite() : controls.current.canRead())
  }
  /** 一覧 refresh では呼ばず、原 receipt の確認後に人が要求した場合だけ進める。 */
  function continueBatch(): void {
    if (!canContinue()) return
    controls.current.beforeBatchAction?.()
    mutation.acknowledge()
    update({ ...currentBatch.current, paused: false })
    sendNext()
  }
  /** 停止の照合中に upload GET が並行して原項目を置換しないよう同期して所有する。 */
  function claimClosure(original: OriginalDocumentUpload): boolean {
    if (!mounted.current || readDenied() || !controls.current.canRead() || closureClaim.current
      || !currentBatch.current.items.some((item) => item.original === original && item.phase === 'unknown')) return false
    controls.current.beforeBatchAction?.()
    cancelCheck()
    closureClaim.current = original; setClosing(true)
    return true
  }
  /** 現在の PENDING 読取だけを停止候補にでき、File や batch を補造しない。 */
  function claimRecoveredClosure(original: OriginalDocumentUpload): boolean {
    const observed = currentRecovery.current
    if (!canStart() || !observed || observed.original !== original || observed.phase !== 'settled'
      || observed.record?.state !== 'PENDING' || observed.failure || original.body !== null) return false
    controls.current.beforeBatchAction?.()
    closeRecovery()
    closureClaim.current = original; setClosing(true)
    return true
  }
  /** 初回の確定拒否だけが停止の子門禁を返す。元 upload の未知はそのまま残す。 */
  function releaseClosure(original: OriginalDocumentUpload): void {
    if (!mounted.current || closureClaim.current !== original) return
    closureClaim.current = null; setClosing(false)
  }
  /** 原所有者と key が一致する公開停止だけを確定し、後続 File は人工継続まで送らない。 */
  function acceptClosure(original: OriginalDocumentUpload, receipt: DocumentUploadClosureReceipt): boolean {
    if (!mounted.current || closureClaim.current !== original || readDenied() || !controls.current.canRead()
      || !sameUuid(receipt.project_id, original.projectId) || !sameUuid(receipt.upload_key, original.uploadKey)
      || !currentBatch.current.items.some((item) => item.original === original && item.phase === 'unknown')) return false
    settle(original, { phase: 'closed', failure: null, document: null }, true)
    releaseClosure(original)
    return true
  }
  /** 独立停止の確認は元 batch に公開/停止の項目を追加せず、子門禁だけを返す。 */
  function acceptRecoveredClosure(original: OriginalDocumentUpload, receipt: DocumentUploadClosureReceipt): boolean {
    if (!mounted.current || closureClaim.current !== original || original.body !== null || readDenied()
      || !controls.current.canRead() || !sameUuid(receipt.project_id, original.projectId)
      || !sameUuid(receipt.upload_key, original.uploadKey)) return false
    releaseClosure(original)
    return true
  }
  return { batch, denied, readDenied: readDenied(), notice, start, canStart, recover, recovery, closeRecovery, canRecover, checkOriginal, cancelCheck, continueBatch,
    closing, claimClosure, claimRecoveredClosure, releaseClosure, acceptClosure, acceptRecoveredClosure, observeDenial: deny,
    canRead: () => mounted.current && !readDenied() && controls.current.canRead(),
    canContinue, cancelUpload: mutation.interrupt, isLocked: () => !!closureClaim.current || uploadBatchLocked(currentBatch.current),
    checking: !!ticket && query.pending, checkFailure: ticket?.purpose === 'batch' ? query.failure : null,
    pendingReceipt: ticket?.purpose === 'batch' && !query.pending && !query.failure && query.data?.state === 'PENDING' }
}
