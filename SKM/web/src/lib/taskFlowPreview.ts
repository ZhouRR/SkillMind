import { ApiProblemError } from '../api'
import type { ResourceRequestPolicy } from '../hooks/useResourceRequest'

/** Server 本文を公開せず、読取だけの拒否と未確認を安定した三語 key に分類する。 */
export interface TaskFlowFailure { key: 'sessionExpired' | 'unavailable' | 'invalid' | 'loadFailed' | 'timeout' }
/** 欠けた宣言は合法 200 だけで判定する。404/503 は空計画へ変換しない。 */
export function classifyTaskFlowFailure(error: unknown): TaskFlowFailure {
  if (error instanceof ApiProblemError) {
    if (error.status === 401) return { key: 'sessionExpired' }
    if ([403, 404].includes(error.status)) return { key: 'unavailable' }
    if (error.status === 409 && error.code === 'task_flow_preview_invalid') return { key: 'invalid' }
    return { key: 'loadFailed' }
  }
  return error instanceof TypeError ? { key: 'loadFailed' } : { key: 'invalid' }
}
/** 新しい request lifecycle を作らず、共通 deadline と current gate を使用する。 */
export const TASK_FLOW_POLICY: ResourceRequestPolicy<TaskFlowFailure> = {
  classify: classifyTaskFlowFailure, readTimeout: { key: 'timeout' }, writeTimeout: { key: 'timeout' }, blocks: () => false,
}
