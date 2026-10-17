import { useCallback, useMemo, useRef } from 'react'

import { loadProjectDocumentImage, loadProjectDocumentText, projectDocumentContentHref, type ProjectDocumentRecord } from '../api'
import { useResourceQuery } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { DOCUMENT_REQUEST_POLICY } from '../lib/documentFeedback'
import type { DocumentPreviewKind } from '../lib/documentPreview'
import { formatJsonPreview } from '../lib/jsonPreview'
import { formatByteSize } from '../lib/presentation'
import { HtmlPreview } from './HtmlPreview'
import { ImageDocumentPreview } from './ImageDocumentPreview'
import { MarkdownDocumentPreview } from './MarkdownDocumentPreview'
import { LoadingSkeleton, ModalDialog } from './PageElements'
import { SourcePreview } from './SourcePreview'

/** 文書 preview dialog の非同期状態。 */
export type DocumentPreviewState =
  | { status: 'loading'; document: ProjectDocumentRecord }
  | { status: 'ready'; document: ProjectDocumentRecord; kind: Exclude<DocumentPreviewKind, 'image'>; content: string }
  | { status: 'ready'; document: ProjectDocumentRecord; kind: 'image'; content: Blob }
  | { status: 'error'; document: ProjectDocumentRecord; message: string }

/** 同じ ID の再読取も別 request として所有し、閉じる瞬間に旧応答を無効化する。 */
export interface DocumentPreviewRequest {
  id: number
  document: ProjectDocumentRecord
  kind: DocumentPreviewKind
}

/** 読取の期限・取消・遅延応答を通常 query と共有し、現在 request の拒否だけを伝える。 */
export function DocumentPreviewLoader({ request, projectId, isCurrent, observeFailure, onClose }: {
  request: DocumentPreviewRequest
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
    const content = request.kind === 'image'
      ? { kind: request.kind, content: await loadProjectDocumentImage(projectId, request.document.document_id, signal) }
      : { kind: request.kind, content: await loadProjectDocumentText(projectId, request.document.document_id, signal) }
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
      : { status: 'ready', document: request.document, ...query.data }
  return <DocumentPreviewDialog preview={preview} projectId={projectId} onClose={onClose} />
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
  const text = useMemo(() => preview.status === 'ready' && preview.kind !== 'image'
    ? formatJsonPreview(preview.content, document.name) : '', [preview, document.name])
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
      {preview.status === 'ready' && preview.kind === 'image' && (
        <ImageDocumentPreview blob={preview.content} title={document.name} />
      )}
      {preview.status === 'ready' && preview.kind === 'markdown' && (
        <MarkdownDocumentPreview source={preview.content} title={document.name} />
      )}
      {preview.status === 'ready' && preview.kind === 'text' && (
        <SourcePreview source={text} />
      )}
      {preview.status === 'ready' && preview.kind === 'html' && (
        <HtmlPreview source={preview.content} title={document.name} />
      )}
    </ModalDialog>
  )
}

