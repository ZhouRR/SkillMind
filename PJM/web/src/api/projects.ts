import { isApiTimestamp, isUuid } from '../lib/validation'
import {
  API_BASE,
  exactFields,
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
  row_version: number
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
export type UpdateProjectInput = { expected_row_version: number } & Partial<Pick<
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
  signal?.throwIfAborted()
  const value = await requestApiJson(`${API_BASE}/projects${query}`, { signal, cache: 'no-store' }, 200)
  signal?.throwIfAborted()
  if (!isRecord(value) || !exactFields(value, ['items'])) {
    throw new Error('Project list response did not match its contract')
  }
  const projects = parseItemList(value, 'items', isProject, 'Project list response did not match its contract')
  if (new Set(projects.map((project) => project.project_id.toLowerCase())).size !== projects.length
    || new Set(projects.map((project) => project.key)).size !== projects.length
    || !includeArchived && projects.some((project) => project.status !== 'ACTIVE')) {
    throw new Error('Project list response did not match its requested scope')
  }
  return projects
}

/** 明示された Project だけを取得し、活動一覧にない認可済み履歴も読み取る。 */
export async function loadProject(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectRecord> {
  return sameProject(await callProject(projectPath(projectId), { signal }), projectId)
}

/** ADMIN と CSRF token を使って Project を作成する。 */
export async function createProject(
  input: CreateProjectInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectRecord> {
  const { key, name, description, retention_days } = input
  const project = await callProject(`${API_BASE}/projects`, projectMutation(input, csrfToken, signal), 201)
  if (project.key !== key || project.status !== 'ACTIVE' || project.row_version !== 1
    || project.name !== name || project.description !== description || project.retention_days !== retention_days) {
    throw new Error('Project response did not match the requested creation')
  }
  return project
}

/** ADMIN と CSRF token を使って Project metadata を更新する。 */
export async function updateProject(
  projectId: string,
  input: UpdateProjectInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectRecord> {
  const version = projectVersion(input.expected_row_version)
  const { name, description, retention_days } = input
  const project = await callProject(projectPath(projectId), projectMutation(
    { ...input, ...version }, csrfToken, signal, 'PATCH',
  ))
  sameProjectVersion(project, projectId, version.expected_row_version)
  if (name !== undefined && project.name !== name
    || description !== undefined && project.description !== description
    || retention_days !== undefined && project.retention_days !== retention_days) {
    throw new Error('Project response did not match its requested metadata')
  }
  return project
}

/** ADMIN が Project を物理削除せず ARCHIVED にする。 */
export async function archiveProject(
  projectId: string,
  expectedRowVersion: number,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectRecord> {
  return changeProjectStatus(projectId, expectedRowVersion, 'ARCHIVED', csrfToken, signal)
}

/** ADMIN が ARCHIVED Project を ACTIVE へ戻す。
 *
 * key は archive しても解放されないため、同じ key の作成が 409 になった場合の復旧経路は
 * 新規作成ではなくこの復元になる。
 */
export async function unarchiveProject(
  projectId: string,
  expectedRowVersion: number,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectRecord> {
  return changeProjectStatus(projectId, expectedRowVersion, 'ACTIVE', csrfToken, signal)
}

/** ADMIN が Run/Schedule のない ARCHIVED Project を物理削除し key を解放する。
 *
 * 削除できない場合の理由は `ApiProblemError.code` で区別する
 * 版競合、未帰档、Run/Schedule/成員監査の各拒否を保持する。
 * 拒否されても参照を削除したり版を更新して自動再送したりしない。
 */
export async function deleteProject(
  projectId: string,
  expectedRowVersion: number,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<void> {
  signal?.throwIfAborted()
  const version = projectVersion(expectedRowVersion)
  const query = new URLSearchParams({ expected_row_version: String(version.expected_row_version) })
  await requestApiEmpty(`${projectPath(projectId)}?${query}`, {
    method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken }, signal, cache: 'no-store',
  }, 204)
  signal?.throwIfAborted()
}

/** UUID 以外を URL の path/query として解釈しない。 */
function projectPath(projectId: string): string {
  if (!isUuid(projectId)) throw new Error('Invalid project identity')
  return `${API_BASE}/projects/${encodeURIComponent(projectId)}`
}

/** DB/public contract と同じ整数範囲を保ち、欠版を現在版で補わない。 */
function isProjectVersion(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 2147483647
}

/** 表示時の原版をそのまま送信する。 */
function projectVersion(value: number): { expected_row_version: number } {
  if (!isProjectVersion(value)) throw new Error('Invalid project version')
  return { expected_row_version: value }
}

/** 全 metadata response に cache/abort 検証を一度だけ適用する。 */
async function callProject(url: string, init: RequestInit, expectedStatus = 200): Promise<ProjectRecord> {
  init.signal?.throwIfAborted()
  const value = await requestApiJson(url, { ...init, cache: 'no-store' }, expectedStatus)
  init.signal?.throwIfAborted()
  return parseProject(value)
}

/** CSRF を送る一回の要求を作り、再送権限や幂等性は追加しない。 */
function projectMutation(body: object, csrfToken: string, signal?: AbortSignal, method = 'POST'): RequestInit {
  return { method, signal, body: JSON.stringify(body), headers: {
    'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken,
  } }
}

/** 別 Project の合法な response も原要求の成功とは認めない。 */
function sameProject(project: ProjectRecord, projectId: string): ProjectRecord {
  if (project.project_id.toLowerCase() !== projectId.toLowerCase()) {
    throw new Error('Project response did not match its requested identity')
  }
  return project
}

/** No-op の原版と一度だけ変更した新版以外を受理しない。 */
function sameProjectVersion(project: ProjectRecord, projectId: string, expected: number): ProjectRecord {
  sameProject(project, projectId)
  if (project.row_version !== expected && project.row_version !== expected + 1) {
    throw new Error('Project response did not match its requested version')
  }
  return project
}

/** 帰档/復元の同一プロトコルを共有し、反対状態の応答を成功にしない。 */
async function changeProjectStatus(
  projectId: string, expected: number, status: ProjectStatus, csrfToken: string, signal?: AbortSignal,
): Promise<ProjectRecord> {
  const version = projectVersion(expected)
  const action = status === 'ARCHIVED' ? 'archive' : 'unarchive'
  const project = sameProjectVersion(await callProject(
    `${projectPath(projectId)}/${action}`, projectMutation(version, csrfToken, signal),
  ), projectId, expected)
  if (project.status !== status) throw new Error('Project response did not match its requested status')
  return project
}

/** ADMIN が Project membership 一覧を取得する。 */
export async function loadProjectMembers(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectMemberRecord[]> {
  signal?.throwIfAborted()
  const value = await requestApiJson(projectMembersPath(projectId), { signal, cache: 'no-store' })
  signal?.throwIfAborted()
  if (!isRecord(value) || !exactFields(value, ['items'])) {
    throw new Error('Project member list response did not match its contract')
  }
  const members = parseItemList(
    value,
    'items',
    isProjectMember,
    'Project member list response did not match its contract',
  )
  if (new Set(members.map((member) => member.user_id.toLowerCase())).size !== members.length) {
    throw new Error('Project member list contained duplicate identities')
  }
  return members
}

/** ADMIN が ACTIVE User を Project member に追加する。 */
export async function addProjectMember(
  projectId: string,
  userId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectMemberRecord> {
  signal?.throwIfAborted()
  const member = parseProjectMember(await requestApiJson(
    projectMembersPath(projectId, userId),
    { method: 'PUT', headers: { 'X-CSRF-Token': csrfToken }, signal, cache: 'no-store' },
  ))
  signal?.throwIfAborted()
  if (member.user_id.toLowerCase() !== userId.toLowerCase() || member.status !== 'ACTIVE') {
    throw new Error('Project member response did not match the requested addition')
  }
  return member
}

/** ADMIN が Project membership を REMOVED にする。 */
export async function removeProjectMember(
  projectId: string,
  userId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<void> {
  signal?.throwIfAborted()
  await requestApiEmpty(
    projectMembersPath(projectId, userId),
    { method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken }, signal, cache: 'no-store' },
    204,
  )
  signal?.throwIfAborted()
}

/** Project/User の精確な UUID 以外は HTTP を開始する前に拒否する。 */
function projectMembersPath(projectId: string, userId?: string): string {
  if (!isUuid(projectId) || userId !== undefined && !isUuid(userId)) {
    throw new Error('Invalid project member identity')
  }
  const member = userId === undefined ? '' : `/${encodeURIComponent(userId)}`
  return `${API_BASE}/projects/${encodeURIComponent(projectId)}/members${member}`
}

/** Unknown JSON を Project response へ制限する。 */
function parseProject(value: unknown): ProjectRecord {
  if (!isProject(value)) throw new Error('Project response did not match its contract')
  return value
}

/** Unknown object が Project response の全公開 field を持つことを検証する。 */
function isProject(value: unknown): value is ProjectRecord {
  return isRecord(value)
    && exactFields(value, [
      'project_id', 'key', 'name', 'description', 'status', 'settings',
      'retention_days', 'row_version', 'created_at', 'updated_at',
    ])
    && hasStrings(value, [
      'project_id', 'key', 'name', 'description', 'status', 'created_at', 'updated_at',
    ])
    && isUuid(value.project_id)
    && typeof value.key === 'string' && [...value.key].length >= 1 && [...value.key].length <= 100
    && typeof value.name === 'string' && [...value.name].length >= 1 && [...value.name].length <= 200
    && typeof value.description === 'string' && [...value.description].length <= 4000
    && typeof value.status === 'string'
    && ['ACTIVE', 'ARCHIVED'].includes(value.status)
    && isRecord(value.settings)
    && typeof value.retention_days === 'number'
    && Number.isInteger(value.retention_days)
    && value.retention_days >= 1 && value.retention_days <= 3650
    && isProjectVersion(value.row_version)
    && isApiTimestamp(value.created_at) && isApiTimestamp(value.updated_at)
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
    && exactFields(value, ['user_id', 'email', 'display_name', 'status', 'joined_at'])
    && hasStrings(value, ['user_id', 'email', 'display_name', 'status', 'joined_at'])
    && isUuid(value.user_id)
    && typeof value.email === 'string' && [...value.email].length <= 320
    && /^[^@\s]+@[^@\s]+$/u.test(value.email)
    && typeof value.display_name === 'string'
    && [...value.display_name].length >= 1 && [...value.display_name].length <= 200
    && isApiTimestamp(value.joined_at)
    && typeof value.status === 'string'
    && ['ACTIVE', 'REMOVED'].includes(value.status)
}
