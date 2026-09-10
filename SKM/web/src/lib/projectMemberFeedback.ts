import { ApiProblemError } from '../api'
import type { ResourceRequestPolicy } from '../hooks/useResourceRequest'

/** 成員管理の拒否を本文・email・内部理由を含まない固定分類にする。 */
export interface ProjectMemberFailure {
  key: 'sessionExpired' | 'csrfRejected' | 'adminRequired' | 'notFound' | 'invalidRequest' | 'unknown' | 'loadFailed'
}

/** 既知拒否以外の書込失敗は結果未知であり、自動再送や成功推測をしない。 */
export function projectMemberFailure(error: unknown, mutation: boolean): ProjectMemberFailure {
  if (error instanceof ApiProblemError) {
    if (error.status === 401) return { key: 'sessionExpired' }
    if (error.status === 403 && error.code === 'csrf_rejected') return { key: 'csrfRejected' }
    if (error.status === 403 && error.code === 'administrator_required') return { key: 'adminRequired' }
    if (error.status === 404 && ['project_not_found', 'user_not_found'].includes(error.code ?? '')) return { key: 'notFound' }
    if (error.status === 422 && error.code === 'validation_error') return { key: 'invalidRequest' }
  }
  return { key: mutation ? 'unknown' : 'loadFailed' }
}

/** 現在の関係が一致しても、未知書込の送信禁止を自動で解除しない。 */
export const PROJECT_MEMBER_REQUEST_POLICY: ResourceRequestPolicy<ProjectMemberFailure> = {
  classify: projectMemberFailure,
  readTimeout: { key: 'loadFailed' },
  writeTimeout: { key: 'unknown' },
  blocks: (failure) => failure.key === 'unknown',
}
