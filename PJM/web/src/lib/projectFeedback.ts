import { ApiProblemError } from '../api'
import type { ResourceRequestPolicy } from '../hooks/useResourceRequest'
import type { UiMessages } from './i18n/messages'

/** Project の既知拒否と結果不明を分け、Server 本文は画面へ渡さない。 */
export interface ProjectFailure {
  key: 'sessionExpired' | 'csrfRejected' | 'adminRequired' | 'notFound' | 'invalidRequest'
    | 'keyConflict' | 'versionConflict' | 'versionExhausted' | 'needsArchive'
    | 'blockedByRuns' | 'blockedBySchedules' | 'blockedByMemberAudit' | 'unknown' | 'loadFailed'
}

/** 409 の理由を区別し、読取失敗を write の rollback と解釈しない。 */
export function projectFailure(error: unknown, mutation: boolean): ProjectFailure {
  if (error instanceof ApiProblemError) {
    const { status, code } = error
    if (status === 401) return { key: 'sessionExpired' }
    if (status === 403 && code === 'csrf_rejected') return { key: 'csrfRejected' }
    if (status === 403 && code === 'administrator_required') return { key: 'adminRequired' }
    if (status === 404 && code === 'project_not_found') return { key: 'notFound' }
    if (status === 422 && code === 'validation_error') return { key: 'invalidRequest' }
    if (status === 409) {
      if (code === 'project_key_conflict') return { key: 'keyConflict' }
      if (code === 'project_version_conflict') return { key: 'versionConflict' }
      if (code === 'project_version_exhausted') return { key: 'versionExhausted' }
      if (code === 'project_delete_requires_archive') return { key: 'needsArchive' }
      if (code === 'project_delete_blocked_by_runs') return { key: 'blockedByRuns' }
      if (code === 'project_delete_blocked_by_schedules') return { key: 'blockedBySchedules' }
      if (code === 'project_delete_blocked_by_member_audit') return { key: 'blockedByMemberAudit' }
    }
  }
  return { key: mutation ? 'unknown' : 'loadFailed' }
}

/** 共用 request 境界を使い、未知と版本衝突は人の確認まで再送禁止にする。 */
export const PROJECT_REQUEST_POLICY: ResourceRequestPolicy<ProjectFailure> = {
  classify: projectFailure,
  readTimeout: { key: 'loadFailed' },
  writeTimeout: { key: 'unknown' },
  blocks: ({ key }) => key === 'unknown' || key === 'versionConflict',
}

/** 旧 import の互換入口も任意の Error.message を表示しない。 */
export function projectDeleteErrorMessage(error: unknown, messages: UiMessages): string {
  const key = projectFailure(error, true).key
  if (key === 'blockedByRuns') return messages.projects.deleteBlockedByRuns
  if (key === 'blockedBySchedules') return messages.projects.deleteBlockedBySchedules
  if (key === 'blockedByMemberAudit') return messages.projects.deleteBlockedByMemberAudit
  if (key === 'needsArchive') return messages.projects.deleteNeedsArchive
  return messages.projectManagement.failures[key]
}
