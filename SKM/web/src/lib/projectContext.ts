import type { ProjectRecord } from '../api'
import { routeFromHash } from './routing'
import { isUuid } from './validation'

/** URL の対象と未指定を分ける。空・重複・不正 UUID を既定 Project に読み替えない。 */
export type ProjectRequest =
  | { kind: 'absent' }
  | { kind: 'explicit'; projectId: string }
  | { kind: 'invalid'; value: string }

/** Platform の草稿境界。初回の既定選択は人による Project 切替と区別する。 */
export interface ProjectManagementBoundary {
  owner: string
  selectionId: string
  revision: number
}

/** 未指定 URL への初回一覧到着だけでは作成草稿を消さず、以後の対象変更は旧要求を隔離する。 */
export function nextProjectManagementBoundary(
  current: ProjectManagementBoundary, owner: string, selectionId: string, request: ProjectRequest,
): ProjectManagementBoundary {
  const selected = selectionId.toLowerCase()
  if (current.owner !== owner) return { owner, selectionId: selected, revision: 0 }
  if (current.selectionId === selected) return current
  const initialDefault = current.selectionId === '' && request.kind === 'absent'
  return { owner, selectionId: selected, revision: current.revision + (initialDefault ? 0 : 1) }
}

/** Account の URL は Project 文脈を持たず、それ以外は明示 parameter を厳密に読む。 */
export function projectRequestFromHash(hash: string): ProjectRequest {
  if (routeFromHash(hash) === 'accounts') return { kind: 'absent' }
  const queryStart = hash.indexOf('?')
  const values = new URLSearchParams(queryStart < 0 ? '' : hash.slice(queryStart + 1)).getAll('project')
  if (values.length === 0) return { kind: 'absent' }
  const value = values[0] ?? ''
  if (values.length !== 1 || !isUuid(value)) return { kind: 'invalid', value }
  return { kind: 'explicit', projectId: value }
}

/** 対象の選択だけを行い、認可は詳細 API に任せる。明示対象は一覧外でも保持する。 */
export function resolveProjectSelection(
  projects: readonly ProjectRecord[],
  requestedProjectId: string | null,
  preferredProjectId: string | null,
): string {
  if (requestedProjectId !== null) return requestedProjectId
  const active = projects.filter(({ status }) => status === 'ACTIVE')
  return active.find(({ project_id }) => project_id.toLowerCase() === preferredProjectId?.toLowerCase())?.project_id
    ?? active[0]?.project_id ?? ''
}
