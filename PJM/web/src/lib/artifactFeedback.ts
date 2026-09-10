import { ApiProblemError, type RunResultDetail } from '../api'
import type { ResourceRequestPolicy } from '../hooks/useResourceRequest'

/** 読取拒否だけを分類し、Server の内部 Problem 本文を画面に出さない。 */
export interface ArtifactFailure {
  key: 'sessionExpired' | 'denied' | 'notFound' | 'contentInvalid' | 'storageUnavailable' | 'tooLarge' | 'loadFailed' | 'timeout'
}

/** 原添付の不存在を Project/Run の読取拒否と区別する。 */
export function artifactFailure(error: unknown): ArtifactFailure {
  if (error instanceof ApiProblemError) {
    if (error.status === 401) return { key: 'sessionExpired' }
    if (error.status === 404 && error.code === 'artifact_not_found') return { key: 'notFound' }
    if (error.status === 403 || error.status === 404) return { key: 'denied' }
    if (error.status === 409 && error.code === 'artifact_content_invalid') return { key: 'contentInvalid' }
    if (error.status === 503 && error.code === 'artifact_storage_unavailable') return { key: 'storageUnavailable' }
    if (error.code === 'response_too_large') return { key: 'tooLarge' }
  }
  return { key: 'loadFailed' }
}

/** 通常 query の原要求/期限門禁を再利用し、ダウンロードを mutation と解釈しない。 */
export const ARTIFACT_REQUEST_POLICY: ResourceRequestPolicy<ArtifactFailure> = {
  classify: artifactFailure, readTimeout: { key: 'timeout' }, writeTimeout: { key: 'timeout' }, blocks: () => false,
}

/** モデルの任意 business field や URL は拾わず、宣言済み位置の文字列だけを表示する。 */
export function resultArtifactRefs(result: RunResultDetail | null): string[] {
  if (!result) return []
  const refs = [...result.artifact_refs]
  if (result.result_kind === 'OUTCOME_ENVELOPE' && Array.isArray(result.data.deliverables)) {
    for (const item of result.data.deliverables) {
      if (typeof item === 'object' && item !== null && 'artifact_ref' in item && typeof item.artifact_ref === 'string') refs.push(item.artifact_ref)
    }
  }
  return [...new Set(refs)]
}
