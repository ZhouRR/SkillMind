import { API_BASE, isRecord, requestApiJson } from './http'
import { isUuid, sameUuid } from '../lib/validation'

export interface RunDeletionPreview {
  cleanup_pending: number; run_id: string; deleted: boolean; output_count: number; protected_output_count: number
  outputs: { document_id: string; name: string; folder: string; protected: boolean }[]
}

/** 破損・別 Run の応答を削除確認や成功として扱わない。 */
function parse(value: unknown, runId: string): RunDeletionPreview {
  if (!isRecord(value) || !isUuid(value.run_id) || !sameUuid(value.run_id, runId)
    || typeof value.deleted !== 'boolean' || typeof value.cleanup_pending !== 'number' || !Number.isSafeInteger(value.cleanup_pending) || value.cleanup_pending < 0 || !Number.isSafeInteger(value.output_count)
    || !Number.isSafeInteger(value.protected_output_count) || !Array.isArray(value.outputs)
    || !value.outputs.every((v: unknown) => isRecord(v) && isUuid(v.document_id) && typeof v.name === 'string' && typeof v.folder === 'string' && typeof v.protected === 'boolean')) throw new Error('Invalid deletion response')
  if (value.output_count !== value.outputs.length || value.protected_output_count !== value.outputs.filter((d: unknown) => isRecord(d) && d.protected).length) throw new Error('Invalid output counts')
  return value as unknown as RunDeletionPreview
}

/** 変更前に現在の公開成果と参照保護を取得する。 */
export async function previewRunDeletion(projectId: string, runId: string, signal: AbortSignal): Promise<RunDeletionPreview> {
  return parse(await requestApiJson(`${API_BASE}/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}/deletion-preview`, { signal, cache: 'no-store' }, 200), runId)
}

/** 回収箱操作は一度だけ送信し、外部操作の再実行は行わない。 */
export async function changeRunDeletion(projectId: string, runId: string, action: 'TRASH' | 'RESTORE' | 'PURGE', includeOutputs: boolean, csrfToken: string, signal: AbortSignal): Promise<RunDeletionPreview> {
  return parse(await requestApiJson(`${API_BASE}/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}${action === 'RESTORE' ? '/restore' : action === 'PURGE' ? '/purge' : ''}`, {
    method: action === 'RESTORE' ? 'POST' : 'DELETE', headers: { 'X-CSRF-Token': csrfToken, 'Content-Type': 'application/json' },
    ...(action === 'RESTORE' ? {} : { body: JSON.stringify({ include_outputs: includeOutputs }) }), signal, cache: 'no-store',
  }, 200), runId)
}
