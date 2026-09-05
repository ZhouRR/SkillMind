import { API_BASE, hasStrings, isRecord, isStringArray, requestApiJson } from './http'
import { RUN_STATUSES, type RunStatus } from './runs'

/** task catalog に同梱される最新 Run の要約。詳細は Run API が返す。 */
export interface TaskLastRunRecord {
  run_id: string
  status: RunStatus
  created_at: string
  finished_at: string | null
  result_summary: string | null
}

/** Task が要求する Tool capability と必須性。 */
export interface TaskToolRequirementRecord {
  capability: string
  required: boolean
}

/** 資源要求へ束縛できる Project 内候補。Secret や接続情報は含まない。 */
export interface ResourceCandidateRecord {
  key: string
  kind: string
  provider: string
  label: string
}

/** 一つの資源要求の束縛状態と不足理由。 */
export interface RequirementBindingRecord {
  key: string
  kind: string
  required: boolean
  access: string
  status: 'AVAILABLE' | 'UNAVAILABLE' | 'UNSUPPORTED'
  reason: string
  capabilities: string[]
  selection_guidance: string | null
  candidates: ResourceCandidateRecord[]
}

/** Task が今この Project で到達できる実行段階と、その逐項根拠。 */
export interface TaskReadinessRecord {
  level: 'GUIDANCE_ONLY' | 'CONFIGURATION_REQUIRED' | 'RUNNABLE' | 'ACTIONABLE'
  requirements: RequirementBindingRecord[]
}

/** PUBLISHED SkillVersion から投影した、精確 version 束縛の実行可能 task descriptor。 */
export interface PublishedTaskRecord {
  skill_id: string
  skill_version_id: string
  skill_key: string
  skill_name: string
  version: string
  task_key: string
  /** Run 側と同じ決定的 ID。Run 履歴との突き合わせはこの値で行う（自前で導出しない）。 */
  task_id: string
  capability: string
  title: string
  task_type: string
  input_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
  input_schema_checksum: string
  output_schema_checksum: string
  task_output_schema: Record<string, unknown> | null
  task_output_schema_checksum: string | null
  workflow: string
  view: string
  default_view: string
  compatibility_level: string
  tool_requirements: TaskToolRequirementRecord[]
  published_at: string | null
  /** Catalog 未配線の環境では判定不能。資源が無いと断定せず null になる。 */
  readiness: TaskReadinessRecord | null
  /** この task の最新 Run。一度も実行されていなければ null（server 側で突き合わせ済み）。 */
  last_run: TaskLastRunRecord | null
}

/** Project 内で発見可能な実行可能 task の一覧。 */
export interface TaskCatalogRecord {
  tasks: PublishedTaskRecord[]
}

/** Project 所有の PUBLISHED SkillVersion から実行可能 task catalog を取得する。 */
export async function loadProjectTasks(
  projectId: string,
  signal?: AbortSignal,
): Promise<TaskCatalogRecord> {
  return parseTaskCatalog(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/tasks`,
    { signal },
  ))
}

/** Unknown JSON を task catalog の公開 contract へ制限する。 */
function parseTaskCatalog(value: unknown): TaskCatalogRecord {
  if (!isRecord(value) || !Array.isArray(value.tasks) || !value.tasks.every(isPublishedTask)) {
    throw new Error('Task catalog response did not match its contract')
  }
  return value as unknown as TaskCatalogRecord
}

/** Unknown object が精確 version 束縛の task descriptor shape を満たすか確認する。 */
function isPublishedTask(value: unknown): value is PublishedTaskRecord {
  return isRecord(value)
    && hasStrings(value, [
      'skill_id', 'skill_version_id', 'skill_key', 'skill_name', 'version',
      'task_key', 'task_id', 'capability', 'title', 'task_type',
      'workflow', 'view', 'default_view', 'compatibility_level',
    ])
    && (typeof value.published_at === 'string' || value.published_at === null)
    && schemaPairIsValid(value.input_schema, value.input_schema_checksum)
    && schemaPairIsValid(value.output_schema, value.output_schema_checksum)
    && optionalSchemaPairIsValid(value.task_output_schema, value.task_output_schema_checksum)
    && Array.isArray(value.tool_requirements)
    && value.tool_requirements.every(isToolRequirement)
    && isReadiness(value.readiness)
    && isLastRun(value.last_run)
}

/** 最新 Run の要約を実行時に検証する。未実行は null。 */
function isLastRun(value: unknown): value is TaskLastRunRecord | null {
  if (value === null) return true
  return isRecord(value)
    && hasStrings(value, ['run_id', 'created_at'])
    && typeof value.status === 'string'
    && RUN_STATUSES.has(value.status)
}

/** 任意 task-specific Schema は本文と checksum が同時に null または同時に有効であることを検証する。 */
function optionalSchemaPairIsValid(schema: unknown, checksum: unknown): boolean {
  return (schema === null && checksum === null)
    || (isRecord(schema) && typeof checksum === 'string' && /^sha256:[a-f0-9]{64}$/.test(checksum))
}

/** 就緒度は未判定 (null) を許すが、返る場合は逐項根拠まで揃っていることを求める。 */
function isReadiness(value: unknown): value is TaskReadinessRecord | null {
  if (value === null) return true
  return isRecord(value)
    && typeof value.level === 'string'
    && Array.isArray(value.requirements)
    && value.requirements.every(isRequirementBinding)
}

/** Unknown object が資源要求の束縛状態 shape を満たすか確認する。 */
function isRequirementBinding(value: unknown): value is RequirementBindingRecord {
  return isRecord(value)
    && hasStrings(value, ['key', 'kind', 'access', 'status', 'reason'])
    && typeof value.required === 'boolean'
    && isStringArray(value.capabilities)
    && (typeof value.selection_guidance === 'string' || value.selection_guidance === null)
    && Array.isArray(value.candidates)
    && value.candidates.every(
      (item) => isRecord(item) && hasStrings(item, ['key', 'kind', 'provider', 'label']),
    )
}

/** Generated Schema は SHA-256 checksum と対になった場合だけ受理する。 */
function schemaPairIsValid(schema: unknown, checksum: unknown): boolean {
  return isRecord(schema)
    && typeof checksum === 'string'
    && /^sha256:[a-f0-9]{64}$/.test(checksum)
}

/** Unknown object が Tool requirement shape を満たすか確認する。 */
function isToolRequirement(value: unknown): value is TaskToolRequirementRecord {
  return isRecord(value)
    && hasStrings(value, ['capability'])
    && typeof value.required === 'boolean'
}
