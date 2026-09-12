import { API_BASE, isRecord, requestApiJson } from './http'
import { apiTimestampMicroseconds, isApiTimestamp, isNonNilUuid, sameUuid } from '../lib/validation'

/** 原書込の成功状態とは独立した只読核対の公開要約。 */
export interface ReconciliationRecord {
  request_id: string; project_id: string; run_id: string; effect_execution_id: string
  kind: 'DATABASE_TRANSACTION' | 'DOCUMENT_OBJECT'
  status: 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'REVOKED'
  created_at: string; finished_at: string | null
  observation_status: 'CONFIRMED' | 'NOT_OBSERVED' | 'CONFLICT' | null
  observed_at: string | null
  error_code: 'lookup_unavailable' | 'lookup_interrupted' | 'authorization_revoked' | 'target_changed' | null
}

/** HTTP の対象帰属と応答 identity を同時に検証する。 */
export interface ReconciliationScope { projectId: string; runId: string; effectId: string }
const KEYS = ['request_id', 'project_id', 'run_id', 'effect_execution_id', 'kind', 'status', 'created_at', 'finished_at', 'observation_status', 'observed_at', 'error_code']

/** 余分な内部 receipt/owner と不整合な終態を public boundary で拒否する。 */
export function parseReconciliation(value: unknown, scope: ReconciliationScope, requestId?: string): ReconciliationRecord {
  if (!isRecord(value) || Object.keys(value).length !== KEYS.length || !KEYS.every((key) => key in value)
    || !['request_id', 'project_id', 'run_id', 'effect_execution_id'].every((key) => isNonNilUuid(value[key]))
    || !sameUuid(String(value.project_id), scope.projectId) || !sameUuid(String(value.run_id), scope.runId)
    || !sameUuid(String(value.effect_execution_id), scope.effectId)
    || (requestId !== undefined && !sameUuid(String(value.request_id), requestId))
    || !['DATABASE_TRANSACTION', 'DOCUMENT_OBJECT'].includes(String(value.kind))
    || !isApiTimestamp(value.created_at)
    || !(value.finished_at === null || isApiTimestamp(value.finished_at))
    || !(value.observed_at === null || isApiTimestamp(value.observed_at))) throw new Error('Invalid reconciliation response')
  const active = value.status === 'QUEUED' || value.status === 'RUNNING'
  const succeeded = value.status === 'SUCCEEDED'
  const failed = value.status === 'FAILED' || value.status === 'REVOKED'
  if ((!active && !succeeded && !failed)
    || (active && [value.finished_at, value.observation_status, value.observed_at, value.error_code].some((v) => v !== null))
    || (succeeded && (value.finished_at === null || value.observed_at === null || value.error_code !== null
      || !['CONFIRMED', 'NOT_OBSERVED', 'CONFLICT'].includes(String(value.observation_status))))
    || (failed && (value.finished_at === null || value.observed_at !== null || value.observation_status !== null
      || !['lookup_unavailable', 'lookup_interrupted', 'authorization_revoked', 'target_changed'].includes(String(value.error_code))))) {
    throw new Error('Invalid reconciliation state')
  }
  if ((typeof value.finished_at === 'string' && apiTimestampMicroseconds(value.finished_at) < apiTimestampMicroseconds(value.created_at))
    || (typeof value.observed_at === 'string' && (apiTimestampMicroseconds(value.observed_at) < apiTimestampMicroseconds(value.created_at)
      || apiTimestampMicroseconds(value.observed_at) > apiTimestampMicroseconds(String(value.finished_at))))) throw new Error('Invalid reconciliation time')
  return value as unknown as ReconciliationRecord
}

/** scope の固定 path だけを使い、接続や会話 ID は HTTP に持ち込まない。 */
function base(scope: ReconciliationScope): string {
  return `${API_BASE}/projects/${encodeURIComponent(scope.projectId)}/runs/${encodeURIComponent(scope.runId)}`
}

/** 現在 Project の最新核対だけを取得する。 */
export async function loadLatestReconciliation(scope: ReconciliationScope, signal?: AbortSignal): Promise<ReconciliationRecord | null> {
  const value = await requestApiJson(`${base(scope)}/effects/${encodeURIComponent(scope.effectId)}/reconciliations/latest`, { signal }, 200)
  if (!isRecord(value) || Object.keys(value).length !== 1 || !('latest' in value)) throw new Error('Invalid latest reconciliation')
  return value.latest === null ? null : parseReconciliation(value.latest, scope)
}

/** 応答を失った元 UUID のみを確認する。 */
export async function confirmReconciliation(scope: ReconciliationScope, requestId: string, signal?: AbortSignal): Promise<ReconciliationRecord> {
  return parseReconciliation(await requestApiJson(`${base(scope)}/effect-reconciliations/${encodeURIComponent(requestId)}`, { signal }, 200), scope, requestId)
}

/** 同じ UUID の原要求だけを受理し、API から外部 read/write は呼ばない。 */
export async function requestReconciliation(scope: ReconciliationScope, requestId: string, csrfToken: string, signal?: AbortSignal): Promise<ReconciliationRecord> {
  return parseReconciliation(await requestApiJson(`${base(scope)}/effects/${encodeURIComponent(scope.effectId)}/reconciliations`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify({ request_id: requestId }), signal,
  }, 202), scope, requestId)
}
