import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'

import {
  loadProjectDocuments,
  loadProjectDocumentText,
  projectDocumentContentHref,
  type ProjectDocumentRecord,
} from '../api'
import { useMessages } from '../i18n'
import { useDocumentDeletion } from '../hooks/useDocumentDeletion'
import { useDocumentUpload } from '../hooks/useDocumentUpload'
import { useDocumentUploadClosure } from '../hooks/useDocumentUploadClosure'
import { useResourceQuery, type SessionEnded } from '../hooks/useResourceRequest'
import { DOCUMENT_REQUEST_POLICY } from '../lib/documentFeedback'
import { DOCUMENT_PREVIEW_MAX_BYTES as PREVIEW_MAX_BYTES, documentPreviewHtml } from '../lib/documentPreview'
import { formatByteSize, formatLocalTimestamp } from '../lib/presentation'
import { EmptyState, LoadingSkeleton, ModalDialog, useConfirmDialog } from './PageElements'
import { DocumentUploadStatus } from './DocumentUploadStatus'
import { DocumentUploadClosure } from './DocumentUploadClosure'

/** 画面内 preview の描画種別。拡張子登録で excel 等の viewer を後付けする拡張点。 */
export type DocumentPreviewKind = 'text' | 'html'

/** 拡張子 → preview 種別の登録表。未登録拡張子は preview 対象外(download のみ)。 */
const DOCUMENT_PREVIEWERS: Record<string, DocumentPreviewKind> = {
  txt: 'text',
  md: 'text',
  markdown: 'text',
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
  const refresh = useCallback(() => setRevision((current) => current + 1), [])
  const deletion = useDocumentDeletion({ projectId, csrfToken, readOnly, onSessionEnded, onDeleted: refresh })
  const { observeFailure } = deletion
  const closureGate = useRef<() => boolean>(() => false)
  const closeClosureLookup = useRef<() => void>(() => undefined)
  const upload = useDocumentUpload({ actorId, projectId, csrfToken, readOnly: readOnly || !!deletion.denied,
    canWrite: () => deletion.canWrite() && !confirmPending.current && !closureGate.current(),
    canRead: () => deletion.canRead() && !confirmPending.current,
    onDenied: deletion.observeDenial, onPublished: refresh, beforeBatchAction: () => closeClosureLookup.current() })
  const closure = useDocumentUploadClosure({ actorId, projectId, csrfToken,
    readOnly: readOnly || !!deletion.denied || !!upload.denied,
    canRead: () => upload.canRead(), canWrite: () => deletion.canWrite() && !confirmPending.current && !upload.denied,
    claim: upload.claimClosure, claimRecovery: upload.claimRecoveredClosure,
    release: upload.releaseClosure, accept: upload.acceptClosure, acceptRecovery: upload.acceptRecoveredClosure,
    beforeAction: upload.closeRecovery,
    onDenied: upload.observeDenial })
  closureGate.current = closure.locked
  closeClosureLookup.current = closure.recovery.close
  const loader = useCallback((signal: AbortSignal) => loadProjectDocuments(projectId, signal), [projectId])
  const list = useResourceQuery(`${projectId}:${revision}`, loader, () => undefined, DOCUMENT_REQUEST_POLICY,
    true, observeFailure)
  const documentsState = list.failure ? { status: 'error' as const, message: messages.documentsPanel.failures[list.failure.key] }
    : list.data ? { status: 'ready' as const, documents: list.data } : { status: 'loading' as const }
  const blocked = readOnly || !!deletion.denied || deletion.phase !== 'idle' || confirming || upload.isLocked() || closure.locked()
  const busyId = deletion.phase === 'sending' ? deletion.intent?.document_id ?? '__blocked__' : blocked ? '__blocked__' : null

  useLayoutEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      previewRequest.current = null
    }
  }, [])

  /** 所有確認は backend に委ね、UI では明示確認の上で 1 件を削除する。 */
  async function handleDelete(document: ProjectDocumentRecord): Promise<void> {
    if (!mounted.current || !deletion.canWrite() || confirmPending.current || upload.isLocked() || closure.locked()) return
    closure.recovery.close()
    upload.closeRecovery()
    confirmPending.current = true
    setConfirming(true)
    const confirmed = await confirm({
      title: messages.documentsPanel.remove,
      message: messages.documentsPanel.deleteConfirm(document.name),
      confirmLabel: messages.documentsPanel.remove,
      destructive: true,
    })
    confirmPending.current = false
    if (!mounted.current) return
    setConfirming(false)
    if (confirmed) deletion.submit(document)
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
  return (
    <section className="panel documentPanel" aria-label={messages.documentsPanel.panelAria}>
      {/* 画面見出し(项目文档)との二重表示を避け、panel は一覧の性格を示す。 */}
      <div className="panelHeader">
        <h2>{messages.documentsPanel.listTitle}</h2>
        {documentsState.status === 'ready' && <span className="eventCount">{documents.length}</span>}
      </div>
      <p className="hint">
        {messages.documentsPanel.hint}
      </p>
      <div className="documentUpload">
        <label className="secondaryButton fileUploadButton">
          {messages.documentsPanel.chooseFiles}
          <input
            type="file"
            multiple
            disabled={blocked}
            aria-label={messages.documentsPanel.uploadFilesAria}
            onChange={(event) => {
              const selected = event.currentTarget.files ? Array.from(event.currentTarget.files) : []
              event.currentTarget.value = ''
              upload.start(selected)
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
              upload.start(selected)
            }}
          />
        </label>
      </div>
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
      <button type="button" className="secondaryButton" onClick={refresh} disabled={list.pending}>
        {messages.documentsPanel.refresh}
      </button>
      {documentsState.status === 'loading' && <LoadingSkeleton label={messages.documentsPanel.loadingDocs} rows={2} />}
      {documentsState.status === 'error' && <p className="error" role="alert">{documentsState.message}</p>}
      {documentsState.status === 'ready' && documents.length === 0 && (
        <EmptyState text={messages.documentsPanel.emptyDocs} />
      )}
      {documentsState.status === 'ready' && documents.length > 0 && (
        <DocumentTree
          root={buildDocumentTree(documents)}
          projectId={projectId}
          busyId={busyId}
          onDelete={(document) => void handleDelete(document)}
          onPreview={(document, kind) => void handlePreview(document, kind)}
        />
      )}
      {preview && <DocumentPreviewLoader key={preview.id} request={preview} projectId={projectId}
        isCurrent={() => mounted.current && previewRequest.current === preview}
        observeFailure={observeFailure} onClose={closePreview} />}
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

/** 文書一覧を folder path の実階層で表示する presentational tree。folder 先行・file 後続で安定表示する。 */
export function DocumentTree({ root, projectId, busyId, onDelete, onPreview }: {
  root: DocumentTreeNode
  projectId: string
  busyId: string | null
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
function FolderNode({ folder, depth, projectId, busyId, onDelete, onPreview }: {
  folder: DocumentTreeNode
  depth: number
  projectId: string
  busyId: string | null
  onDelete: (document: ProjectDocumentRecord) => void
  onPreview: (document: ProjectDocumentRecord, kind: DocumentPreviewKind) => void
}) {
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
        {folder.folders.map((child) => (
          <FolderNode
            key={child.path}
            folder={child}
            depth={depth + 1}
            projectId={projectId}
            busyId={busyId}
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
function FileRow({ document, projectId, busyId, onDelete, onPreview }: {
  document: ProjectDocumentRecord
  projectId: string
  busyId: string | null
  onDelete: (document: ProjectDocumentRecord) => void
  onPreview: (document: ProjectDocumentRecord, kind: DocumentPreviewKind) => void
}) {
  const messages = useMessages()
  const kind = documentPreviewKind(document.name)
  const oversized = document.size > PREVIEW_MAX_BYTES
  return (
    <li className="documentItem">
      <div className="documentInfo">
        <strong>{document.name}</strong>
        <span>
          {formatByteSize(document.size)} · {document.mime}
          {' · '}{formatLocalTimestamp(document.created_at)}
        </span>
      </div>
      <div className="documentActions">
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
          {busyId === document.document_id ? messages.documentsPanel.deleting : messages.documentsPanel.remove}
        </button>
      </div>
    </li>
  )
}

/** 文書 preview を共通 modal として描画する。HTML は静的 allowlist と CSP 付き sandbox に限定する。
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
  const html = useMemo(() => preview.status === 'ready' && preview.kind === 'html'
    ? documentPreviewHtml(preview.content) : '', [preview])
  return (
    <ModalDialog
      open
      wide
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
      {preview.status === 'ready' && preview.kind === 'text' && (
        <pre className="previewText">{preview.content}</pre>
      )}
      {preview.status === 'ready' && preview.kind === 'html' && (
        <>
          <p className="hint">{messages.documentsPanel.previewNotice}</p>
          <iframe className="previewFrame" sandbox="" referrerPolicy="no-referrer" srcDoc={html} title={document.name} />
        </>
      )}
    </ModalDialog>
  )
}

/** 文書一覧から folder path の実階層 tree を構築する。folder・file とも名称昇順で安定させる。 */
export function buildDocumentTree(documents: ProjectDocumentRecord[]): DocumentTreeNode {
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
  for (const document of documents) {
    ensureFolder(document.folder).files.push(document)
  }
  const sortNode = (node: DocumentTreeNode): void => {
    node.folders.sort((left, right) => left.name.localeCompare(right.name))
    node.files.sort((left, right) => left.name.localeCompare(right.name))
    node.folders.forEach(sortNode)
  }
  sortNode(root)
  return root
}

/** Folder 配下(子孫含む)の文書件数を数える。 */
export function countDocuments(node: DocumentTreeNode): number {
  return node.files.length + node.folders.reduce((sum, child) => sum + countDocuments(child), 0)
}
