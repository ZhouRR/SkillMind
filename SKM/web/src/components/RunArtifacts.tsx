import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { loadRunArtifactContent, loadRunArtifacts, type RunArtifactRecord, type RunResultDetail, type RunDetailRecord } from '../api'
import { RESOURCE_REQUEST_TIMEOUT_MS, useResourceQuery, type SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { ModalDialog } from './PageElements'
import { DOCUMENT_PREVIEW_MAX_BYTES, documentPreviewHtml } from '../lib/documentPreview'
import { artifactTitle } from '../lib/resultPresentation'
import { MarkdownText } from './MarkdownText'
import { ARTIFACT_REQUEST_POLICY, resultArtifactRefs } from '../lib/artifactFeedback'
import { formatJsonPreview } from '../lib/jsonPreview'

/** 一つの明示 click に固定した回执。別索引や同名 path で上書きしない。 */
interface DownloadRequest { mode: 'download' | 'preview'; id: number; artifact: RunArtifactRecord; title: string; deadline: number }

/** 実 Workspace の actor/Session/Project/Run ごとに親が再作成する添付区画。 */
export function RunArtifacts({ projectId, runId, result, onSessionExpired, evidence = [], snapshots = [], requestedPreview }: {
  projectId: string; runId: string; result: RunResultDetail | null; onSessionExpired: SessionEnded
  evidence?: RunDetailRecord['evidence']; snapshots?: RunDetailRecord['document_snapshots']
  requestedPreview?: { ref: string; nonce: number } | null
}) {
  const messages = useMessages().runResult
  const labels = messages.artifacts
  const loader = useCallback((signal: AbortSignal) => loadRunArtifacts(projectId, runId, signal), [projectId, runId])
  const index = useResourceQuery(`${projectId}:${runId}`, loader, onSessionExpired, ARTIFACT_REQUEST_POLICY)
  const [request, setRequest] = useState<DownloadRequest | null>(null)
  const active = useRef<DownloadRequest | null>(null)
  const sequence = useRef(0)
  const mounted = useRef(false)
  const handled = useRef<typeof requestedPreview>(null)
  const [unavailable, setUnavailable] = useState(false)
  const section = useRef<HTMLElement>(null)
  useEffect(() => { if (unavailable) section.current?.scrollIntoView({ block: 'nearest' }) }, [unavailable])
  useLayoutEffect(() => { mounted.current = true; return () => { mounted.current = false; active.current = null } }, [])
  const refs = resultArtifactRefs(result)
  const records = !index.pending && !index.failure ? index.data ?? [] : []
  const unmatched = refs.filter((ref) => !records.some((record) => record.artifact_ref === ref))

  /** 再描画前の同 tick 重複 click と、別添付への暗黙の中断を防ぐ。 */
  function start(artifact: RunArtifactRecord, mode: 'download' | 'preview' = 'download'): void {
    if (!mounted.current || active.current || index.pending || index.failure) return
    const next = { mode, id: ++sequence.current, artifact: { ...artifact }, title: artifactTitle(artifact, result, evidence, snapshots), deadline: performance.now() + RESOURCE_REQUEST_TIMEOUT_MS }
    setUnavailable(false)
    active.current = next
    setRequest(next)
  }
  /** 取消/閉じる時点で資格を閉じ、passive cleanup を待って旧応答を許可しない。 */
  function close(): void { active.current = null; setRequest(null) }

  // 成果物の明示 click だけを索引の完全一致へ解決する。path や似た題名では取得しない。
  useEffect(() => {
    if (!requestedPreview || handled.current === requestedPreview || index.pending || index.failure || active.current) return
    handled.current = requestedPreview
    const match = records.find((record) => record.artifact_ref === requestedPreview.ref)
    if (!match || !/\.(md|markdown|html?|txt|json)$/i.test(match.path) || match.size_bytes > DOCUMENT_PREVIEW_MAX_BYTES) {
      setUnavailable(true)
      return
    }
    start(match, 'preview')
  }, [requestedPreview, index.pending, index.failure, index.data, request])

  return <section ref={section} className="resultSection runArtifacts" aria-label={labels.title}>
    <h3>{labels.title}</h3>
    {unavailable && <p role="status" className="hint">{labels.previewUnavailable}</p>}
    {index.pending ? <p role="status">{labels.loading}</p> : index.failure
      ? <p className="error" role="alert">{labels.failures[index.failure.key]}</p>
      : <>
        {records.length === 0 && <p>{labels.empty}</p>}
        <ul className="artifactList">{records.map((record) => <li key={record.artifact_ref}>
          <div><strong>{artifactTitle(record, result, evidence, snapshots)}</strong><p>{record.size_bytes} B · {record.mime_type}</p>
            <details className="artifactMetadata"><summary>{messages.technicalDetails}</summary><code>{record.path}</code><code>{record.checksum}</code></details>
            <p className="hint">{refs.includes(record.artifact_ref) ? labels.referenced : labels.unreferenced}</p></div>
          <div className="panelHeaderActions">
            {/\.(md|markdown|html?|txt|json)$/i.test(record.path) && record.size_bytes <= DOCUMENT_PREVIEW_MAX_BYTES
              && <button className="secondaryButton compactButton" type="button" disabled={request !== null}
                onClick={() => start(record, 'preview')}>{labels.preview}</button>}
            <button className="secondaryButton compactButton" type="button" disabled={request !== null}
              onClick={() => start(record)}>{labels.download}</button>
          </div>
        </li>)}</ul>
        {unmatched.length > 0 && <div className="artifactUnmatched"><p className="hint">{labels.unavailableRefs}</p>
          <ul>{unmatched.map((ref) => <li key={ref}><code>{ref}</code></li>)}</ul></div>}
      </>}
    <button className="secondaryButton compactButton" type="button" disabled={index.pending || request !== null}
      onClick={index.refresh}>{labels.refresh}</button>
    {request?.mode === 'download' && <ArtifactDownload key={request.id} projectId={projectId} runId={runId} request={request}
      isCurrent={() => mounted.current && active.current === request} onClose={close} onSessionExpired={onSessionExpired} />}
    {request?.mode === 'preview' && <ArtifactPreview key={request.id} projectId={projectId} runId={runId}
      request={request} isCurrent={() => mounted.current && active.current === request}
      onClose={close} onSessionExpired={onSessionExpired} />}
  </section>
}

/** 検証済み公開添付だけを読み、既存の文書 sanitizer と同じ sandbox で表示する。 */
function ArtifactPreview({ projectId, runId, request, isCurrent, onClose, onSessionExpired }: {
  projectId: string; runId: string; request: DownloadRequest; isCurrent: () => boolean;
  onClose: () => void; onSessionExpired: SessionEnded
}) {
  const labels = useMessages().runResult.artifacts
  const current = useRef(isCurrent)
  current.current = isCurrent
  const loader = useCallback(async (signal: AbortSignal) => {
    if (!current.current()) throw new DOMException('Preview is no longer current', 'AbortError')
    const blob = await loadRunArtifactContent(projectId, runId, request.artifact, signal)
    if (blob.size > DOCUMENT_PREVIEW_MAX_BYTES) throw new Error('Preview is too large')
    const text = new TextDecoder('utf-8', { fatal: true }).decode(await blob.arrayBuffer())
    signal.throwIfAborted()
    if (!current.current()) throw new DOMException('Preview is no longer current', 'AbortError')
    return /\.html?$/i.test(request.artifact.path) ? { format: 'html', content: documentPreviewHtml(text) }
      : /\.(md|markdown)$/i.test(request.artifact.path) ? { format: 'markdown', content: text }
        : { format: 'text', content: formatJsonPreview(text, request.artifact.path) }
  }, [projectId, runId, request])
  const query = useResourceQuery(String(request.id), loader,
    () => { if (current.current()) onSessionExpired() }, ARTIFACT_REQUEST_POLICY)
  return <ModalDialog open title={request.title} viewport onClose={onClose}>
    {query.pending ? <p role="status">{labels.preparing}</p>
      : query.failure ? <p className="error" role="alert">{labels.failures[query.failure.key]}</p>
        : query.data?.format === 'html' ? <iframe className="runReportPreview" title={request.title}
          sandbox="" referrerPolicy="no-referrer" srcDoc={query.data.content} />
          : query.data?.format === 'markdown' ? <div className="artifactMarkdownPreview"><MarkdownText text={query.data.content} /></div>
            : <pre className="previewText">{query.data?.content}</pre>}
  </ModalDialog>
}

/** HTTP 完了だけでは保存成功と主張せず、現在 click の byte だけを browser へ渡す。 */
function ArtifactDownload({ projectId, runId, request, isCurrent, onClose, onSessionExpired }: {
  projectId: string; runId: string; request: DownloadRequest; isCurrent: () => boolean; onClose: () => void; onSessionExpired: SessionEnded
}) {
  const labels = useMessages().runResult.artifacts
  const current = useRef(isCurrent)
  current.current = isCurrent
  const loader = useCallback(async (signal: AbortSignal) => {
    signal.throwIfAborted()
    if (!current.current()) throw new DOMException('Download is no longer current', 'AbortError')
    const blob = await loadRunArtifactContent(projectId, runId, request.artifact, signal)
    signal.throwIfAborted()
    if (!current.current()) throw new DOMException('Download is no longer current', 'AbortError')
    return blob
  }, [projectId, runId, request])
  const query = useResourceQuery(String(request.id), loader,
    () => { if (current.current()) onSessionExpired() }, ARTIFACT_REQUEST_POLICY)
  const delivered = useRef(false)
  const objectUrl = useRef<string | null>(null)
  const [status, setStatus] = useState<'waiting' | 'delivered' | 'timeout' | 'failed'>('waiting')
  // URL の寿命も所有する。離頁時は即座に閉じ、timer を待って旧添付を残さない。
  useLayoutEffect(() => () => { if (objectUrl.current) URL.revokeObjectURL(objectUrl.current) }, [])
  useEffect(() => {
    if (query.pending || query.failure || !query.data || delivered.current || !current.current()) return
    delivered.current = true
    if (performance.now() >= request.deadline) { setStatus('timeout'); return }
    let timer: number | undefined
    try {
      const url = URL.createObjectURL(query.data)
      objectUrl.current = url
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = request.artifact.path.split('/').at(-1) ?? 'artifact.txt'
      anchor.hidden = true
      document.body.append(anchor)
      try { if (current.current()) anchor.click() } finally { anchor.remove() }
      setStatus('delivered')
      timer = window.setTimeout(() => { URL.revokeObjectURL(url); if (objectUrl.current === url) objectUrl.current = null }, 1000)
    } catch { setStatus('failed') }
    return () => { window.clearTimeout(timer); if (objectUrl.current) { URL.revokeObjectURL(objectUrl.current); objectUrl.current = null } }
  }, [query.pending, query.failure, query.data, request])
  const failure = query.failure?.key ?? (status === 'timeout' ? 'timeout' : status === 'failed' ? 'loadFailed' : null)
  return <div className="artifactDownload" role="status">
    <strong>{request.title}</strong>
    <p className={failure ? 'error' : undefined}>{failure ? labels.failures[failure]
      : status === 'delivered' ? labels.delivered : labels.preparing}</p>
    <button className="secondaryButton compactButton" type="button" onClick={onClose}>
      {failure || status === 'delivered' ? labels.close : labels.cancel}</button>
  </div>
}
