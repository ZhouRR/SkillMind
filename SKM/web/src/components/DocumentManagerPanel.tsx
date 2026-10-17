import { Select } from './Select'
import { DocumentFolderInput } from './DocumentFolderInput'
import { ApiProblemError, purgeProjectDocument } from '../api'
import { useCallback, useLayoutEffect, useRef, useState } from 'react'

import {
  loadProjectDocuments,
  loadDocumentFolders,
  manageDocuments,
  type ProjectDocumentRecord,
} from '../api'
import { DocumentOrganizeDialog, type DocumentEdit } from './DocumentOrganizeDialog'
import { useMessages } from '../i18n'
import { useDocumentDeletion } from '../hooks/useDocumentDeletion'
import { useDocumentUpload } from '../hooks/useDocumentUpload'
import { useDocumentUploadClosure } from '../hooks/useDocumentUploadClosure'
import { useResourceQuery, type SessionEnded } from '../hooks/useResourceRequest'
import { DOCUMENT_REQUEST_POLICY, documentFailure, type DocumentFailure } from '../lib/documentFeedback'
import { DOCUMENT_PREVIEW_MAX_BYTES as PREVIEW_MAX_BYTES, type DocumentPreviewKind } from '../lib/documentPreview'
import { buildDocumentTree } from '../lib/documentTree'
import { DocumentTree, type DocumentTreeSelection } from './DocumentTree'
import { DocumentPreviewLoader, type DocumentPreviewRequest } from './DocumentPreviewDialog'
import { EmptyState, LoadingSkeleton, useConfirmDialog } from './PageElements'
import { DocumentUploadStatus, DocumentUploadRecovery } from './DocumentUploadStatus'
import { DocumentUploadClosure, DocumentUploadClosureRecovery } from './DocumentUploadClosure'

/** 原 actor/会話/Project ごとに未知意図と非同期処理の owner を分離する。 */
interface DocumentManagerProps {
  projectId: string
  csrfToken: string
  actorId: string
  readOnly: boolean
  onSessionEnded: SessionEnded
}

/** 会話切替で同じ Project に戻っても旧 DELETE の結果を引き継がない。 */
export function DocumentManagerPanel(props: DocumentManagerProps) {
  return <DocumentManagerBody key={`${props.actorId}:${props.csrfToken}:${props.projectId}`} {...props} />
}

/** 現在の文書目录と原削除意図を分けて所有する。 */
function DocumentManagerBody({ projectId, csrfToken, actorId, readOnly, onSessionEnded }: DocumentManagerProps) {
  const messages = useMessages()
  const [preview, setPreview] = useState<DocumentPreviewRequest | null>(null)
  const [revision, setRevision] = useState(0)
  const { confirm, confirmDialog } = useConfirmDialog()
  const previewRequest = useRef<DocumentPreviewRequest | null>(null)
  const previewSequence = useRef(0)
  const mounted = useRef(false)
  const confirmPending = useRef(false)
  const [confirming, setConfirming] = useState(false)
  const [targetFolder, setTargetFolder] = useState('')
  const replacementInput = useRef<HTMLInputElement | null>(null)
  const replacementTarget = useRef<ProjectDocumentRecord | null>(null)
  const [trashed, setTrashed] = useState(false)
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState('name')
  const [edit, setEdit] = useState<DocumentEdit | null>(null)
  const [manageBusy, setManageBusy] = useState(false)
  const managePending = useRef(false)
  const manageController = useRef<AbortController | null>(null)
  const [manageFailure, setManageFailure] = useState<DocumentFailure['key'] | 'conflict' | null>(null)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set())
  const refresh = useCallback(() => setRevision((current) => current + 1), [])
  const deletion = useDocumentDeletion({ projectId, csrfToken, readOnly, onSessionEnded, onDeleted: refresh })
  const { observeFailure } = deletion
  const closureGate = useRef<() => boolean>(() => false)
  const closeClosureLookup = useRef<() => void>(() => undefined)
  const upload = useDocumentUpload({ actorId, projectId, csrfToken, readOnly: readOnly || !!deletion.denied,
    canWrite: () => deletion.canWrite() && !confirmPending.current && !closureGate.current() && !managePending.current,
    canRead: () => deletion.canRead() && !confirmPending.current,
    onDenied: deletion.observeDenial, onPublished: refresh, beforeBatchAction: () => closeClosureLookup.current() })
  const closure = useDocumentUploadClosure({ actorId, projectId, csrfToken,
    readOnly: readOnly || !!deletion.denied || !!upload.denied,
    canRead: () => upload.canRead(), canWrite: () => deletion.canWrite() && !confirmPending.current && !managePending.current && !upload.denied,
    claim: upload.claimClosure, claimRecovery: upload.claimRecoveredClosure,
    release: upload.releaseClosure, accept: upload.acceptClosure, acceptRecovery: upload.acceptRecoveredClosure,
    beforeAction: upload.closeRecovery,
    onDenied: upload.observeDenial })
  closureGate.current = closure.locked
  closeClosureLookup.current = closure.recovery.close
  const loader = useCallback((signal: AbortSignal) => loadProjectDocuments(projectId, signal, trashed), [projectId, trashed])
  const folderLoader = useCallback((signal: AbortSignal) => loadDocumentFolders(projectId, signal), [projectId])
  const folderQuery = useResourceQuery(`${projectId}:${revision}`, folderLoader, () => undefined, DOCUMENT_REQUEST_POLICY, true, observeFailure)
  const list = useResourceQuery(`${projectId}:${revision}:${trashed}`, loader, () => undefined, DOCUMENT_REQUEST_POLICY,
    true, observeFailure)
  const documentsState = list.failure ? { status: 'error' as const, message: messages.documentsPanel.failures[list.failure.key] }
    : list.data ? { status: 'ready' as const, documents: list.data } : { status: 'loading' as const }
  const blocked = manageBusy || readOnly || !!deletion.denied || deletion.phase !== 'idle' || confirming || upload.isLocked() || closure.locked()
  const browsingBlocked = manageBusy || confirming || upload.isLocked() || closure.locked()
  const busyId = deletion.phase === 'sending' ? deletion.intent?.document_id ?? '__blocked__' : blocked ? '__blocked__' : null
  const managementMessage = manageFailure === null ? null : manageFailure === 'conflict'
    ? messages.fileManagement.failure : manageFailure === 'unknown' ? messages.fileManagement.unknown
      : messages.documentsPanel.failures[manageFailure]

  useLayoutEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      manageController.current?.abort()
      previewRequest.current = null
    }
  }, [])

  /** 原対象を保持した単一 request。未知を自動再送せず、現状確認へ戻す。 */
  async function performManagement(body: Parameters<typeof manageDocuments>[1]): Promise<void> {
    if (!mounted.current || blocked || managePending.current || !deletion.canWrite()) return
    managePending.current = true
    setManageBusy(true)
    setManageFailure(null)
    const controller = new AbortController()
    manageController.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 30_000)
    try {
      await manageDocuments(projectId, body, csrfToken, controller.signal)
      if (!mounted.current) return
      setEdit(null)
      setSelectedIds(new Set())
      refresh()
    } catch (error) {
      if (mounted.current) {
        const reason = reportManagementFailure(error)
        // 確定した入力拒否は草稿を残し、その場で訂正できる。未知は再送を促さない。
        if (reason !== 'conflict' && reason !== 'invalid') setEdit(null)
        refresh()
      }
    } finally {
      window.clearTimeout(timeout)
      if (mounted.current) { managePending.current = false; setManageBusy(false) }
    }
  }

  /** 参照拒否と結果未知を区別し、内部の例外本文は表示しない。 */
  function reportManagementFailure(error: unknown): DocumentFailure['key'] | 'conflict' {
    const reason = error instanceof ApiProblemError && error.status === 409 && error.code === 'document_conflict'
      ? 'conflict' : documentFailure(error, true).key
    setManageFailure(reason)
    observeFailure(error)
    return reason
  }

  /** 対象を一度確認し、複数件も同一 transaction で回収箱へ移す。 */
  async function recycle(items: ProjectDocumentRecord[], restore = false): Promise<void> {
    if (blocked || confirmPending.current || !items.length) return
    confirmPending.current = true
    setConfirming(true)
    const confirmed = await confirm({ title: restore ? messages.fileManagement.restore : messages.fileManagement.trashAction,
      message: restore ? messages.fileManagement.restoreDocuments : messages.fileManagement.trashDocuments,
      targets: items.map((d) => ({ id: d.document_id, label: [d.folder, d.name].filter(Boolean).join('/'), detail: d.document_id })),
      targetsLabel: messages.documentsPanel.selectedCount(items.length),
      confirmLabel: restore ? messages.fileManagement.restore : messages.fileManagement.trashAction, destructive: !restore })
    confirmPending.current = false
    if (!mounted.current) return
    setConfirming(false)
    if (confirmed) await performManagement({ action: restore ? 'RESTORE' : 'TRASH', changes: items.map((d) => ({ document_id: d.document_id, expected_folder: d.folder, expected_name: d.name, folder: d.folder, name: d.name })) })
  }

  /** 明示確認した原 ID のみ逐次削除し、一件でも未知/失敗なら停止する。 */
  async function purge(items: ProjectDocumentRecord[]): Promise<void> {
    if (blocked || confirmPending.current || !items.length) return
    confirmPending.current = true; setConfirming(true)
    const confirmed = await confirm({ title: messages.fileManagement.purge, message: messages.fileManagement.purgeDocuments,
      targets: items.map((d) => ({ id: d.document_id, label: [d.folder, d.name].filter(Boolean).join('/'), detail: d.document_id })),
      targetsLabel: messages.documentsPanel.selectedCount(items.length), confirmLabel: messages.fileManagement.purge, destructive: true })
    confirmPending.current = false
    if (!mounted.current) return
    setConfirming(false)
    if (!confirmed || !deletion.canWrite() || managePending.current) return
    managePending.current = true; setManageBusy(true); setManageFailure(null)
    const request = new AbortController(); manageController.current = request
    try {
      for (const document of items) {
        if (!mounted.current || !deletion.canWrite()) break
        const timeout = window.setTimeout(() => request.abort(), 30_000)
        try { await purgeProjectDocument(projectId, document.document_id, csrfToken, request.signal) }
        finally { window.clearTimeout(timeout) }
      }
    } catch (error) { if (mounted.current) reportManagementFailure(error) }
    finally { if (mounted.current) { managePending.current = false; setManageBusy(false); setSelectedIds(new Set()); refresh() } }
  }

  function saveEdit(folder: string, name: string): void {
    if (!edit) return
    if (edit.mode === 'MOVE') void performManagement({ action: 'MOVE', changes: edit.documents.map((d) => ({ document_id: d.document_id, expected_folder: d.folder, expected_name: d.name, folder, name: edit.documents.length === 1 ? name : d.name })) })
    else void performManagement({ action: edit.mode, source: edit.source, target: folder })
  }

  /** HTTP 待機は共通 query に委ね、クリックと同時に前 request の所有権を閉じる。 */
  function handlePreview(
    document: ProjectDocumentRecord,
    kind: DocumentPreviewKind,
  ): void {
    if (!mounted.current || document.size > PREVIEW_MAX_BYTES) return
    const request = { id: ++previewSequence.current, document: { ...document }, kind }
    previewRequest.current = request
    setPreview(request)
  }

  /** React commit 前の遅れた 401 も、新しい会話や書込資格へ影響させない。 */
  function closePreview(): void {
    previewRequest.current = null
    setPreview(null)
  }

  const documents = documentsState.status === 'ready' ? documentsState.documents : []
  const selectedDocuments = documents.filter((item) => selectedIds.has(item.document_id))
  const visible = documents.filter((d) => `${d.folder}/${d.name}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()))
  const visibleFolders = trashed ? [] : (folderQuery.data ?? [])
    .filter((folder) => folder.toLocaleLowerCase().includes(search.toLocaleLowerCase()))
  const tree = buildDocumentTree(visible, visibleFolders, sort)
  const hasVisibleItems = tree.files.length > 0 || tree.folders.length > 0
  const folders = new Set<string>(trashed ? [] : folderQuery.data ?? [])
  for (const item of documents) {
    const segments = item.folder.split('/').filter(Boolean)
    while (segments.length) { folders.add(segments.join('/')); segments.pop() }
  }
  const selection: DocumentTreeSelection = { selectedIds, disabled: blocked,
    toggle: (id, checked) => setSelectedIds((current) => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    }), onFolder: setTargetFolder,
    replace: trashed ? undefined : (document) => {
      if (blocked) return
      replacementTarget.current = document
      replacementInput.current?.click()
    },
    edit: trashed ? undefined : (d) => setEdit({ mode: 'MOVE', documents: [d], folder: d.folder }),
    folderEdit: trashed ? undefined : (path) => setEdit({ mode: 'MOVE_FOLDER', documents: [], source: path, folder: path }),
    folderSelect: (path, checked) => setSelectedIds((current) => {
      const next = new Set(current)
      // 検索結果の subtree だけを変更し、別フォルダーの選択を保つ。
      for (const document of visible) {
        if (document.folder !== path && !document.folder.startsWith(path + '/')) continue
        if (checked) next.add(document.document_id)
        else next.delete(document.document_id)
      }
      return next
    }),
    folderDelete: trashed ? undefined : (path) => { void performManagement({ action: 'DELETE_FOLDER', source: path }) },
    // 確認には現在の検索範囲の原 ID だけを渡し、非表示の文書へ選択を拡大しない。
    folderRecycle: (path) => { void recycle(visible.filter((d) => d.folder === path || d.folder.startsWith(`${path}/`)), trashed) },
    purge: trashed ? (document) => { void purge([document]) } : undefined,
    trashed }
  return (
    <section className="panel documentPanel" aria-label={messages.documentsPanel.panelAria}>
      {/* 画面見出し(项目文档)との二重表示を避け、panel は一覧の性格を示す。 */}
      <div className="panelHeader">
        <h2>{messages.documentsPanel.listTitle}</h2>
        {documentsState.status === 'ready' && <span className="eventCount">{documents.length}</span>}
      </div>
      <div className="formRow documentManagementToolbar">
        <div className="segmentedControl" role="group" aria-label={messages.documentsPanel.listTitle}>
          <button type="button" className="segmentedOption" disabled={browsingBlocked} aria-pressed={!trashed} onClick={() => { setTrashed(false); setSelectedIds(new Set()) }}>{messages.fileManagement.active}</button>
          <button type="button" className="segmentedOption" disabled={browsingBlocked} aria-pressed={trashed} onClick={() => { setTrashed(true); setSelectedIds(new Set()) }}>{messages.fileManagement.trash}</button>
        </div>
        <input aria-label={messages.fileManagement.search} placeholder={messages.fileManagement.search} value={search} onChange={(e) => { setSearch(e.target.value); setSelectedIds(new Set()) }} />
        <label className="documentSort shortField"><span className="documentSortLabel">{messages.fileManagement.sort}</span><Select className="shortControl shortControlNarrow" density="compact" aria-label={messages.fileManagement.sort} value={sort} onValueChange={(nextValue) => setSort(nextValue)}><option value="name">{messages.fileManagement.byName}</option><option value="date">{messages.fileManagement.byDate}</option><option value="size">{messages.fileManagement.bySize}</option></Select></label>
        {!trashed && <button className="secondaryButton" type="button" disabled={blocked} onClick={() => setEdit({ mode: 'CREATE_FOLDER', documents: [], folder: targetFolder ? targetFolder + '/' : '' })}>{messages.fileManagement.newFolder}</button>}
        <button type="button" className="secondaryButton" onClick={refresh} disabled={list.pending}>
          {messages.documentsPanel.refresh}
        </button>
      </div>
      {trashed && <p className="hint">{messages.fileManagement.recycleHint}</p>}
      {folderQuery.failure && <p role="alert" className="error">{messages.fileManagement.failure}</p>}
      {managementMessage && !edit && <p role="alert" className="error">{managementMessage}</p>}
      {!trashed && <div className="documentUpload documentToolbar">
        <DocumentFolderInput value={targetFolder} folders={folders} disabled={blocked} onValueChange={setTargetFolder} />
        <label className="primaryButton fileUploadButton" aria-disabled={blocked}>
          {messages.documentsPanel.chooseFiles}
          <input
            type="file"
            multiple
            disabled={blocked}
            aria-label={messages.documentsPanel.uploadFilesAria}
            onChange={(event) => {
              const selected = event.currentTarget.files ? Array.from(event.currentTarget.files) : []
              event.currentTarget.value = ''
              upload.start(selected, targetFolder, documents)
            }}
          />
        </label>
        <label className="secondaryButton fileUploadButton" aria-disabled={blocked}>
          {messages.documentsPanel.chooseFolder}
          <input
            type="file"
            multiple
            disabled={blocked}
            aria-label={messages.documentsPanel.uploadFolderAria}
            ref={(element) => { element?.setAttribute('webkitdirectory', '') }}
            onChange={(event) => {
              const selected = event.currentTarget.files ? Array.from(event.currentTarget.files) : []
              event.currentTarget.value = ''
              upload.start(selected, targetFolder, documents)
            }}
          />
        </label>
        <input ref={replacementInput} type="file" hidden disabled={blocked} aria-label={messages.documentsPanel.updateFile}
          onChange={(event) => {
            const file = event.currentTarget.files?.[0]
            const target = replacementTarget.current
            event.currentTarget.value = ''
            replacementTarget.current = null
            if (file && target) {
              // 更新先の名前を保ち、ローカル側の名前や directory 選択で対象を変えない。
              const replacement = new File([file], target.name, { type: file.type, lastModified: file.lastModified })
              upload.start([replacement], target.folder, [target])
            }
          }} />
      </div>}
      {documents.length > 0 && <div className="documentSelectionToolbar">
        <label><input type="checkbox" disabled={blocked} checked={visible.length > 0 && selectedDocuments.length === visible.length}
          ref={(element) => { if (element) element.indeterminate = selectedDocuments.length > 0 && selectedDocuments.length < visible.length }}
          onChange={(event) => setSelectedIds(event.target.checked ? new Set(visible.map((item) => item.document_id)) : new Set())} />
          {messages.documentsPanel.selectAll}</label>
        <span role="status">{messages.documentsPanel.selectedCount(selectedDocuments.length)}</span>
        {selectedDocuments.length > 0 && <><button type="button" className="secondaryButton compactButton" disabled={blocked}
          onClick={() => void recycle(selectedDocuments, trashed)}>{trashed ? messages.fileManagement.restore : messages.fileManagement.trashAction}</button>
        {trashed && <button className="secondaryButton compactButton" type="button" disabled={blocked} onClick={() => void purge(selectedDocuments)}>{messages.fileManagement.purge}</button>}
        {!trashed && <button className="secondaryButton compactButton" type="button" disabled={blocked} onClick={() => setEdit({ mode: 'MOVE', documents: selectedDocuments, folder: targetFolder })}>{messages.fileManagement.moveSelected}</button>}
        <button type="button" className="secondaryButton compactButton" disabled={blocked}
          onClick={() => setSelectedIds(new Set())}>{messages.documentsPanel.clearSelection}</button></>}
      </div>}
      <DocumentUploadStatus upload={upload} canRead={deletion.canRead() && !confirming} />
      <DocumentUploadClosure upload={upload} closure={closure} />
      {readOnly && <p className="hint">{messages.documentsPanel.failures.archived}</p>}
      {(deletion.denied || deletion.failure) && <p className="error" role="alert">
        {messages.documentsPanel.failures[(deletion.denied ?? deletion.failure)!.key]}
      </p>}
      {deletion.phase === 'unknown' && deletion.intent && <section className="panel" aria-label={messages.documentsPanel.unknownTitle}>
        <h3>{messages.documentsPanel.unknownTitle}</h3>
        <p>{deletion.intent.name}</p><p className="hint">{deletion.intent.document_id}</p>
        <p>{messages.documentsPanel.factsOnly}</p>
        <button type="button" className="secondaryButton" disabled={deletion.checking || deletion.readDenied}
          onClick={deletion.checkOriginal}>{messages.documentsPanel.checkOriginal}</button>
        {deletion.checking && <p role="status">{messages.documentsPanel.checking}</p>}
        {deletion.checkFailure && <p role="alert">{messages.documentsPanel.failures[deletion.checkFailure.key]}</p>}
        {deletion.facts && <>
          <p role="status">{deletion.facts.status === 'present' ? messages.documentsPanel.present : messages.documentsPanel.absent}</p>
          <button type="button" className="secondaryButton" disabled={deletion.readDenied}
            onClick={deletion.release}>{messages.documentsPanel.release}</button>
        </>}
      </section>}
      {documentsState.status === 'loading' && <LoadingSkeleton label={messages.documentsPanel.loadingDocs} rows={2} />}
      {documentsState.status === 'error' && <p className="error" role="alert">{documentsState.message}</p>}
      {documentsState.status === 'ready' && !hasVisibleItems && (
        <EmptyState text={search ? messages.fileManagement.noMatches
          : trashed ? messages.fileManagement.emptyTrash : messages.documentsPanel.emptyDocs} />
      )}
      {documentsState.status === 'ready' && hasVisibleItems && (
        <DocumentTree
          root={tree}
          expandFolders={search.length > 0}
          projectId={projectId}
          busyId={busyId}
          selection={selection}
          onDelete={(document) => void recycle([document], trashed)}
          onPreview={(document, kind) => void handlePreview(document, kind)}
        />
      )}
      <details className="detailDisclosure documentHelp"><summary>{messages.documentsPanel.uploadHelp}</summary>
        <p className="hint">{messages.documentsPanel.hint}</p>
        <DocumentUploadRecovery upload={upload} canRead={deletion.canRead() && !confirming} />
        <DocumentUploadClosureRecovery closure={closure} />
      </details>
      {preview && <DocumentPreviewLoader key={preview.id} request={preview} projectId={projectId}
        isCurrent={() => mounted.current && previewRequest.current === preview}
        observeFailure={observeFailure} onClose={closePreview} />}
      {edit && <DocumentOrganizeDialog edit={edit} folders={[...folders].sort()} busy={manageBusy}
        error={managementMessage} onClose={() => { setEdit(null); setManageFailure(null) }} onSave={saveEdit} />}
      {confirmDialog}
    </section>
  )
}
