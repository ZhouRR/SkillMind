import {
  API_BASE,
  hasStrings,
  isRecord,
  parseItemList,
  requestApiEmpty,
  requestApiJson,
} from './http'

/** Project の API 公開状態。 */
export type ProjectStatus = 'ACTIVE' | 'ARCHIVED'

/** Actor が参照できる Project metadata。 */
export interface ProjectRecord {
  project_id: string
  key: string
  name: string
  description: string
  status: ProjectStatus
  settings: Record<string, unknown>
  retention_days: number
  created_at: string
  updated_at: string
}

/** ADMIN が Project を作成する入力。 */
export interface CreateProjectInput {
  key: string
  name: string
  description: string
  settings: Record<string, unknown>
  retention_days: number
}

/** ADMIN が変更できる Project metadata。 */
export type UpdateProjectInput = Partial<Pick<
  CreateProjectInput,
  'name' | 'description' | 'settings' | 'retention_days'
>>

/** Credential を含まない Project membership。 */
export interface ProjectMemberRecord {
  user_id: string
  email: string
  display_name: string
  status: 'ACTIVE' | 'REMOVED'
  joined_at: string
}

/** Actor が参照可能な Project 一覧を取得する。 */
export async function loadProjects(
  includeArchived = false,
  signal?: AbortSignal,
): Promise<ProjectRecord[]> {
  const query = includeArchived ? '?include_archived=true' : ''
  const value = await requestApiJson(`${API_BASE}/projects${query}`, { signal })
  return parseItemList(value, 'items', isProject, 'Project list response did not match its contract')
}

/** ADMIN と CSRF token を使って Project を作成する。 */
export async function createProject(
  input: CreateProjectInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectRecord> {
  return parseProject(await requestApiJson(`${API_BASE}/projects`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify(input),
    signal,
  }))
}

/** ADMIN と CSRF token を使って Project metadata を更新する。 */
export async function updateProject(
  projectId: string,
  input: UpdateProjectInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectRecord> {
  return parseProject(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify(input),
      signal,
    },
  ))
}

/** ADMIN が Project を物理削除せず ARCHIVED にする。 */
export async function archiveProject(
  projectId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectRecord> {
  return parseProject(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/archive`,
    { method: 'POST', headers: { 'X-CSRF-Token': csrfToken }, signal },
  ))
}

/** ADMIN が ARCHIVED Project を ACTIVE へ戻す。
 *
 * key は archive しても解放されないため、同じ key の作成が 409 になった場合の復旧経路は
 * 新規作成ではなくこの復元になる。
 */
export async function unarchiveProject(
  projectId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectRecord> {
  return parseProject(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/unarchive`,
    { method: 'POST', headers: { 'X-CSRF-Token': csrfToken }, signal },
  ))
}

/** ADMIN が Run 履歴のない ARCHIVED Project を物理削除し key を解放する。
 *
 * 削除できない場合の理由は `ApiProblemError.code` で区別する
 * (`project_delete_requires_archive` / `project_delete_blocked_by_runs`)。
 */
export async function deleteProject(
  projectId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<void> {
  await requestApiEmpty(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}`,
    { method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken }, signal },
  )
}

/** ADMIN が Project membership 一覧を取得する。 */
export async function loadProjectMembers(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectMemberRecord[]> {
  const value = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/members`,
    { signal },
  )
  return parseItemList(
    value,
    'items',
    isProjectMember,
    'Project member list response did not match its contract',
  )
}

/** ADMIN が ACTIVE User を Project member に追加する。 */
export async function addProjectMember(
  projectId: string,
  userId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectMemberRecord> {
  return parseProjectMember(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(userId)}`,
    { method: 'PUT', headers: { 'X-CSRF-Token': csrfToken }, signal },
  ))
}

/** ADMIN が Project membership を REMOVED にする。 */
export async function removeProjectMember(
  projectId: string,
  userId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<void> {
  await requestApiEmpty(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(userId)}`,
    { method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken }, signal },
  )
}

/** Unknown JSON を Project response へ制限する。 */
function parseProject(value: unknown): ProjectRecord {
  if (!isProject(value)) throw new Error('Project response did not match its contract')
  return value
}

/** Unknown object が Project response の全公開 field を持つことを検証する。 */
function isProject(value: unknown): value is ProjectRecord {
  return isRecord(value)
    && hasStrings(value, [
      'project_id', 'key', 'name', 'description', 'status', 'created_at', 'updated_at',
    ])
    && typeof value.status === 'string'
    && ['ACTIVE', 'ARCHIVED'].includes(value.status)
    && isRecord(value.settings)
    && typeof value.retention_days === 'number'
    && Number.isInteger(value.retention_days)
}

/** Unknown JSON を ProjectMember response へ制限する。 */
function parseProjectMember(value: unknown): ProjectMemberRecord {
  if (!isProjectMember(value)) {
    throw new Error('Project member response did not match its contract')
  }
  return value
}

/** Unknown object が credential 非含有 membership contract を満たすか検証する。 */
function isProjectMember(value: unknown): value is ProjectMemberRecord {
  return isRecord(value)
    && hasStrings(value, ['user_id', 'email', 'display_name', 'status', 'joined_at'])
    && typeof value.status === 'string'
    && ['ACTIVE', 'REMOVED'].includes(value.status)
}
