import { ApiProblemError } from '../api'
import type { ResourceRequestPolicy } from '../hooks/useResourceRequest'

/** Problem 本文を表示せず、原 ID の参照拒否と通信結果未知を区別する。 */
export interface DocumentFailure {
  key: 'sessionExpired' | 'denied' | 'archived' | 'notFound' | 'inUse' | 'referencesUnavailable'
    | 'invalid' | 'unknown' | 'loadFailed'
    | 'previewTooLarge' | 'contentMissing' | 'contentInvalid' | 'storageUnavailable'
}

/** 未知の成功/409/5xx は既知拒否にせず、原対象の読取を必要とする。 */
export function documentFailure(error: unknown, mutation: boolean): DocumentFailure {
  if (error instanceof ApiProblemError) {
    if (error.status === 401) return { key: 'sessionExpired' }
    if (error.status === 403 || error.status === 404 && error.code !== 'document_not_found') return { key: 'denied' }
    if (error.status === 404 && error.code === 'document_not_found') return { key: 'notFound' }
    if (error.status === 409 && error.code === 'project_archived') return { key: 'archived' }
    if (error.status === 409 && error.code === 'document_in_use') return { key: 'inUse' }
    if (error.status === 409 && error.code === 'document_references_unavailable') return { key: 'referencesUnavailable' }
    if (error.status === 422) return { key: 'invalid' }
    if (!mutation) {
      if (error.status === 200 && error.code === 'response_too_large') return { key: 'previewTooLarge' }
      if (error.status === 409 && error.code === 'document_content_missing') return { key: 'contentMissing' }
      if (error.status === 409 && error.code === 'document_content_invalid') return { key: 'contentInvalid' }
      if (error.status === 503 && error.code === 'document_storage_unavailable') return { key: 'storageUnavailable' }
    }
  }
  return { key: mutation ? 'unknown' : 'loadFailed' }
}

/** 拒否された資格は同じ一覧の成功で戻さず、未知は明示核対まで新規書込を閉じる。 */
export const DOCUMENT_REQUEST_POLICY: ResourceRequestPolicy<DocumentFailure> = {
  classify: documentFailure,
  readTimeout: { key: 'loadFailed' }, writeTimeout: { key: 'unknown' },
  blocks: ({ key }) => ['unknown', 'sessionExpired', 'denied', 'archived'].includes(key),
}
