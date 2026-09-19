import { useEffect, useRef, useState } from 'react'
import { changeRunDeletion, previewRunDeletion, type RunDeletionPreview } from '../api'
import { useMessages } from '../i18n'
import { ModalDialog } from './PageElements'

/** actor/Project/Run ごとに親が key を固定する確認 form。 */
export function RunDeletionDialog({ projectId, runId, csrfToken, action, onClose, onChanged }: {
  projectId: string; runId: string; csrfToken: string; action: 'TRASH' | 'RESTORE' | 'PURGE'
  onClose: () => void; onChanged: () => void
}) {
  const restore = action === 'RESTORE'
  const purge = action === 'PURGE'
  const m = useMessages().fileManagement
  const [preview, setPreview] = useState<RunDeletionPreview | null>(null)
  const [outputs, setOutputs] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(false)
  const [cleanupPending, setCleanupPending] = useState(false)
  const [sent, setSent] = useState(false)
  const current = useRef(true)
  const writing = useRef(false)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => {
    current.current = true
    let active = true
    const request = new AbortController()
    const timer = window.setTimeout(() => request.abort(), 30_000)
    void previewRunDeletion(projectId, runId, request.signal).then((p) => { if (!request.signal.aborted) setPreview(p) }).catch(() => { if (active) setError(true) }).finally(() => window.clearTimeout(timer))
    return () => { active = false; current.current = false; request.abort(); controller.current?.abort(); window.clearTimeout(timer) }
  }, [projectId, runId])
  async function submit(): Promise<void> {
    if (writing.current || sent || !preview) return
    writing.current = true; setBusy(true); setError(false); setSent(true)
    const request = new AbortController(); controller.current = request
    const timer = window.setTimeout(() => request.abort(), 30_000)
    try { const result = await changeRunDeletion(projectId, runId, action, outputs, csrfToken, request.signal); if (current.current) { if (result.cleanup_pending > 0) setCleanupPending(true); else onChanged() } }
    catch { if (current.current) setError(true) }
    finally { window.clearTimeout(timer); if (current.current) { setBusy(false); writing.current = false } }
  }
  return <ModalDialog open title={restore ? m.restore : purge ? m.purge : m.trashAction} onClose={() => { if (!busy) onClose() }}>
    <p>{restore ? m.recycleHint : purge ? m.purgeConfirm : m.runConfirm}</p>
    {preview && <><p>{m.outputs}: {preview.output_count} · {m.protected}: {preview.protected_output_count}</p>
      <ul>{preview.outputs.map((d) => <li key={d.document_id}>{d.folder}/{d.name}{d.protected && ` (${m.protected})`}</li>)}</ul>
      {!restore && <label><input type="checkbox" checked={outputs} disabled={busy} onChange={(e) => setOutputs(e.target.checked)} />{purge ? m.purgeOutputs : m.includeOutputs}</label>}</>}
    {cleanupPending && <p role="status">{m.cleanupPending}</p>}
    {error && <p role="alert" className="error">{sent ? `${m.unknown} ${m.runBlocked}` : m.failure}</p>}
    <div className="formRow"><button className="primaryButton" type="button" disabled={!preview || busy || sent} onClick={() => void submit()}>{restore ? m.restore : purge ? m.purge : m.trashAction}</button>
      <button className="secondaryButton" type="button" disabled={busy} onClick={onClose}>{m.cancel}</button></div>
  </ModalDialog>
}
