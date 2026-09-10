import { API_BASE, isRecord, requestApiJson } from './http'
import { isNonNilUuid, sameUuid } from '../lib/validation'
import { compareJsonNumeric, isJsonIntegerToken, isJsonNumeric, jsonNumericKey, parseLosslessJson } from '../lib/losslessJson'
import type { TaskReadinessRecord } from './tasks'

/** Catalog の精確 identity だけを照合に使い、同名 task や latest へ置換しない。 */
export interface TaskFlowPreviewTarget {
  skill_id: string; skill_version_id: string; task_id: string; task_key: string
  skill_key: string; version: string
}
/** 元 Blueprint の短い規則。文言を翻訳・強制化せず保持する。 */
export interface TaskFlowNote { key: string; text: string }
/** 予想交付物は保存済み Artifact ではなく、download URL を持たない。 */
export interface TaskFlowDeliverable { key: string; kind: 'report' | 'structured_data' | 'patch' | 'change_proposal' | 'artifact'; description: string }
/** 元 Task の省略 field は省略のまま保持する。 */
export interface TaskFlowTask {
  key: string; capability: string; objective: string; success_criteria?: TaskFlowNote[]
  resource_keys?: string[]; deliverables?: TaskFlowDeliverable[]
  parameter_contract?: Record<string, unknown>; result_contract?: Record<string, unknown>
}
/** Skill/Task の宣言要求。現在の binding 候補とは別の計画である。 */
export interface TaskFlowResource {
  key: string; kind: 'issue' | 'repository' | 'document' | 'file' | 'knowledge' | 'other'
  required: boolean; access: 'read' | 'write'; capabilities?: string[]
  accepted_providers?: string[]; selection_guidance?: string
}
/** 原参照と範囲を伴う表示単位。将来の実行 node ID ではない。 */
export interface TaskFlowResourceReference { scope: 'TASK' | 'SKILL'; blueprint_ref: string; value: TaskFlowResource }
/** 条件付きの人手関与は、現在の待機や承認状態を表さない。 */
export interface TaskFlowInteraction { key: string; type: 'CLARIFICATION' | 'CHOICE' | 'REVIEW' | 'EFFECT_APPROVAL'; condition: string; prompt?: string }
/** 効果意図と実際の権限/実行/検証済み結果を混同しない。 */
export interface TaskFlowEffect { key: string; mode: 'observe' | 'propose' | 'apply'; operation: string; risk: 'low' | 'medium' | 'high'; resource_key?: string; approval_mode?: 'ask' }
/** 元 Skill 全体の規則。選択 Task だけへの追加割当ては行わない。 */
export interface TaskFlowShared {
  scope: 'SKILL'; resource_requirements: TaskFlowResourceReference[]
  guidance: Partial<Record<'required_rules' | 'recommended_steps' | 'quality_criteria' | 'prohibited_actions', TaskFlowNote[]>> | null
  interaction_points: TaskFlowInteraction[] | null; effect_intents: TaskFlowEffect[] | null
  execution_preferences: { recommended_profile?: 'GUIDED' | 'SUPERVISED' | 'DELEGATED'; session_split_hints?: TaskFlowNote[]; stop_conditions?: TaskFlowNote[] } | null
  assumptions: TaskFlowNote[] | null; questions: Array<TaskFlowNote & { required: boolean }> | null
}
/** API が返した単 Task と明示された Skill 共通領域だけを描画する。 */
export interface TaskFlowPlan { task: { scope: 'TASK'; blueprint_ref: string; value: TaskFlowTask }; task_resources: TaskFlowResourceReference[]; shared: TaskFlowShared }
/** 原 file/行の照合範囲。意味の正しさや今の blob 可読性の証明ではない。 */
export interface TaskFlowSourceTrace { target: string; path: string; line: number | null; reason: string; verification: 'SOURCE_INDEX' | 'TEXT_SNAPSHOT' }
/** Server の意味 identity と独立した現在の Skill 全体の準備評価。 */
export interface TaskFlowPreviewRecord {
  preview_version: 'skillmind.task-flow-preview/v1'
  identity: TaskFlowPreviewTarget & { project_id: string; manifest_checksum: string }
  status: 'AVAILABLE' | 'NOT_DECLARED'; blueprint_checksum: string | null; preview_checksum: string
  plan: TaskFlowPlan | null; source_traces: TaskFlowSourceTrace[]
  readiness: { scope: 'SKILL_BLUEPRINT'; assessment: TaskReadinessRecord | null }
}

const KEY = /^[a-z][a-z0-9_.-]*$/
const HASH = /^sha256:[a-f0-9]{64}$/
const VALUE_TYPES = ['object', 'array', 'string', 'integer', 'number', 'boolean'] as const
/** 元の語彙を保持し、UTF-8 に存在しない孤立 surrogate だけを拒否する。 */
function text(value: unknown, maximum: number, minimum = 1): value is string {
  return typeof value === 'string' && [...value].length >= minimum && [...value].length <= maximum
    && !/[\ud800-\udfff]/u.test(value)
}
/** 公開 enum は未知文字列を success にフォールバックしない。 */
function member<T extends string>(value: unknown, values: readonly T[]): value is T { return typeof value === 'string' && values.includes(value as T) }
/** required/optional の双方を列挙し、未知 field の黙認を避ける。 */
function fields(value: unknown, required: readonly string[], optional: readonly string[] = []): value is Record<string, unknown> {
  return isRecord(value) && required.every((key) => Object.hasOwn(value, key))
    && Object.keys(value).every((key) => required.includes(key) || optional.includes(key))
}
/** 元契約の array 上限を守り、並び順は変更しない。 */
function list<T>(value: unknown, maximum: number, guard: (item: unknown) => item is T): value is T[] {
  return Array.isArray(value) && value.length <= maximum && value.every(guard)
}
/** 同じ collection の key は一意であり、表示 key を配列位置で補造しない。 */
function keyed<T extends { key: string }>(value: unknown, maximum: number, guard: (item: unknown) => item is T): value is T[] {
  return list(value, maximum, guard) && new Set(value.map((item) => item.key)).size === value.length
}
/** Blueprint の key 語彙を原まま受け入れる。 */
function key(value: unknown): value is string { return text(value, 128) && KEY.test(value) }
/** optional は欠落だけを許し、null/壊れた値を空宣言へ変えない。 */
function optional(value: Record<string, unknown>, name: string, valid: (item: unknown) => boolean): boolean { return !Object.hasOwn(value, name) || valid(value[name]) }
/** キー集合に意味を持つ array の重複を拒否する。 */
function keys(value: unknown, maximum = 50): value is string[] { return list(value, maximum, key) && new Set(value).size === value.length }
/** Rule 自体の required/recommended は格納された区分だけから読む。 */
function note(value: unknown): value is TaskFlowNote { return fields(value, ['key', 'text']) && key(value.key) && text(value.text, 1000) }
/** 原 scalar type を守り、integer に float/bool を紛れ込ませない。 */
function enumValue(value: unknown, kind: typeof VALUE_TYPES[number]): boolean {
  if (kind === 'string') return text(value, Number.MAX_SAFE_INTEGER, 0)
  if (kind === 'boolean') return typeof value === 'boolean'
  if (kind === 'integer') return isJsonIntegerToken(value)
  if (kind === 'number') return isJsonNumeric(value)
  return false
}
/** 入れ子契約も Blueprint の公開文法で検証し、表示用の弱い形へ cast しない。 */
function contract(value: unknown, kind: 'root' | 'field' | 'item' = 'root', depth = 1, state = { fields: 0 }): value is Record<string, unknown> {
  if (depth > 5 || (kind === 'field' && ++state.fields > 100)) return false
  const required = kind === 'root' ? ['contract_version', 'type'] : kind === 'field' ? ['key', 'type', 'required'] : ['type']
  if (!fields(value, required, ['description', 'enum', 'fields', 'items', 'min_length', 'max_length', 'pattern', 'minimum', 'maximum'])
    || !member(value.type, VALUE_TYPES)) return false
  if (kind === 'root' && value.contract_version !== 'skillmind.task-contract-draft/v1') return false
  if (kind === 'field' && !(typeof value.key === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(value.key) && typeof value.required === 'boolean')) return false
  if ((Object.hasOwn(value, 'fields') && value.type !== 'object') || (Object.hasOwn(value, 'items') && value.type !== 'array')
    || (value.type === 'array' && !Object.hasOwn(value, 'items'))
    || (['min_length', 'max_length', 'pattern'].some((name) => Object.hasOwn(value, name)) && value.type !== 'string')
    || (['minimum', 'maximum'].some((name) => Object.hasOwn(value, name)) && !['integer', 'number'].includes(value.type))) return false
  const valueType = value.type
  return optional(value, 'description', (item) => text(item, 500))
    && optional(value, 'pattern', (item) => text(item, 128))
    && ['minimum', 'maximum'].every((name) => optional(value, name, isJsonNumeric))
    && (!Object.hasOwn(value, 'minimum') || !Object.hasOwn(value, 'maximum') || compareJsonNumeric(value.minimum, value.maximum) <= 0)
    && ['min_length', 'max_length'].every((name) => optional(value, name, (item) => Number.isSafeInteger(item) && Number(item) >= 0 && Number(item) <= 100000))
    && (!Object.hasOwn(value, 'min_length') || !Object.hasOwn(value, 'max_length') || Number(value.min_length) <= Number(value.max_length))
    && optional(value, 'fields', (item) => keyed(item, 100, (field): field is Record<string, unknown> & { key: string } => contract(field, 'field', depth + 1, state)))
    && optional(value, 'items', (item) => contract(item, 'item', depth + 1, state))
    && optional(value, 'enum', (item) => Array.isArray(item) && item.length >= 1 && item.length <= 50
      && item.every((entry) => enumValue(entry, valueType))
      && new Set(item.map((entry) => isJsonNumeric(entry) ? jsonNumericKey(entry) : JSON.stringify(entry))).size === item.length)
}
/** 原 Task に後付けの成功や任意 action を混ぜない。 */
function task(value: unknown): value is TaskFlowTask {
  return fields(value, ['key', 'capability', 'objective'], ['success_criteria', 'resource_keys', 'deliverables', 'parameter_contract', 'result_contract'])
    && key(value.key) && key(value.capability) && text(value.objective, 1000)
    && optional(value, 'resource_keys', keys)
    && optional(value, 'success_criteria', (item) => keyed(item, 50, note))
    && optional(value, 'deliverables', (item) => keyed(item, 50, (entry): entry is TaskFlowDeliverable => fields(entry, ['key', 'kind', 'description'])
      && key(entry.key) && member(entry.kind, ['report', 'structured_data', 'patch', 'change_proposal', 'artifact']) && text(entry.description, 1000)))
    && optional(value, 'parameter_contract', contract) && optional(value, 'result_contract', contract)
}
/** 候補や Provider は計画へ足さず、元 resource 宣言の field だけを許す。 */
function resource(value: unknown): value is TaskFlowResource {
  return fields(value, ['key', 'kind', 'required', 'access'], ['capabilities', 'accepted_providers', 'selection_guidance'])
    && key(value.key) && member(value.kind, ['issue', 'repository', 'document', 'file', 'knowledge', 'other'])
    && typeof value.required === 'boolean' && member(value.access, ['read', 'write'])
    && optional(value, 'accepted_providers', (item) => keys(item, 20))
    && optional(value, 'capabilities', (item) => list(item, 20, (entry): entry is string => typeof entry === 'string' && /^[a-z][a-z0-9_.-]*\/v[1-9][0-9]*$/.test(entry)) && new Set(item as string[]).size === (item as string[]).length)
    && optional(value, 'selection_guidance', (item) => text(item, 1000))
}
/** Blueprint 内の正規 array index だけを原参照として受け取る。 */
function reference(value: unknown, name: string): value is string { return typeof value === 'string' && new RegExp(`^/${name}/(?:0|[1-9][0-9]*)$`).test(value) && Number(value.split('/')[2]) < 50 }
/** scope をその場で推測せず、Server が分離した領域と一致させる。 */
function resourceRef(value: unknown, scope: 'TASK' | 'SKILL'): value is TaskFlowResourceReference {
  return fields(value, ['scope', 'blueprint_ref', 'value']) && value.scope === scope
    && reference(value.blueprint_ref, 'resource_requirements') && resource(value.value)
}
/** Task 外の全 Skill 宣言は nullable と空宣言を区別して検証する。 */
function shared(value: unknown): value is TaskFlowShared {
  if (!fields(value, ['scope', 'resource_requirements', 'guidance', 'interaction_points', 'effect_intents', 'execution_preferences', 'assumptions', 'questions']) || value.scope !== 'SKILL') return false
  return list(value.resource_requirements, 50, (item): item is TaskFlowResourceReference => resourceRef(item, 'SKILL'))
    && (value.guidance === null || (fields(value.guidance, [], ['required_rules', 'recommended_steps', 'quality_criteria', 'prohibited_actions'])
      && Object.values(value.guidance).every((item) => keyed(item, 100, note))))
    && (value.interaction_points === null || keyed(value.interaction_points, 50, (item): item is TaskFlowInteraction => fields(item, ['key', 'type', 'condition'], ['prompt'])
      && key(item.key) && member(item.type, ['CLARIFICATION', 'CHOICE', 'REVIEW', 'EFFECT_APPROVAL']) && text(item.condition, 1000) && optional(item, 'prompt', (entry) => text(entry, 1000))))
    && (value.effect_intents === null || keyed(value.effect_intents, 50, (item): item is TaskFlowEffect => fields(item, ['key', 'mode', 'operation', 'risk'], ['resource_key', 'approval_mode'])
      && key(item.key) && member(item.mode, ['observe', 'propose', 'apply']) && text(item.operation, 1000) && member(item.risk, ['low', 'medium', 'high'])
      && optional(item, 'resource_key', key) && optional(item, 'approval_mode', (entry) => entry === 'ask')))
    && (value.assumptions === null || keyed(value.assumptions, 100, note))
    && (value.questions === null || keyed(value.questions, 50, (item): item is TaskFlowNote & { required: boolean } => fields(item, ['key', 'text', 'required']) && key(item.key) && text(item.text, 1000) && typeof item.required === 'boolean'))
    && (value.execution_preferences === null || (fields(value.execution_preferences, [], ['recommended_profile', 'session_split_hints', 'stop_conditions'])
      && optional(value.execution_preferences, 'recommended_profile', (item) => member(item, ['GUIDED', 'SUPERVISED', 'DELEGATED']))
      && ['session_split_hints', 'stop_conditions'].every((name) => optional(value.execution_preferences as Record<string, unknown>, name, (item) => keyed(item, 100, note)))))
}
/** 分割が原 resource_keys と対応し、重複/別 scope を混ぜていないことを確認する。 */
function plan(value: unknown, target: TaskFlowPreviewTarget): value is TaskFlowPlan {
  if (!fields(value, ['task', 'task_resources', 'shared']) || !fields(value.task, ['scope', 'blueprint_ref', 'value'])
    || value.task.scope !== 'TASK' || !reference(value.task.blueprint_ref, 'tasks') || !task(value.task.value)
    || value.task.value.key !== target.task_key || !shared(value.shared)
    || !list(value.task_resources, 50, (item): item is TaskFlowResourceReference => resourceRef(item, 'TASK'))) return false
  const selected = value.task_resources
  const expected = value.task.value.resource_keys ?? []
  const all = [...selected, ...value.shared.resource_requirements]
  const sharedIndices = value.shared.resource_requirements.map((item) => Number(item.blueprint_ref.split('/')[2]))
  return selected.length === expected.length && selected.every((item, index) => item.value.key === expected[index])
    && all.length <= 50 && new Set(all.map((item) => item.value.key)).size === all.length
    && new Set(all.map((item) => item.blueprint_ref)).size === all.length
    && all.map((item) => Number(item.blueprint_ref.split('/')[2])).sort((left, right) => left - right).every((index, position) => index === position)
    && sharedIndices.every((index, position) => position === 0 || index > sharedIndices[position - 1]!)
    && (value.shared.effect_intents ?? []).every((item) => item.resource_key === undefined || all.some((entry) => entry.value.key === item.resource_key))
}
/** Source path は navigation/HTML として使わず、安全な原相対 path として表示する。 */
function trace(value: unknown): value is TaskFlowSourceTrace {
  return fields(value, ['target', 'path', 'line', 'reason', 'verification'])
    && text(value.target, 512) && value.target.startsWith('/') && !/~(?:[^01]|$)/.test(value.target)
    && text(value.path, 1024) && !/[\\:\u0000-\u001f\u007f]/.test(value.path)
    && value.path.split('/').every((part) => part !== '' && part !== '.' && part !== '..')
    && (value.line === null || Number.isSafeInteger(value.line) && Number(value.line) >= 1)
    && text(value.reason, 1000) && member(value.verification, ['SOURCE_INDEX', 'TEXT_SNAPSHOT'])
    && (value.verification !== 'SOURCE_INDEX' || value.line === null)
}
/** readiness は原意味 hash の一部ではなく、現在の Skill 全体評価として検証する。 */
function readiness(value: unknown): value is TaskFlowPreviewRecord['readiness'] {
  if (!fields(value, ['scope', 'assessment']) || value.scope !== 'SKILL_BLUEPRINT') return false
  const current = value.assessment
  return current === null || fields(current, ['level', 'requirements'])
    && member(current.level, ['GUIDANCE_ONLY', 'CONFIGURATION_REQUIRED', 'RUNNABLE', 'ACTIONABLE'])
    && keyed(current.requirements, 50, (item): item is TaskReadinessRecord['requirements'][number] => fields(item,
      ['key', 'kind', 'required', 'access', 'status', 'reason', 'capabilities', 'selection_guidance', 'candidates'])
      && key(item.key) && member(item.kind, ['issue', 'repository', 'document', 'file', 'knowledge', 'other'])
      && typeof item.required === 'boolean' && member(item.access, ['read', 'write'])
      && member(item.status, ['AVAILABLE', 'UNAVAILABLE', 'UNSUPPORTED']) && text(item.reason, Number.MAX_SAFE_INTEGER)
      && list(item.capabilities, 20, (entry): entry is string => typeof entry === 'string' && /^[a-z][a-z0-9_.-]*\/v[1-9][0-9]*$/.test(entry))
      && (item.selection_guidance === null || text(item.selection_guidance, 1000))
      && Array.isArray(item.candidates) && item.candidates.every((entry) => fields(entry, ['key', 'kind', 'provider', 'label'])
        && ['key', 'kind', 'provider', 'label'].every((name) => text(entry[name], Number.MAX_SAFE_INTEGER))))
}
/** 現在評価が別 Blueprint の要求を返していないか、全要求の保存宣言と比較する。 */
function readinessMatches(value: TaskFlowPreviewRecord): boolean {
  const current = value.readiness.assessment
  if (!current) return true
  if (!value.plan) return false
  const resources = [...value.plan.task_resources, ...value.plan.shared.resource_requirements].map((item) => item.value)
  return current.requirements.length === resources.length && current.requirements.every((requirement) => {
    const declared = resources.find((item) => item.key === requirement.key)
    return declared !== undefined && requirement.kind === declared.kind && requirement.required === declared.required
      && requirement.access === declared.access && requirement.capabilities.length === (declared.capabilities ?? []).length
      && requirement.selection_guidance === (declared.selection_guidance ?? null)
      && requirement.capabilities.every((capability) => (declared.capabilities ?? []).includes(capability))
      && new Set(requirement.capabilities).size === requirement.capabilities.length
  })
}
/** Server で生成する task_id は再生成せず、ユーザーが選んだ catalog の原 ID と比較する。 */
function identity(value: unknown, projectId: string, target: TaskFlowPreviewTarget): boolean {
  if (!fields(value, ['project_id', 'skill_id', 'skill_version_id', 'task_id', 'task_key', 'skill_key', 'version', 'manifest_checksum'])) return false
  return isNonNilUuid(value.project_id) && sameUuid(value.project_id, projectId)
    && (['skill_id', 'skill_version_id', 'task_id'] as const).every((name) => isNonNilUuid(value[name]) && sameUuid(value[name], target[name]))
    && (['task_key', 'skill_key', 'version'] as const).every((name) => value[name] === target[name])
    && typeof value.manifest_checksum === 'string' && HASH.test(value.manifest_checksum)
}
/** Client 境界の全検証。checksum は Server identity であり Python canonical JSON を再実装しない。 */
export function parseTaskFlowPreview(value: unknown, projectId: string, target: TaskFlowPreviewTarget): TaskFlowPreviewRecord {
  if (!fields(value, ['preview_version', 'identity', 'status', 'blueprint_checksum', 'preview_checksum', 'plan', 'source_traces', 'readiness'])
    || value.preview_version !== 'skillmind.task-flow-preview/v1'
    || !isNonNilUuid(projectId) || !isValidTaskFlowTarget(target)
    || !identity(value.identity, projectId, target)
    || typeof value.preview_checksum !== 'string' || !HASH.test(value.preview_checksum)
    || !list(value.source_traces, 500, trace) || !readiness(value.readiness)
    || !(value.status === 'NOT_DECLARED' ? value.plan === null && value.blueprint_checksum === null && value.source_traces.length === 0 && value.readiness.assessment === null
      : value.status === 'AVAILABLE' && value.source_traces.length > 0 && typeof value.blueprint_checksum === 'string' && HASH.test(value.blueprint_checksum) && plan(value.plan, target))) {
    throw new Error('Task flow preview response did not match its contract')
  }
  const record = value as unknown as TaskFlowPreviewRecord
  if (!readinessMatches(record)) throw new Error('Task flow readiness does not match original declarations')
  return record
}
/** 壊れた旧 catalog から新しい読取 target を補造しない。 */
export function isValidTaskFlowTarget(value: TaskFlowPreviewTarget): boolean {
  return [value.skill_id, value.skill_version_id, value.task_id].every(isNonNilUuid)
    && key(value.task_key) && key(value.skill_key) && text(value.version, 32)
}
/** 明示された精確 Task の投影だけを GET し、Run 作成や再解釈は起動しない。 */
export async function loadTaskFlowPreview(projectId: string, target: TaskFlowPreviewTarget, signal?: AbortSignal): Promise<TaskFlowPreviewRecord> {
  if (!isNonNilUuid(projectId) || !isValidTaskFlowTarget(target)) throw new Error('Invalid task flow preview identity')
  let preview: TaskFlowPreviewRecord | null = null
  await requestApiJson(`${API_BASE}/projects/${encodeURIComponent(projectId)}/skill-versions/${encodeURIComponent(target.skill_version_id)}/tasks/${encodeURIComponent(target.task_key)}/flow-preview`, { signal }, {
    statuses: [200], decode: parseLosslessJson,
    validate: (value) => { preview = parseTaskFlowPreview(value, projectId, target) },
  })
  if (preview === null) throw new Error('Task flow preview was not validated')
  return preview
}
