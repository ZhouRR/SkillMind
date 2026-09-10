import { useCallback, useEffect, useRef, useState } from 'react'

import { loadProjectTasks, loadSchedule, loadScheduleActivity, loadSchedulePage, type ScheduleStatus } from '../api'
import { classifyScheduleReadFailure, exactScheduleTask, schedulePageOffset, scheduleTaskEditability, SCHEDULE_READ_POLICY, type ScheduleReadFailure } from '../lib/scheduleManager'
import { useResourceQuery, type SessionEnded } from './useResourceRequest'

/** 一覧の絞込は URL や永続 storage に入力を漏らさず、同値へ戻っても読取世代を更新する。 */
interface ScheduleFilter { q: string; status: ScheduleStatus | ''; offset: number; revision: number }

/** 一覧・task catalog・精確詳細・在途を分離し、片方の失敗で他方の履歴を消さない。 */
export function useSchedules(projectId: string, onSessionEnded: SessionEnded, enabled = true) {
  const [filter, setFilter] = useState<ScheduleFilter>({ q: '', status: '', offset: 0, revision: 0 })
  const [selection, setSelection] = useState({ id: '', revision: 0 })
  const [readDenied, setReadDenied] = useState<ScheduleReadFailure | null>(null)
  const limit = 25
  const writableFacts = useRef(false)
  const denied = useRef<{ failure: ScheduleReadFailure | null; generation: number }>({ failure: null, generation: 0 })
  const revalidation = useRef<{ generation: number } | null>(null)
  /** 同じ Project の四つの読取は、拒否だけを共有し古い成功で権限を復活させない。 */
  const rejectAccess = useCallback((error: unknown, signal: AbortSignal) => {
    const failure = classifyScheduleReadFailure(error)
    if (signal.aborted || failure.key === 'loadFailed') return
    const current = denied.current.failure?.key === 'sessionExpired' ? denied.current.failure : failure
    denied.current = { failure: current, generation: denied.current.generation + 1 }
    writableFacts.current = false
    setReadDenied(current)
  }, [])
  const listLoader = useCallback(async (signal: AbortSignal) => {
    try {
      return await loadSchedulePage(projectId, {
        q: filter.q || undefined, status: filter.status || undefined, limit, offset: filter.offset,
      }, signal)
    } catch (error) {
      rejectAccess(error, signal)
      throw error
    }
  }, [projectId, filter, rejectAccess])
  const catalogLoader = useCallback(async (signal: AbortSignal) => {
    try { return await loadProjectTasks(projectId, signal) }
    catch (error) {
      if (!signal.aborted) writableFacts.current = false
      rejectAccess(error, signal)
      throw error
    }
  }, [projectId, rejectAccess])
  const detailLoader = useCallback(async (signal: AbortSignal) => {
    const proof = revalidation.current
    try {
      const record = await loadSchedule(projectId, selection.id, signal)
      if (!signal.aborted && proof && revalidation.current === proof
        && proof.generation === denied.current.generation && denied.current.failure?.key !== 'sessionExpired') {
        denied.current = { failure: null, generation: denied.current.generation }
        revalidation.current = null
        setReadDenied(null)
      }
      return record
    } catch (error) {
      rejectAccess(error, signal)
      throw error
    }
  }, [projectId, selection, rejectAccess])
  const activityLoader = useCallback(async (signal: AbortSignal) => {
    try { return await loadScheduleActivity(projectId, selection.id, signal) }
    catch (error) {
      rejectAccess(error, signal)
      throw error
    }
  }, [projectId, selection, rejectAccess])
  const list = useResourceQuery(`${projectId}:list:${filter.revision}`, listLoader, onSessionEnded, SCHEDULE_READ_POLICY, enabled)
  const catalog = useResourceQuery(`${projectId}:catalog`, catalogLoader, onSessionEnded, SCHEDULE_READ_POLICY, enabled)
  const detail = useResourceQuery(`${projectId}:detail:${selection.id}:${selection.revision}`, detailLoader, onSessionEnded,
    SCHEDULE_READ_POLICY, enabled && Boolean(selection.id))
  const activity = useResourceQuery(`${projectId}:activity:${selection.id}:${selection.revision}`, activityLoader, onSessionEnded,
    SCHEDULE_READ_POLICY, enabled && Boolean(selection.id))
  // 失敗した再読取は現在の成功ではないが、原 editor/status の owner を破棄する理由でもない。
  const record = detail.data
  const task = record && !catalog.pending && !catalog.failure && catalog.data
    ? exactScheduleTask(record, catalog.data.tasks) : null
  const taskEligibility = scheduleTaskEditability(task)
  writableFacts.current = enabled && !detail.pending && !detail.failure && Boolean(record) && taskEligibility === 'ready'

  useEffect(() => {
    if (list.pending || list.failure || !list.data) return
    const offset = schedulePageOffset(filter.offset, list.data.total, list.data.limit)
    if (offset !== filter.offset) setFilter((current) => ({ ...current, offset, revision: current.revision + 1 }))
  }, [list.pending, list.failure, list.data, filter.offset])

  /** 旧 callback の編集開始も、同期的に失効済みの読取資格では受理しない。 */
  const canEdit = useCallback(() => writableFacts.current && !denied.current.failure, [])
  /** 行選択は一覧の古い値を編集に渡さず、原 ID の新しい GET を必ず要求する。 */
  const select = useCallback((id: string) => {
    writableFacts.current = false
    revalidation.current = null
    // render 前に選択が A→B→A と戻っても、旧読取の 401 を新 owner に渡さない。
    detail.refresh()
    activity.refresh()
    setSelection((current) => ({ id, revision: current.revision + 1 }))
  }, [detail.refresh, activity.refresh])
  /** 詳細の再照会開始時点で、旧画面 callback による編集開始を閉じる。 */
  const refreshDetail = useCallback(() => {
    writableFacts.current = false
    revalidation.current = { generation: denied.current.generation }
    detail.refresh()
  }, [detail.refresh])
  /** 書込回执による自動再読取は、人工の再認可意思を作成も再利用もしない。 */
  const refreshFacts = useCallback(() => {
    writableFacts.current = false
    revalidation.current = null
    detail.refresh()
  }, [detail.refresh])
  /** Catalog の再照会中は編集資格を証明できないが、Schedule の履歴は独立して残す。 */
  const refreshCatalog = useCallback(() => { writableFacts.current = false; catalog.refresh() }, [catalog.refresh])
  /** フィルタ適用は新しい読取世代にし、A→B→A の古い成功を使い回さない。 */
  const search = useCallback((q: string, status: ScheduleStatus | '') => {
    setFilter((current) => ({ q, status, offset: 0, revision: current.revision + 1 }))
  }, [])
  /** 页番号はサーバーの全件数に基づき、取得済み行の数だけで補完しない。 */
  const turnPage = useCallback((offset: number) => {
    setFilter((current) => ({ ...current, offset, revision: current.revision + 1 }))
  }, [])
  return { filter, selection, limit, list, catalog, detail, activity, record, task, taskEligibility, readDenied,
    canWrite: writableFacts.current && readDenied === null,
    canEdit, select, search, turnPage, refreshDetail, refreshFacts, refreshCatalog }
}
