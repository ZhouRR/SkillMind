import { useCallback, useEffect, useRef, useState } from 'react'
import { compareEvaluations, loadEvaluationPage, type EvaluationRecord } from '../api'
import { EVALUATION_REQUEST_POLICY, evaluationFailure, mergeEvaluationRecords, type EvaluationFailure } from '../lib/evaluationSubmission'
import { useResourceQuery, type SessionEnded } from './useResourceRequest'

/** 履歴の cursor と原受付記録は独立させ、古いページで直近の確認済み保存を失わない。 */
export function useEvaluationHistory(projectId: string, runId: string, resultId: string,
  onSessionExpired: SessionEnded, onFailure: (failure: EvaluationFailure) => void) {
  const [after, setAfter] = useState<string | null>(null)
  const [items, setItems] = useState<EvaluationRecord[]>([])
  const saved = useRef<EvaluationRecord[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [localFailure, setLocalFailure] = useState<EvaluationFailure | null>(null)
  const loading = useRef(false)
  const consumed = useRef<unknown>(null)
  const loader = useCallback(async (signal: AbortSignal) => {
    const page = await loadEvaluationPage(projectId, runId, resultId, after, 20, signal)
    const anchor = after === null ? null : saved.current.find((item) => item.evaluation_id.toLowerCase() === after.toLowerCase())
    if (after !== null && (!anchor || page.items.some((item) => compareEvaluations(anchor, item) >= 0))) throw new Error('Invalid evaluation page order')
    // receipt は読み込み開始後にも増え得るので、merge の矛盾を成功として渡さない。
    mergeEvaluationRecords(saved.current, page.items)
    return page
  }, [projectId, runId, resultId, after])
  const observe = useCallback((error: unknown) => onFailure(evaluationFailure(error, false)), [onFailure])
  const query = useResourceQuery('evaluation-history', loader, onSessionExpired, EVALUATION_REQUEST_POLICY, true, observe)
  useEffect(() => {
    if (query.pending) return
    loading.current = false
    if (!query.data || query.failure || consumed.current === query.data) return
    consumed.current = query.data
    try {
      const merged = mergeEvaluationRecords(saved.current, query.data.items).sort(compareEvaluations)
      saved.current = merged; setItems(merged); setNextCursor(query.data.next_cursor); setLocalFailure(null)
    } catch { setLocalFailure({ key: 'loadFailed' }) }
  }, [query.pending, query.data, query.failure])
  /** 同じ受付記録は一度だけ追加し、一覧の進行 cursor を保存時刻へ飛ばさない。 */
  const remember = useCallback((item: EvaluationRecord) => {
    try {
      const merged = mergeEvaluationRecords(saved.current, [item]).sort(compareEvaluations)
      saved.current = merged; setItems(merged)
    } catch { setLocalFailure({ key: 'loadFailed' }) }
  }, [])
  /** 利用者の操作一回につき一ページだけ取得し、最初の20件を全件と呼ばない。 */
  function more(): void {
    if (loading.current || query.pending || !nextCursor || query.failure || localFailure) return
    loading.current = true; setAfter(nextCursor)
  }
  /** 再読取は保存済み受付記録を消さず、server cursor の先頭からたどり直す。 */
  function refresh(): void {
    if (loading.current || query.pending) return
    loading.current = true; setAfter(null); query.refresh(); setLocalFailure(null)
  }
  return { items, pending: query.pending, failure: query.failure ?? localFailure, nextCursor, more, refresh, remember }
}
