import { ApiProblemError } from '../api'
import type { ResourceRequestPolicy } from '../hooks/useResourceRequest'

/** 停止固有の拒否を upload/delete の確定結果として扱わせない。 */
export interface DocumentUploadClosureFailure {
  key: 'sessionExpired' | 'denied' | 'archived' | 'alreadyPublished' | 'uploadNotFound'
    | 'notFound' | 'unavailable' | 'invalid' | 'unknown' | 'loadFailed' | 'invalidKey'
}

/** 生の Problem detail を表示せず、GET の 404 は未確認のまま保持する。 */
export function documentUploadClosureFailure(error: unknown, mutation: boolean): DocumentUploadClosureFailure {
  if (error instanceof ApiProblemError) {
    if (error.status === 401) return { key: 'sessionExpired' }
    if (error.status === 403 || error.status === 404 && error.code === 'project_not_found') return { key: 'denied' }
    if (error.status === 409 && error.code === 'project_archived') return { key: 'archived' }
    if (mutation && error.status === 409 && error.code === 'document_upload_already_published') return { key: 'alreadyPublished' }
    if (mutation && error.status === 404 && error.code === 'document_upload_not_found') return { key: 'uploadNotFound' }
    if (!mutation && error.status === 404 && error.code === 'document_upload_closure_not_found') return { key: 'notFound' }
    if (error.status === 503 && error.code === 'document_upload_unavailable') return { key: mutation ? 'unknown' : 'unavailable' }
    if (mutation && error.status === 422 && error.code === 'validation_error') return { key: 'invalid' }
  }
  return { key: mutation ? 'unknown' : 'loadFailed' }
}

/** deadline と同期防重は既存の要求 hook に委ねる。 */
export const DOCUMENT_UPLOAD_CLOSURE_POLICY: ResourceRequestPolicy<DocumentUploadClosureFailure> = {
  classify: documentUploadClosureFailure,
  readTimeout: { key: 'loadFailed' }, writeTimeout: { key: 'unknown' },
  blocks: ({ key }) => ['unknown', 'sessionExpired', 'denied', 'archived'].includes(key),
}
