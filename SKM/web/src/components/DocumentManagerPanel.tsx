import { ApiProblemError, purgeProjectDocument } from '../api'
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'

import {
  loadProjectDocuments,
  loadDocumentFolders,
  manageDocuments,
  loadProjectDocumentText,
  projectDocumentContentHref,
  type ProjectDocumentRecord,
} from '../api'
import { DocumentOrganizeDialog, type DocumentEdit } from './DocumentOrganizeDialog'
import { useMessages } from '../i18n'
import { useDocumentDeletion } from '../hooks/useDocumentDeletion'
import { useDocumentUpload } from '../hooks/useDocumentUpload'
import { useDocumentUploadClosure } from '../hooks/useDocumentUploadClosure'
import { useResourceQuery, type SessionEnded } from '../hooks/useResourceRequest'
import { DOCUMENT_REQUEST_POLICY, documentFailure, type DocumentFailure } from '../lib/documentFeedback'
import { DOCUMENT_PREVIEW_MAX_BYTES as PREVIEW_MAX_BYTES, documentPreviewHtml, documentMarkdownHtml } from '../lib/documentPreview'
import { formatByteSize, formatLocalTimestamp } from '../lib/presentation'
import { formatJsonPreview } from '../lib/jsonPreview'
import { EmptyState, LoadingSkeleton, ModalDialog, useConfirmDialog } from './PageElements'
import { DocumentUploadStatus, DocumentUploadRecovery } from './DocumentUploadStatus'
import { DocumentUploadClosure, DocumentUploadClosureRecovery } from './DocumentUploadClosure'

/** 画面内 preview の描画種別。拡張子登録で excel 等の viewer を後付けする拡張点。 */
export type DocumentPreviewKind = 'text' | 'html' | 'markdown'

/** 拡張子 → preview 種別の登録表。未登録拡張子は preview 対象外(download のみ)。 */
const DOCUMENT_PREVIEWERS: Record<string, DocumentPreviewKind> = {
  txt: 'text',
  json: 'text',
  jsonl: 'text',
  csv: 'text',
  tsv: 'text',
  log: 'text',
  yaml: 'text',
  yml: 'text',
  xml: 'text',
  md: 'markdown',
  markdown: 'markdown',
  html: 'html',
  htm: 'html',
}

/** 画面内 preview を許可する最大 byte 数。超過は download へ誘導する。 */
export { PREVIEW_MAX_BYTES }

/** 文書名から preview 種別を引く。対象外は null。 */
export function documentPreviewKind(name: string): DocumentPreviewKind | null {
  const extension = name.slice(name.lastIndexOf('.') + 1).toLowerCase()
  return DOCUMENT_PREVIEWERS[extension] ?? null
}

/** 文書 preview dialog の非同期状態。 */
export type DocumentPreviewState =
  | { status: 'loading'; document: ProjectDocumentRecord }
  | { status: 'ready'; document: ProjectDocumentRecord; kind: DocumentPreviewKind; content: string }
  | { status: 'error'; document: ProjectDocumentRecord; message: string }

/** 同じ ID の再読取も別 request として所有し、閉じる瞬間に旧応答を無効化する。 */
interface PreviewRequest {
  id: number
  document: ProjectDocumentRecord
  kind: DocumentPreviewKind
}

/** folder path を実際の階層として表示するための文書 tree node。root は name/path とも空文字。 */
export interface DocumentTreeNode {
  name: string
  path: string
  folders: DocumentTreeNode[]
  files: ProjectDocumentRecord[]
}

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
  const [preview, setPreview] = useState<PreviewRequest | null>(null)
  const [revision, setRevision] = useState(0)
  const { confirm, confirmDialog } = useConfirmDialog()
  const previewRequest = useRef<PreviewRequest | null>(null)
  const previewSequence = useRef(0)
  const mounted = useRef(false)
  const confirmPending = useRef(false)
  const [confirming, setConfirming] = useState(false)
  const [targetFolder, setTargetFolder] = useState('')
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
      message: items.map((d) => d.name).join('、'), confirmLabel: restore ? messages.fileManagement.restore : messages.fileManagement.trashAction, destructive: !restore })
    confirmPending.current = false
    if (!mounted.current) return
    setConfirming(false)
    if (confirmed) await performManagement({ action: restore ? 'RESTORE' : 'TRASH', changes: items.map((d) => ({ document_id: d.document_id, expected_folder: d.folder, expected_name: d.name, folder: d.folder, name: d.name })) })
  }

  /** 明示確認した原 ID のみ逐次削除し、一件でも未知/失敗なら停止する。 */
  async function purge(items: ProjectDocumentRecord[]): Promise<void> {
    if (blocked || confirmPending.current || !items.length) return
    confirmPending.current = true; setConfirming(true)
    const confirmed = await confirm({ title: messages.fileManagement.purge, message: messages.fileManagement.purgeDocuments + '\n' + items.map((d) => d.name).join('、'), confirmLabel: messages.fileManagement.purge, destructive: true })
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
  const tree = buildDocumentTree(visible, trashed || search ? [] : folderQuery.data ?? [], sort)
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
    edit: trashed ? undefined : (d) => setEdit({ mode: 'MOVE', documents: [d], folder: d.folder }),
    folderEdit: trashed ? undefined : (path) => setEdit({ mode: 'MOVE_FOLDER', documents: [], source: path, folder: path }),
    folderSelect: (path) => setSelectedIds(new Set(visible.filter((d) => d.folder === path || d.folder.startsWith(path + '/')).map((d) => d.document_id))),
    folderDelete: trashed ? undefined : (path) => { void performManagement({ action: 'DELETE_FOLDER', source: path }) },
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
        <button type="button" className="secondaryButton" disabled={browsingBlocked} aria-pressed={!trashed} onClick={() => { setTrashed(false); setSelectedIds(new Set()) }}>{messages.fileManagement.active}</button>
        <button type="button" className="secondaryButton" disabled={browsingBlocked} aria-pressed={trashed} onClick={() => { setTrashed(true); setSelectedIds(new Set()) }}>{messages.fileManagement.trash}</button>
        <input aria-label={messages.fileManagement.search} placeholder={messages.fileManagement.search} value={search} onChange={(e) => { setSearch(e.target.value); setSelectedIds(new Set()) }} />
        <select aria-label={messages.fileManagement.sort} value={sort} onChange={(e) => setSort(e.target.value)}><option value="name">{messages.fileManagement.byName}</option><option value="date">{messages.fileManagement.byDate}</option><option value="size">{messages.fileManagement.bySize}</option></select>
        {!trashed && <button className="secondaryButton" type="button" disabled={blocked} onClick={() => setEdit({ mode: 'CREATE_FOLDER', documents: [], folder: targetFolder ? targetFolder + '/' : '' })}>{messages.fileManagement.newFolder}</button>}
        <button type="button" className="secondaryButton" onClick={refresh} disabled={list.pending}>
          {messages.documentsPanel.refresh}
        </button>
      </div>
      {trashed && <p className="hint">{messages.fileManagement.recycleHint}</p>}
      {folderQuery.failure && <p role="alert" className="error">{messages.fileManagement.failure}</p>}
      {managementMessage && !edit && <p role="alert" className="error">{managementMessage}</p>}
      {!trashed && <div className="documentUpload documentToolbar">
        <label className="documentTargetFolder">{messages.documentsPanel.targetFolder}
          <input type="text" list="document-upload-folders" value={targetFolder} maxLength={200} disabled={blocked}
            placeholder={messages.documentsPanel.rootFolder} onChange={(event) => setTargetFolder(event.target.value)} />
          <datalist id="document-upload-folders">{[...folders].sort().map((folder) => <option key={folder} value={folder} />)}</datalist>
        </label>
        <label className="primaryButton fileUploadButton">
          {messages.documentsPanel.chooseFiles}
          <input
            type="file"
            multiple
            disabled={blocked}
            aria-label={messages.documentsPanel.uploadFilesAria}
            onChange={(event) => {
              const selected = event.currentTarget.files ? Array.from(event.currentTarget.files) : []
              event.currentTarget.value = ''
              upload.start(selected, targetFolder)
            }}
          />
        </label>
        <label className="secondaryButton fileUploadButton">
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
              upload.start(selected, targetFolder)
            }}
          />
        </label>
      </div>}
      {documents.length > 0 && <div className="documentSelectionToolbar">
        <label><input type="checkbox" disabled={blocked} checked={visible.length > 0 && selectedDocuments.length === visible.length}
          ref={(element) => { if (element) element.indeterminate = selectedDocuments.length > 0 && selectedDocuments.length < visible.length }}
          onChange={(event) => setSelectedIds(event.target.checked ? new Set(visible.map((item) => item.document_id)) : new Set())} />
          {messages.documentsPanel.selectAll}</label>
        <span role="status">{messages.documentsPanel.selectedCount(selectedDocuments.length)}</span>
        <button type="button" className="secondaryButton compactButton" disabled={blocked || selectedDocuments.length === 0}
          onClick={() => void recycle(selectedDocuments, trashed)}>{trashed ? messages.fileManagement.restore : messages.fileManagement.trashAction}</button>
        {trashed && <button className="secondaryButton compactButton" type="button" disabled={blocked || selectedDocuments.length === 0} onClick={() => void purge(selectedDocuments)}>{messages.fileManagement.purge}</button>}
        {!trashed && <button className="secondaryButton compactButton" type="button" disabled={blocked || selectedDocuments.length === 0} onClick={() => setEdit({ mode: 'MOVE', documents: selectedDocuments, folder: targetFolder })}>{messages.fileManagement.moveSelected}</button>}
        <button type="button" className="secondaryButton compactButton" disabled={blocked || selectedDocuments.length === 0}
          onClick={() => setSelectedIds(new Set())}>{messages.documentsPanel.clearSelection}</button>
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
      {documentsState.status === 'ready' && visible.length === 0 && (trashed || search || !folderQuery.data?.length) && (
        <EmptyState text={search ? messages.fileManagement.noMatches
          : trashed ? messages.fileManagement.emptyTrash : messages.documentsPanel.emptyDocs} />
      )}
      {documentsState.status === 'ready' && (visible.length > 0 || (!trashed && (folderQuery.data?.length ?? 0) > 0)) && (
        <DocumentTree
          root={tree}
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

/** 読取の期限・取消・遅延応答を通常 query と共有し、現在 request の拒否だけを伝える。 */
function DocumentPreviewLoader({ request, projectId, isCurrent, observeFailure, onClose }: {
  request: PreviewRequest
  projectId: string
  isCurrent: () => boolean
  observeFailure: (error: unknown) => void
  onClose: () => void
}) {
  const messages = useMessages()
  const current = useRef(isCurrent)
  current.current = isCurrent
  const loader = useCallback(async (signal: AbortSignal) => {
    signal.throwIfAborted()
    if (!current.current()) throw new DOMException('Preview request is no longer current', 'AbortError')
    const content = await loadProjectDocumentText(projectId, request.document.document_id, signal)
    signal.throwIfAborted()
    if (!current.current()) throw new DOMException('Preview request is no longer current', 'AbortError')
    return content
  }, [projectId, request])
  // observeFailure が書込門禁と会話失効を一度に通知するため、query から二重通知しない。
  const query = useResourceQuery(String(request.id), loader, () => undefined, DOCUMENT_REQUEST_POLICY, true,
    (error) => { if (current.current()) observeFailure(error) })
  const preview: DocumentPreviewState = query.failure
    ? { status: 'error', document: request.document, message: messages.documentsPanel.failures[query.failure.key] }
    : query.pending || query.data === null ? { status: 'loading', document: request.document }
      : { status: 'ready', document: request.document, kind: request.kind, content: query.data }
  return <DocumentPreviewDialog preview={preview} projectId={projectId} onClose={onClose} />
}

/** 文書 tree の選択と upload 先を親 owner へ返す操作。 */
interface DocumentTreeSelection {
  selectedIds: ReadonlySet<string>
  disabled: boolean
  toggle: (id: string, checked: boolean) => void
  onFolder: (path: string) => void
  edit?: (document: ProjectDocumentRecord) => void
  folderEdit?: (path: string) => void
  folderDelete?: (path: string) => void
  folderSelect?: (path: string) => void
  purge?: (document: ProjectDocumentRecord) => void
  trashed?: boolean
}

/** 文書一覧を folder path の実階層で表示する presentational tree。folder 先行・file 後続で安定表示する。 */
export function DocumentTree({ root, projectId, busyId, onDelete, onPreview, selection }: {
  root: DocumentTreeNode
  projectId: string
  busyId: string | null
  selection?: DocumentTreeSelection
  onDelete: (document: ProjectDocumentRecord) => void
  onPreview: (document: ProjectDocumentRecord, kind: DocumentPreviewKind) => void
}) {
  return (
    <div className="documentTree">
      {root.folders.map((folder) => (
        <FolderNode
          key={folder.path}
          folder={folder}
          depth={0}
          projectId={projectId}
          busyId={busyId}
          selection={selection}
          onDelete={onDelete}
          onPreview={onPreview}
        />
      ))}
      {root.files.length > 0 && (
        <ul className="documentList">
          {root.files.map((document) => (
            <FileRow
              key={document.document_id}
              document={document}
              projectId={projectId}
              busyId={busyId}
              selection={selection}
              onDelete={onDelete}
              onPreview={onPreview}
            />
          ))}
        </ul>
      )}
    </div>
  )
}

/** 一つの folder を開閉可能な節として描画し、子 folder → file の順で内容を並べる。 */
function FolderNode({ folder, depth, projectId, busyId, onDelete, onPreview, selection }: {
  folder: DocumentTreeNode
  depth: number
  projectId: string
  busyId: string | null
  selection?: DocumentTreeSelection
  onDelete: (document: ProjectDocumentRecord) => void
  onPreview: (document: ProjectDocumentRecord, kind: DocumentPreviewKind) => void
}) {
  const messages = useMessages()
  return (
    <details className="docFolder" open={depth === 0}>
      <summary>
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path d="M1.8 4.2a1 1 0 0 1 1-1h3.4l1.6 1.8h5.4a1 1 0 0 1 1 1v6.2a1 1 0 0 1-1 1H2.8a1 1 0 0 1-1-1Z" />
        </svg>
        <strong>{folder.name}</strong>
        <span className="docFolderCount">{countDocuments(folder)}</span>
      </summary>
      <div className="docFolderBody">
        {selection && <div className="formRow documentFolderToolbar">
          <button className="secondaryButton compactButton" type="button" disabled={selection.disabled} onClick={() => selection.folderSelect?.(folder.path)}>{messages.fileManagement.selectFolder}</button>
          {selection.folderEdit && <button className="secondaryButton compactButton" type="button" disabled={selection.disabled} onClick={() => selection.folderEdit?.(folder.path)}>{messages.fileManagement.rename} / {messages.fileManagement.move}</button>}
          {countDocuments(folder) === 0 && selection.folderDelete && <button className="secondaryButton compactButton" type="button" disabled={selection.disabled} onClick={() => selection.folderDelete?.(folder.path)}>{messages.fileManagement.emptyFolder}</button>}
        </div>}
        {selection && !selection.trashed && <button type="button" className="secondaryButton compactButton" disabled={selection.disabled}
          onClick={() => selection.onFolder(folder.path)}>{messages.documentsPanel.uploadHere}</button>}
        {folder.folders.map((child) => (
          <FolderNode
            key={child.path}
            folder={child}
            depth={depth + 1}
            projectId={projectId}
            busyId={busyId}
            selection={selection}
            onDelete={onDelete}
            onPreview={onPreview}
          />
        ))}
        {folder.files.length > 0 && (
          <ul className="documentList">
            {folder.files.map((document) => (
              <FileRow
                key={document.document_id}
                document={document}
                projectId={projectId}
                busyId={busyId}
                selection={selection}
                onDelete={onDelete}
                onPreview={onPreview}
              />
            ))}
          </ul>
        )}
      </div>
    </details>
  )
}

/** 一つの文書 row。preview は登録拡張子かつ上限内のときだけ有効化する。 */
function FileRow({ document, projectId, busyId, onDelete, onPreview, selection }: {
  document: ProjectDocumentRecord
  projectId: string
  busyId: string | null
  selection?: DocumentTreeSelection
  onDelete: (document: ProjectDocumentRecord) => void
  onPreview: (document: ProjectDocumentRecord, kind: DocumentPreviewKind) => void
}) {
  const messages = useMessages()
  const kind = documentPreviewKind(document.name)
  const oversized = document.size > PREVIEW_MAX_BYTES
  return (
    <li className="documentItem">
      {selection && <input type="checkbox" checked={selection.selectedIds.has(document.document_id)} disabled={selection.disabled}
        aria-label={messages.documentsPanel.selectFile(document.name)}
        onChange={(event) => selection.toggle(document.document_id, event.target.checked)} />}
      <div className="documentInfo">
        <strong title={document.name}>{document.name}</strong>
        <span>
          {formatByteSize(document.size)} · {document.mime}
          {' · '}{formatLocalTimestamp(document.created_at)}
        </span>
      </div>
      <div className="documentActions">
        {selection?.purge && <button className="secondaryButton compactButton" type="button" disabled={selection.disabled} onClick={() => selection.purge?.(document)}>{messages.fileManagement.purge}</button>}
        {selection?.edit && <button className="secondaryButton compactButton" type="button" disabled={selection.disabled} onClick={() => selection.edit?.(document)}>{messages.fileManagement.rename} / {messages.fileManagement.move}</button>}
        {kind !== null && (
          <button
            className="secondaryButton compactButton"
            disabled={oversized}
            title={oversized ? messages.documentsPanel.oversizedTitle : undefined}
            type="button"
            onClick={() => onPreview(document, kind)}
          >
            {messages.documentsPanel.previewButton}
          </button>
        )}
        <a
          className="secondaryButton compactButton"
          download={document.name}
          href={projectDocumentContentHref(projectId, document.document_id)}
        >
          {messages.documentsPanel.download}
        </a>
        <button
          className="secondaryButton compactButton"
          disabled={busyId !== null}
          onClick={() => onDelete(document)}
          type="button"
        >
          {busyId === document.document_id ? messages.documentsPanel.deleting : selection?.trashed ? messages.fileManagement.restore : messages.fileManagement.trashAction}
        </button>
      </div>
    </li>
  )
}

/** 文書 preview を広い共通 modal に描画する。埋め込み CSS/静的 SVG は CSP 付き sandbox に保持する。
 *
 *  親が preview 有無で条件描画するため、ここでの open は常に true。
 *  遮罩・Escape・背面 scroll 停止・焦点復帰は ModalDialog 側の共通実装に委ねる。 */
export function DocumentPreviewDialog({ preview, projectId, onClose }: {
  preview: DocumentPreviewState
  projectId: string
  onClose: () => void
}) {
  const messages = useMessages()
  const { document } = preview
  const text = useMemo(() => preview.status === 'ready'
    ? formatJsonPreview(preview.content, document.name) : '', [preview, document.name])
  const [showSource, setShowSource] = useState(false)
  const html = useMemo(() => preview.status !== 'ready' ? ''
    : preview.kind === 'markdown' ? documentMarkdownHtml(preview.content)
      : preview.kind === 'html' ? documentPreviewHtml(preview.content) : '', [preview])
  return (
    <ModalDialog
      open
      viewport
      title={document.name}
      meta={`${formatByteSize(document.size)} · ${document.mime}`}
      actions={(
        <a
          className="secondaryButton compactButton"
          download={document.name}
          href={projectDocumentContentHref(projectId, document.document_id)}
        >
          {messages.documentsPanel.download}
        </a>
      )}
      onClose={onClose}
    >
      {preview.status === 'loading' && <LoadingSkeleton label={messages.documentsPanel.loadingPreview} rows={3} />}
      {preview.status === 'error' && <p className="error" role="alert">{preview.message}</p>}
      {preview.status === 'ready' && preview.kind === 'markdown' && <button type="button"
        className="secondaryButton compactButton" aria-pressed={showSource} onClick={() => setShowSource((current) => !current)}>
        {showSource ? messages.documentsPanel.previewButton : messages.documentsPanel.viewSource}</button>}
      {preview.status === 'ready' && (preview.kind === 'text' || preview.kind === 'markdown' && showSource) && (
        <pre className="previewText">{text}</pre>
      )}
      {preview.status === 'ready' && (preview.kind === 'html' || preview.kind === 'markdown' && !showSource) && (
        <iframe className="previewFrame" sandbox="" referrerPolicy="no-referrer" srcDoc={html} title={document.name} />
      )}
    </ModalDialog>
  )
}

/** 文書一覧から folder path の実階層 tree を構築する。folder・file とも名称昇順で安定させる。 */
export function buildDocumentTree(documents: ProjectDocumentRecord[], emptyFolders: string[] = [], sort = 'name'): DocumentTreeNode {
  const root: DocumentTreeNode = { name: '', path: '', folders: [], files: [] }
  const nodes = new Map<string, DocumentTreeNode>([['', root]])
  const ensureFolder = (path: string): DocumentTreeNode => {
    const existing = nodes.get(path)
    if (existing) return existing
    const segments = path.split('/')
    const parent = ensureFolder(segments.slice(0, -1).join('/'))
    const node: DocumentTreeNode = { name: segments.at(-1) ?? '', path, folders: [], files: [] }
    parent.folders.push(node)
    nodes.set(path, node)
    return node
  }
  for (const path of emptyFolders) ensureFolder(path)
  for (const document of documents) {
    ensureFolder(document.folder).files.push(document)
  }
  const sortNode = (node: DocumentTreeNode): void => {
    node.folders.sort((left, right) => left.name.localeCompare(right.name))
    node.files.sort((left, right) => (sort === 'size' ? right.size - left.size : sort === 'date' ? Date.parse(right.created_at) - Date.parse(left.created_at) : 0) || left.name.localeCompare(right.name))
    node.folders.forEach(sortNode)
  }
  sortNode(root)
  return root
}

/** Folder 配下(子孫含む)の文書件数を数える。 */
export function countDocuments(node: DocumentTreeNode): number {
  return node.files.length + node.folders.reduce((sum, child) => sum + countDocuments(child), 0)
}
