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

/** Upload の表示語彙は DELETE の門禁 policy に渡せない独立した型に限定する。 */
export interface DocumentUploadFailure {
  key: Exclude<DocumentFailure['key'], 'unknown'> | 'uploadTooLarge' | 'uploadTypeNotAllowed' | 'uploadUnknown'
    | 'uploadPending' | 'uploadKeyConflict' | 'uploadConflict' | 'uploadNotFound'
    | 'uploadUnavailable' | 'uploadInvalidKey' | 'uploadPreparationFailed' | 'uploadClosed'
}

/** Upload 固有の確定拒否を DELETE の未知判定と混同せず、本文も表示しない。 */
export function documentUploadFailure(error: unknown, mutation = true): DocumentUploadFailure {
  if (error instanceof ApiProblemError) {
    if (mutation && error.status === 422 && error.code === 'content_type_not_allowed') return { key: 'uploadTypeNotAllowed' }
    if (error.status === 409 && error.code === 'document_upload_closed') return { key: 'uploadClosed' }
    if (error.status === 409 && error.code === 'document_upload_pending') return { key: 'uploadPending' }
    if (error.status === 409 && error.code === 'document_upload_key_conflict') return { key: 'uploadKeyConflict' }
    if (mutation && error.status === 409 && error.code === 'document_conflict') return { key: 'uploadConflict' }
    if (!mutation && error.status === 404 && error.code === 'document_upload_not_found') return { key: 'uploadNotFound' }
    if (!mutation && error.status === 503 && error.code === 'document_upload_unavailable') return { key: 'uploadUnavailable' }
  }
  if (mutation && error instanceof ApiProblemError && error.status === 413
    && error.code === 'document_upload_too_large') return { key: 'uploadTooLarge' }
  const reason = documentFailure(error, mutation)
  return { key: reason.key === 'unknown' ? 'uploadUnknown' : reason.key }
}

/** 拒否された資格は同じ一覧の成功で戻さず、未知は明示核対まで新規書込を閉じる。 */
export const DOCUMENT_REQUEST_POLICY: ResourceRequestPolicy<DocumentFailure> = {
  classify: documentFailure,
  readTimeout: { key: 'loadFailed' }, writeTimeout: { key: 'unknown' },
  blocks: ({ key }) => ['unknown', 'sessionExpired', 'denied', 'archived'].includes(key),
}
