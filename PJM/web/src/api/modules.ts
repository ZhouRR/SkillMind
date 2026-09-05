import {
  API_BASE,
  hasStrings,
  isRecord,
  parseItemList,
  requestApiEmpty,
  requestApiJson,
} from './http'

/** Module に束縛された PUBLISHED SkillVersion の公開投影。 */
export interface ModuleSkillRecord {
  skill_version_id: string
  skill_id: string
  skill_key: string
  skill_name: string
  version: string
  sort_order: number
}

/** Project で有効な module(SkillComposition)の公開 record。 */
export interface ProjectModuleRecord {
  module_id: string
  project_id: string
  name: string
  description: string
  skills: ModuleSkillRecord[]
  created_at: string
  updated_at: string
}

/** Module 作成/更新の入力。束縛は PUBLISHED SkillVersion の ID 列。 */
export interface ModuleWriteInput {
  name: string
  description: string
  skill_version_ids: string[]
}

/** Project で有効な module の一覧を取得する。 */
export async function loadProjectModules(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectModuleRecord[]> {
  const value = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/modules`,
    { signal },
  )
  return parseItemList(
    value,
    'modules',
    isModule,
    'Module list response did not match its contract',
  )
}

/** ADMIN の CSRF token 付きで module を作成し、現在 Project へ有効化する。 */
export async function createProjectModule(
  projectId: string,
  input: ModuleWriteInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectModuleRecord> {
  return parseModule(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/modules`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify(input),
      signal,
    },
  ))
}

/** ADMIN の CSRF token 付きで module の名称/説明/束縛集合を置き換える。 */
export async function updateProjectModule(
  projectId: string,
  moduleId: string,
  input: ModuleWriteInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ProjectModuleRecord> {
  return parseModule(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/modules/${encodeURIComponent(moduleId)}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify(input),
      signal,
    },
  ))
}

/** ADMIN の CSRF token 付きで Project の module を削除する。 */
export async function deleteProjectModule(
  projectId: string,
  moduleId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<void> {
  await requestApiEmpty(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/modules/${encodeURIComponent(moduleId)}`,
    { method: 'DELETE', headers: { 'X-CSRF-Token': csrfToken }, signal },
  )
}

/** Unknown JSON を単一 module record の公開 contract へ制限する。 */
function parseModule(value: unknown): ProjectModuleRecord {
  if (!isModule(value)) throw new Error('Module response did not match its contract')
  return value
}

/** Unknown object が module record の公開 field を満たすか確認する。 */
function isModule(value: unknown): value is ProjectModuleRecord {
  return isRecord(value)
    && hasStrings(value, ['module_id', 'project_id', 'name', 'description', 'created_at', 'updated_at'])
    && Array.isArray(value.skills)
    && value.skills.every(isModuleSkill)
}

/** Unknown object が束縛 Skill の公開 field を満たすか確認する。 */
function isModuleSkill(value: unknown): value is ModuleSkillRecord {
  return isRecord(value)
    && hasStrings(value, ['skill_version_id', 'skill_id', 'skill_key', 'skill_name', 'version'])
    && typeof value.sort_order === 'number'
}
