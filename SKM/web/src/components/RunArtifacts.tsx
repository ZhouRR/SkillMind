import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { loadRunArtifactContent, loadRunArtifacts, type RunArtifactRecord, type RunResultDetail } from '../api'
import { RESOURCE_REQUEST_TIMEOUT_MS, useResourceQuery, type SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { ARTIFACT_REQUEST_POLICY, resultArtifactRefs } from '../lib/artifactFeedback'

/** 一つの明示 click に固定した回执。別索引や同名 path で上書きしない。 */
interface DownloadRequest { id: number; artifact: RunArtifactRecord; deadline: number }

/** 実 Workspace の actor/Session/Project/Run ごとに親が再作成する添付区画。 */
export function RunArtifacts({ projectId, runId, result, onSessionExpired }: {
  projectId: string; runId: string; result: RunResultDetail | null; onSessionExpired: SessionEnded
}) {
  const labels = useMessages().runResult.artifacts
  const loader = useCallback((signal: AbortSignal) => loadRunArtifacts(projectId, runId, signal), [projectId, runId])
  const index = useResourceQuery(`${projectId}:${runId}`, loader, onSessionExpired, ARTIFACT_REQUEST_POLICY)
  const [request, setRequest] = useState<DownloadRequest | null>(null)
  const active = useRef<DownloadRequest | null>(null)
  const sequence = useRef(0)
  const mounted = useRef(false)
  useLayoutEffect(() => { mounted.current = true; return () => { mounted.current = false; active.current = null } }, [])
  const refs = resultArtifactRefs(result)
  const records = !index.pending && !index.failure ? index.data ?? [] : []
  const unmatched = refs.filter((ref) => !records.some((record) => record.artifact_ref === ref))

  /** 再描画前の同 tick 重複 click と、別添付への暗黙の中断を防ぐ。 */
  function start(artifact: RunArtifactRecord): void {
    if (!mounted.current || active.current || index.pending || index.failure) return
    const next = { id: ++sequence.current, artifact: { ...artifact }, deadline: performance.now() + RESOURCE_REQUEST_TIMEOUT_MS }
    active.current = next
    setRequest(next)
  }
  /** 取消/閉じる時点で資格を閉じ、passive cleanup を待って旧応答を許可しない。 */
  function close(): void { active.current = null; setRequest(null) }

  return <section className="resultSection runArtifacts" aria-label={labels.title}>
    <h3>{labels.title}</h3><p className="hint">{labels.hint}</p>
    {index.pending ? <p role="status">{labels.loading}</p> : index.failure
      ? <p className="error" role="alert">{labels.failures[index.failure.key]}</p>
      : <>
        {records.length === 0 && <p>{labels.empty}</p>}
        <ul className="artifactList">{records.map((record) => <li key={record.artifact_ref}>
          <div><strong>{record.path}</strong><p>{record.size_bytes} B · {record.mime_type}</p>
            <p className="hint">{refs.includes(record.artifact_ref) ? labels.referenced : labels.unreferenced}</p></div>
          <button className="secondaryButton compactButton" type="button" disabled={request !== null}
            onClick={() => start(record)}>{labels.download}</button>
        </li>)}</ul>
        {unmatched.length > 0 && <div className="artifactUnmatched"><p className="hint">{labels.unavailableRefs}</p>
          <ul>{unmatched.map((ref) => <li key={ref}><code>{ref}</code></li>)}</ul></div>}
      </>}
    <button className="secondaryButton compactButton" type="button" disabled={index.pending || request !== null}
      onClick={index.refresh}>{labels.refresh}</button>
    {request && <ArtifactDownload key={request.id} projectId={projectId} runId={runId} request={request}
      isCurrent={() => mounted.current && active.current === request} onClose={close} onSessionExpired={onSessionExpired} />}
  </section>
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
    <strong>{request.artifact.path}</strong>
    <p className={failure ? 'error' : undefined}>{failure ? labels.failures[failure]
      : status === 'delivered' ? labels.delivered : labels.preparing}</p>
    <button className="secondaryButton compactButton" type="button" onClick={onClose}>
      {failure || status === 'delivered' ? labels.close : labels.cancel}</button>
  </div>
}
