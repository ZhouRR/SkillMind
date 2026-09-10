import { useEffect, useMemo, useState } from 'react'

import { createTaskRun, type RunRecord } from '../api'
import { createIdempotencyKey } from '../lib/idempotency'
import {
  canStartRunSubmission,
  failedRunSubmission,
  freezeRunSubmission,
  sendingRunSubmission,
  submissionBelongsTo,
  submissionPayload,
  type FrozenRunSubmission,
  type PendingRunSubmission,
  type RunSubmissionScope,
} from '../lib/runSubmission'
import type { TaskDraft } from '../lib/taskDraft'

/** HTTP 応答を待つ上限。Run の実行 timeout や取消とは独立した UI の待機期限。 */
const RESPONSE_TIMEOUT_MS = 30_000

/** actor/Project ごとの memory-only 所有者。古い Promise と二重クリックを同期的に遮断する。 */
interface SubmissionOwner extends RunSubmissionScope {
  active: boolean
  pending: PendingRunSubmission | null
  controller: AbortController | null
  timeout: ReturnType<typeof setTimeout> | null
}

/** 一回の要求再送だけを調整し、確認後の Run lifecycle は呼出し元 Workspace に渡す。 */
export function useRunSubmission({ actorId, projectId, csrfToken, onConfirmed }: RunSubmissionScope & {
  csrfToken: string
  onConfirmed: (request: FrozenRunSubmission, run: RunRecord) => void
}) {
  const owner = useMemo<SubmissionOwner>(() => ({
    actorId, projectId, active: false, pending: null, controller: null, timeout: null,
  }), [actorId, projectId])
  const [view, setView] = useState<{ owner: SubmissionOwner; pending: PendingRunSubmission | null }>({ owner, pending: null })

  useEffect(() => {
    owner.active = true
    return () => {
      owner.active = false
      owner.controller?.abort()
      if (owner.timeout !== null) clearTimeout(owner.timeout)
      owner.pending = null
    }
  }, [owner])

  /** 同じ owner の state だけを公開し、別 actor の入力を一 frame でも描画しない。 */
  function publish(pending: PendingRunSubmission | null): void {
    owner.pending = pending
    if (owner.active) setView({ owner, pending })
  }

  /** 一回の HTTP 呼出し。失敗後も本文/key を残し、新しい key での自動 retry はしない。 */
  async function send(request: FrozenRunSubmission): Promise<void> {
    if (!owner.active || owner.controller !== null || !submissionBelongsTo(request, owner)) return
    const controller = new AbortController()
    owner.controller = controller
    const sending = sendingRunSubmission(request, owner.pending)
    publish(sending)
    const timeout = setTimeout(() => {
      if (!owner.active || owner.controller !== controller) return
      controller.abort()
      owner.controller = null
      owner.timeout = null
      publish(failedRunSubmission(sending, undefined))
    }, RESPONSE_TIMEOUT_MS)
    owner.timeout = timeout
    try {
      const run = await createTaskRun(
        request.projectId, submissionPayload(request), request.idempotencyKey, csrfToken, controller.signal,
      )
      if (!owner.active || owner.controller !== controller) return
      if (controller.signal.aborted || run.project_id !== request.projectId) {
        publish(failedRunSubmission(sending, undefined))
        return
      }
      publish(null)
      onConfirmed(request, run)
    } catch (error: unknown) {
      if (owner.active && owner.controller === controller) publish(failedRunSubmission(sending, error))
    } finally {
      clearTimeout(timeout)
      if (owner.timeout === timeout) owner.timeout = null
      if (owner.controller === controller) owner.controller = null
    }
  }

  /** ユーザーが明示した新規実行だけに新しい key を割り当てる。 */
  function start(draft: TaskDraft, capability: string, acknowledgePrevious: boolean): void {
    if (!owner.active || !canStartRunSubmission(owner.pending, owner, acknowledgePrevious)) return
    const request = freezeRunSubmission(owner, draft, capability, createIdempotencyKey())
    void send(request)
  }

  /** 現在の CSRF で原要求を確認する。編集された草稿や再取得した task catalog は読まない。 */
  function retry(): void {
    const pending = owner.pending
    if (pending === null || pending.phase === 'sending' || pending.phase === 'conflict') return
    void send(pending.request)
  }

  return { pending: view.owner === owner ? view.pending : null, start, retry }
}
