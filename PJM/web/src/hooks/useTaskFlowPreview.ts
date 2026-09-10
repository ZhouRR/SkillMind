import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { isValidTaskFlowTarget, loadTaskFlowPreview, type TaskFlowPreviewTarget } from '../api'
import { TASK_FLOW_POLICY } from '../lib/taskFlowPreview'
import { isNonNilUuid } from '../lib/validation'
import { useResourceQuery, type SessionEnded } from './useResourceRequest'

/** task 選択は read-only の原対象だけを保持し、入力草稿や実行権限を持たない。 */
export function useTaskFlowPreview({ actorId, sessionKey, projectId, onSessionEnded }: {
  actorId: string; sessionKey: string; projectId: string; onSessionEnded: SessionEnded
}) {
  const owner = useMemo(() => ({ actorId, sessionKey, projectId }), [actorId, sessionKey, projectId])
  const currentOwner = useRef(owner)
  currentOwner.current = owner
  const mounted = useRef(true)
  const [selection, setSelection] = useState<{ owner: typeof owner; target: TaskFlowPreviewTarget; revision: number } | null>(null)
  const sequence = useRef(0)
  const selected = selection?.owner === owner ? selection : null
  const allowed = isNonNilUuid(actorId) && isNonNilUuid(projectId) && sessionKey.length > 0
  const loader = useCallback((signal: AbortSignal) => {
    if (!selected || !allowed) throw new Error('Invalid task flow preview owner')
    return loadTaskFlowPreview(projectId, selected.target, signal)
  }, [projectId, selected, allowed])
  const query = useResourceQuery(JSON.stringify([actorId, sessionKey, projectId, selected?.revision ?? 0]), loader,
    onSessionEnded, TASK_FLOW_POLICY, allowed && selected !== null)
  useLayoutEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  /** 同 tick の A→B→A と旧 callback も、旧応答を現在の会話へ戻せない。 */
  const select = useCallback((target: TaskFlowPreviewTarget) => {
    if (!mounted.current || currentOwner.current !== owner || !allowed || !isValidTaskFlowTarget(target)) return false
    query.refresh()
    const { skill_id, skill_version_id, task_id, task_key, skill_key, version } = target
    setSelection({ owner, target: { skill_id, skill_version_id, task_id, task_key, skill_key, version }, revision: ++sequence.current })
    return true
  }, [owner, allowed, query.refresh])
  /** 取消は表示だけを閉じる。実行や調度を開始・停止する副作用を持たない。 */
  const close = useCallback(() => {
    if (!mounted.current || currentOwner.current !== owner) return
    query.refresh()
    setSelection(null)
  }, [owner, query.refresh])
  /** 現在の対象を明示的に再読取し、古い失敗や data を新しい成功に流用しない。 */
  const refresh = useCallback(() => {
    if (!mounted.current || currentOwner.current !== owner || !allowed || !selected) return
    query.refresh()
  }, [owner, allowed, selected, query.refresh])
  return {
    target: selected?.target ?? null, selectionRevision: selected?.revision ?? 0,
    data: selected && !query.pending && !query.failure ? query.data : null,
    pending: selected !== null && query.pending, failure: selected ? query.failure : null,
    allowed, select, close, refresh,
  }
}
