import { useEffect, useRef, useState } from 'react'

import {
  deleteProjectDocument,
  loadProjectDocuments,
  loadProjectDocumentText,
  projectDocumentContentHref,
  uploadProjectDocument,
  type ProjectDocumentRecord,
} from '../api'
import { useMessages } from '../i18n'
import { formatByteSize, formatLocalTimestamp } from '../lib/presentation'
import { EmptyState, LoadingSkeleton, ModalDialog, useConfirmDialog } from './PageElements'

/** 文書一覧取得の非同期状態。 */
type DocumentsState =
  | { status: 'loading' }
  | { status: 'ready'; documents: ProjectDocumentRecord[] }
  | { status: 'error'; message: string }

/** 多 file/目録 upload の非同期状態。 */
type UploadState =
  | { status: 'idle' }
  | { status: 'uploading'; done: number; total: number }
  | { status: 'error'; message: string }

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
export const PREVIEW_MAX_BYTES = 1_000_000

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

/** folder path を実際の階層として表示するための文書 tree node。root は name/path とも空文字。 */
export interface DocumentTreeNode {
  name: string
  path: string
  folders: DocumentTreeNode[]
  files: ProjectDocumentRecord[]
}

/** Project 作用域の文書を階層 tree で一覧・preview・upload・download・削除する自蔵 panel。 */
export function DocumentManagerPanel({ projectId, csrfToken }: {
  projectId: string
  csrfToken: string
}) {
  const messages = useMessages()
  const [documentsState, setDocumentsState] = useState<DocumentsState>({ status: 'loading' })
  const [uploadState, setUploadState] = useState<UploadState>({ status: 'idle' })
  const [preview, setPreview] = useState<DocumentPreviewState | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const { confirm, confirmDialog } = useConfirmDialog()
  const loadController = useRef<AbortController | null>(null)
  const uploadController = useRef<AbortController | null>(null)
  const deleteController = useRef<AbortController | null>(null)
  const previewController = useRef<AbortController | null>(null)

  useEffect(() => () => {
    loadController.current?.abort()
    uploadController.current?.abort()
    deleteController.current?.abort()
    previewController.current?.abort()
  }, [])

  // projectId は sidebar/项目管理の検証済み選択に限られるため、未選択（空）だけを弾けばよい。
  useEffect(() => {
    loadController.current?.abort()
    if (!projectId) {
      setDocumentsState({ status: 'error', message: messages.documentsPanel.selectProjectFirst })
      return
    }
    const controller = new AbortController()
    loadController.current = controller
    // 再取得中も直前の一覧を保ち、upload/削除のたびに空表示へ跳ねることを防ぐ。
    setDocumentsState((current) => current.status === 'ready' ? current : { status: 'loading' })
    void loadProjectDocuments(projectId, controller.signal)
      .then((documents) => setDocumentsState({ status: 'ready', documents }))
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) {
          setDocumentsState({
            status: 'error',
            message: caught instanceof Error ? caught.message : 'Unknown document API error',
          })
        }
      })
    return () => controller.abort()
  }, [projectId, revision])

  /** 選択した file/目録を 1 件ずつ multipart upload し、失敗した file だけを報告する。 */
  async function handleUpload(files: File[]): Promise<void> {
    if (files.length === 0) return
    uploadController.current?.abort()
    const controller = new AbortController()
    uploadController.current = controller
    setActionError(null)
    setUploadState({ status: 'uploading', done: 0, total: files.length })
    const failures: string[] = []
    for (const [index, file] of files.entries()) {
      try {
        await uploadProjectDocument(projectId, file, csrfToken, controller.signal)
      } catch (caught: unknown) {
        if (controller.signal.aborted) return
        const label = file.webkitRelativePath || file.name
        failures.push(`${label}: ${caught instanceof Error ? caught.message : messages.documentsPanel.uploadFailed}`)
      }
      setUploadState({ status: 'uploading', done: index + 1, total: files.length })
    }
    if (controller.signal.aborted) return
    setUploadState(
      failures.length === 0 ? { status: 'idle' } : { status: 'error', message: failures.join(messages.documentsPanel.failureJoin) },
    )
    // 一部成功でも一覧を確定 state から取り直す。
    setRevision((current) => current + 1)
  }

  /** 所有確認は backend に委ね、UI では明示確認の上で 1 件を削除する。 */
  async function handleDelete(document: ProjectDocumentRecord): Promise<void> {
    if (!await confirm({
      title: messages.documentsPanel.remove,
      message: messages.documentsPanel.deleteConfirm(document.name),
      confirmLabel: messages.documentsPanel.remove,
      destructive: true,
    })) return
    deleteController.current?.abort()
    const controller = new AbortController()
    deleteController.current = controller
    setBusyId(document.document_id)
    setActionError(null)
    try {
      await deleteProjectDocument(projectId, document.document_id, csrfToken, controller.signal)
      setRevision((current) => current + 1)
    } catch (caught: unknown) {
      if (!controller.signal.aborted) {
        setActionError(caught instanceof Error ? caught.message : 'Unknown delete API error')
      }
    } finally {
      if (!controller.signal.aborted) setBusyId(null)
    }
  }

  /** 対象文書の content を取得し、種別に応じた dialog preview を開く。 */
  async function handlePreview(
    document: ProjectDocumentRecord,
    kind: DocumentPreviewKind,
  ): Promise<void> {
    previewController.current?.abort()
    const controller = new AbortController()
    previewController.current = controller
    setPreview({ status: 'loading', document })
    try {
      const content = await loadProjectDocumentText(projectId, document.document_id, controller.signal)
      setPreview({ status: 'ready', document, kind, content })
    } catch (caught: unknown) {
      if (!controller.signal.aborted) {
        setPreview({
          status: 'error',
          document,
          message: caught instanceof Error ? caught.message : 'Unknown preview error',
        })
      }
    }
  }

  function closePreview(): void {
    previewController.current?.abort()
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
            aria-label={messages.documentsPanel.uploadFilesAria}
            onChange={(event) => {
              const selected = event.currentTarget.files ? Array.from(event.currentTarget.files) : []
              event.currentTarget.value = ''
              void handleUpload(selected)
            }}
          />
        </label>
        <label className="secondaryButton fileUploadButton">
          {messages.documentsPanel.chooseFolder}
          <input
            type="file"
            multiple
            aria-label={messages.documentsPanel.uploadFolderAria}
            ref={(element) => { element?.setAttribute('webkitdirectory', '') }}
            onChange={(event) => {
              const selected = event.currentTarget.files ? Array.from(event.currentTarget.files) : []
              event.currentTarget.value = ''
              void handleUpload(selected)
            }}
          />
        </label>
      </div>
      {uploadState.status === 'uploading' && (
        <p className="hint" role="status">{messages.documentsPanel.uploading(uploadState.done, uploadState.total)}</p>
      )}
      {uploadState.status === 'error' && <p className="error" role="alert">{uploadState.message}</p>}
      {actionError && <p className="error" role="alert">{actionError}</p>}
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
      {preview && <DocumentPreviewDialog preview={preview} projectId={projectId} onClose={closePreview} />}
      {confirmDialog}
    </section>
  )
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
          disabled={busyId === document.document_id}
          onClick={() => onDelete(document)}
          type="button"
        >
          {busyId === document.document_id ? messages.documentsPanel.deleting : messages.documentsPanel.remove}
        </button>
      </div>
    </li>
  )
}

/** 文書 preview を共通 modal として描画する。text は原文、html は script 無効の sandbox iframe。
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
        // sandbox 属性を空集合にして script/同源権限を全面禁止した表示専用 frame。
        <iframe className="previewFrame" sandbox="" srcDoc={preview.content} title={document.name} />
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
