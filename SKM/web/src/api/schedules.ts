import { API_BASE, exactFields, isRecord, requestApiJson } from './http'
import { isApiTimestamp, isUuid, sameUuid } from '../lib/validation'
import { captureScheduleDefinition, captureScheduleIntent, matchesScheduleIntent } from '../lib/scheduleIntent'

/** 発火形態。監視条件は計画 §22 D1 により作らない。 */
export type ScheduleKind = 'ONCE' | 'CRON'

/** TaskSchedule の lifecycle (計画 §22 D9)。 */
export type ScheduleStatus = 'ACTIVE' | 'PAUSED' | 'COMPLETED' | 'ERROR' | 'ARCHIVED'

/** 直近一回の到期処理の結末。 */
export type ScheduleOutcome =
  | 'RUN_CREATED'
  | 'SKIPPED_OVERLAP'
  | 'FAILED_PRECONDITION'
  | 'COMPLETED'

/** 発火時刻を決める部分。kind ごとに必須 field が変わる。 */
export interface ScheduleDefinitionInput {
  kind: ScheduleKind
  timezone: string
  cron_expression?: string | null
  run_at?: string | null
  end_at?: string | null
  max_runs?: number | null
}

/** schedule の公開 record。接続情報も Secret も含まない。 */
export interface ScheduleRecord {
  schedule_id: string
  project_id: string
  name: string
  kind: ScheduleKind
  status: ScheduleStatus
  timezone: string
  cron_expression: string | null
  run_at: string | null
  end_at: string | null
  max_runs: number | null
  skill_version_id: string
  task_key: string
  input: Record<string, unknown>
  sources: Record<string, string>
  next_run_at: string | null
  last_run_at: string | null
  last_run_id: string | null
  last_outcome: ScheduleOutcome | null
  last_error: string | null
  run_count: number
  missed_count: number
  created_by: string
  row_version: number
  created_at: string
  updated_at: string
}

/** schedule を新規作成する入力。 */
export interface CreateScheduleInput {
  name: string
  definition: ScheduleDefinitionInput
  skill_version_id: string
  task_key: string
  input?: Record<string, unknown>
  sources?: Record<string, string>
}

/** 既存 schedule の定義と凍結入力を差し替える入力。束縛 task は変更できない。 */
export interface UpdateScheduleInput {
  name: string
  definition: ScheduleDefinitionInput
  input?: Record<string, unknown>
  sources?: Record<string, string>
  expected_row_version: number
}

/** 検索は server に渡し、部分ページを全件の代わりにしない。 */
export interface ScheduleListOptions {
  q?: string
  status?: ScheduleStatus
  limit?: number
  offset?: number
}

/** 一覧の件数と位置を UI にそのまま引き継ぐ。 */
export interface SchedulePage {
  schedules: ScheduleRecord[]
  total: number
  limit: number
  offset: number
}

/** 認領済み原 occurrence の公開事実。内部 snapshot と実行権 credential は含めない。 */
export interface SchedulePendingOccurrence {
  occurrence_id: string
  occurrence_at: string
  configuration_version: number
  created_at: string
  updated_at: string
  attempt_count: number
  lease_expires_at: string
}

/** 一つの SQL 読取時点の在途投影。null は履歴や Run の不存在証明ではない。 */
export interface ScheduleActivity {
  schedule_id: string
  project_id: string
  row_version: number
  configuration_version: number
  tracking: 'TRACKED' | 'LEGACY_UNAVAILABLE'
  checked_at: string
  automatic_attempt_limit: number
  pending: SchedulePendingOccurrence | null
}

/** Schedule の版が変わらない lease 接管もあるため、毎回独立した非 cache GET を行う。 */
export async function loadScheduleActivity(
  projectId: string, scheduleId: string, signal?: AbortSignal,
): Promise<ScheduleActivity> {
  const path = `${schedulePath(projectId, scheduleId)}/activity`
  signal?.throwIfAborted()
  const value = await requestApiJson(path, { signal, cache: 'no-store' }, 200)
  signal?.throwIfAborted()
  if (!isRecord(value) || !exactFields(value, [
    'schedule_id', 'project_id', 'row_version', 'configuration_version', 'tracking',
    'checked_at', 'automatic_attempt_limit', 'pending',
  ]) || !isUuid(value.schedule_id) || !sameUuid(value.schedule_id, scheduleId)
    || !isUuid(value.project_id) || !sameUuid(value.project_id, projectId)
    || !isInteger(value.row_version, 1) || !isInteger(value.configuration_version, 1)
    || !isInteger(value.automatic_attempt_limit, 1) || !isActivityTimestamp(value.checked_at)
    || typeof value.tracking !== 'string' || !['TRACKED', 'LEGACY_UNAVAILABLE'].includes(value.tracking)
    || value.tracking === 'LEGACY_UNAVAILABLE' && value.pending !== null
    || value.pending !== null && (!isPendingOccurrence(value.pending)
      || value.pending.configuration_version > value.configuration_version)) {
    throw new Error('Schedule activity is invalid')
  }
  return value as unknown as ScheduleActivity
}

/** 未知状態・欠落を空に変えず、元の時刻と strict な回数だけを公開する。 */
function isPendingOccurrence(value: unknown): value is SchedulePendingOccurrence {
  return isRecord(value) && exactFields(value, [
    'occurrence_id', 'occurrence_at', 'configuration_version', 'created_at', 'updated_at',
    'attempt_count', 'lease_expires_at',
  ]) && isUuid(value.occurrence_id) && isInteger(value.configuration_version, 1)
    && isInteger(value.attempt_count, 1)
    && ['occurrence_at', 'created_at', 'updated_at', 'lease_expires_at'].every((key) => isActivityTimestamp(value[key]))
}

/** Python datetime の年範囲と offset 必須条件を、ブラウザの自動日付補正で広げない。 */
function isActivityTimestamp(value: unknown): value is string {
  return isApiTimestamp(value) && Number(value.slice(0, 4)) >= 1
}

/** Project の全状態を独立に列挙し、Task catalog に無い履歴も落とさない。 */
export async function loadSchedulePage(
  projectId: string,
  options: ScheduleListOptions = {},
  signal?: AbortSignal,
): Promise<SchedulePage> {
  const limit = options.limit ?? 20
  const offset = options.offset ?? 0
  const q = options.q?.trim() ?? ''
  const status = options.status
  if (!isInteger(limit, 1) || limit > 100 || !isInteger(offset, 0) || [...q].length > 200 || q.includes('\u0000')
    || status !== undefined && !SCHEDULE_STATUSES.has(status)) {
    throw new Error('Schedule query is invalid')
  }
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (q) query.set('q', q)
  if (status) query.set('status', status)
  signal?.throwIfAborted()
  const payload = await requestApiJson(`${schedulePath(projectId)}?${query}`, { signal, cache: 'no-store' }, 200)
  signal?.throwIfAborted()
  if (!isRecord(payload) || !exactFields(payload, ['schedules', 'total', 'limit', 'offset'])
    || !Array.isArray(payload.schedules) || !payload.schedules.every(isScheduleRecord)
    || !isInteger(payload.total, 0) || payload.limit !== limit || payload.offset !== offset
    || payload.schedules.length > limit || payload.schedules.length > Math.max(0, payload.total - offset)
    || payload.schedules.length === 0 && offset < payload.total
    || payload.schedules.some((item) => !sameUuid(item.project_id, projectId)
      || status !== undefined && item.status !== status)
    || new Set(payload.schedules.map((item) => item.schedule_id.toLowerCase())).size !== payload.schedules.length) {
    throw new Error('Schedule list is invalid')
  }
  return payload as unknown as SchedulePage
}

/** Task card の集約も全ページを読む。途中で一覧が変化したら不完全な集計を返さない。 */
export async function loadProjectSchedules(
  projectId: string,
  signal?: AbortSignal,
): Promise<ScheduleRecord[]> {
  const items: ScheduleRecord[] = []
  const identities = new Set<string>()
  let total: number | null = null
  do {
    const page = await loadSchedulePage(projectId, { limit: 100, offset: items.length }, signal)
    if (total !== null && total !== page.total) throw new Error('Schedule list changed while reading')
    total = page.total
    for (const item of page.schedules) {
      if (identities.has(item.schedule_id.toLowerCase())) throw new Error('Schedule list changed while reading')
      identities.add(item.schedule_id.toLowerCase())
      items.push(item)
    }
  } while (items.length < total)
  return items
}

/** 編集・照合では選択した原 Schedule の現在値だけを取得する。 */
export async function loadSchedule(
  projectId: string, scheduleId: string, signal?: AbortSignal,
): Promise<ScheduleRecord> {
  return sameSchedule(await callSchedule(schedulePath(projectId, scheduleId), { signal }), projectId, scheduleId)
}

/** 保存前に次回発火時刻を確認する。空配列は返らず、不正な定義は Problem になる。 */
export async function previewSchedule(
  projectId: string,
  definition: ScheduleDefinitionInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<string[]> {
  const frozen = captureScheduleDefinition(definition)
  signal?.throwIfAborted()
  const payload = await requestApiJson(
    `${schedulePath(projectId)}/preview`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ definition: frozen }),
      signal,
      cache: 'no-store',
    },
    200,
  )
  signal?.throwIfAborted()
  const occurrences: unknown = isRecord(payload) ? payload.occurrences : null
  if (!isRecord(payload) || Object.keys(payload).some((key) => key !== 'occurrences')
    || !Array.isArray(occurrences) || occurrences.length < 1 || occurrences.length > 5
    || !occurrences.every(isApiTimestamp)
    || occurrences.some((item, index) => index > 0 && Date.parse(item) <= Date.parse(occurrences[index - 1]!))
    || (frozen.kind === 'ONCE' && occurrences.length !== 1)) {
    throw new Error('Schedule preview is invalid')
  }
  return occurrences
}

/** schedule を新規作成する。 */
export async function createSchedule(
  projectId: string,
  input: CreateScheduleInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ScheduleRecord> {
  const frozen = captureScheduleIntent(input, 'create')
  const { skill_version_id: skillVersionId, task_key: taskKey } = frozen
  const record = sameSchedule(await callSchedule(schedulePath(projectId),
    mutation(frozen, csrfToken, signal), 201), projectId)
  if (!sameUuid(record.skill_version_id, skillVersionId) || record.task_key !== taskKey
    || record.row_version !== 1 || record.status !== 'ACTIVE' || !matchesScheduleIntent(record, frozen)) {
    throw new Error('Schedule response did not match its requested creation')
  }
  return record
}

/** schedule の定義と凍結入力を差し替える。 */
export async function updateSchedule(
  projectId: string,
  scheduleId: string,
  input: UpdateScheduleInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ScheduleRecord> {
  const frozen = captureScheduleIntent(input, 'update')
  const expectedVersion = frozen.expected_row_version
  requireVersion(expectedVersion)
  const record = sameSchedule(await callSchedule(schedulePath(projectId, scheduleId),
    mutation(frozen, csrfToken, signal, 'PUT')), projectId, scheduleId, expectedVersion)
  if (!matchesScheduleIntent(record, frozen)) throw new Error('Schedule response did not match its requested update')
  return record
}

/** 暂停・恢复・归档を適用する。遷移の可否は server の状態機が判定する。 */
export async function changeScheduleStatus(
  projectId: string,
  scheduleId: string,
  status: ScheduleStatus,
  expectedRowVersion: number,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ScheduleRecord> {
  requireVersion(expectedRowVersion)
  if (!SCHEDULE_STATUSES.has(status)) throw new Error('Schedule request is invalid')
  const record = sameSchedule(await callSchedule(`${schedulePath(projectId, scheduleId)}/status`,
    mutation({ status, expected_row_version: expectedRowVersion }, csrfToken, signal)),
  projectId, scheduleId, expectedRowVersion)
  if (record.status !== status) throw new Error('Schedule response did not match its requested status')
  return record
}

/** UUID だけを URL に置き、未検証の対象を path として解釈しない。 */
function schedulePath(projectId: string, scheduleId?: string): string {
  if (!isUuid(projectId) || scheduleId !== undefined && !isUuid(scheduleId)) throw new Error('Invalid schedule identity')
  return `${API_BASE}/projects/${encodeURIComponent(projectId)}/schedules${scheduleId ? `/${encodeURIComponent(scheduleId)}` : ''}`
}

/** 全単体 response の HTTP/abort 境界を共有する。失敗時に自動再送しない。 */
async function callSchedule(url: string, init: RequestInit, expectedStatus = 200): Promise<ScheduleRecord> {
  init.signal?.throwIfAborted()
  const value = await requestApiJson(url, { ...init, cache: 'no-store' }, expectedStatus)
  init.signal?.throwIfAborted()
  return parseSchedule(value)
}

/** CSRF と原要求 body を一回だけ送る。 */
function mutation(body: object, csrfToken: string, signal?: AbortSignal, method = 'POST'): RequestInit {
  return { method, signal, body: JSON.stringify(body), headers: {
    'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken,
  } }
}

/** 原 identity と CAS の成功を、形だけ正しい別資源で代用しない。 */
function sameSchedule(record: ScheduleRecord, projectId: string, scheduleId?: string, version?: number): ScheduleRecord {
  if (!sameUuid(record.project_id, projectId) || scheduleId !== undefined && !sameUuid(record.schedule_id, scheduleId)
    || version !== undefined && record.row_version !== version + 1) throw new Error('Schedule response did not match its requested identity or version')
  return record
}

/** 表示時の版を送信し、欠落を最新値で埋めない。 */
function requireVersion(value: number): void {
  if (!isInteger(value, 1)) throw new Error('Invalid schedule version')
}

/** 単体 response を型 guard 付きで解析する。 */
function parseSchedule(value: unknown): ScheduleRecord {
  if (!isScheduleRecord(value)) throw new Error('Schedule response is invalid')
  return value
}

const SCHEDULE_KINDS: ReadonlySet<string> = new Set(['ONCE', 'CRON'])
const SCHEDULE_STATUSES: ReadonlySet<string> = new Set([
  'ACTIVE',
  'PAUSED',
  'COMPLETED',
  'ERROR',
  'ARCHIVED',
])
const SCHEDULE_OUTCOMES: ReadonlySet<string> = new Set(['RUN_CREATED', 'SKIPPED_OVERLAP', 'FAILED_PRECONDITION', 'COMPLETED'])
const SCHEDULE_FIELDS = [
  'schedule_id', 'project_id', 'name', 'kind', 'status', 'timezone', 'cron_expression', 'run_at', 'end_at', 'max_runs',
  'skill_version_id', 'task_key', 'input', 'sources', 'next_run_at', 'last_run_at', 'last_run_id', 'last_outcome',
  'last_error', 'run_count', 'missed_count', 'created_by', 'row_version', 'created_at', 'updated_at',
]

/** JSON の有限整数だけを受け、bool/小数/unsafe number を件数や CAS に使わない。 */
function isInteger(value: unknown, minimum: number): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum
}

/** schedule record の契約を実行時に検証する。 */
function isScheduleRecord(value: unknown): value is ScheduleRecord {
  if (!isRecord(value) || !exactFields(value, SCHEDULE_FIELDS)) return false
  if (!['schedule_id', 'project_id', 'skill_version_id', 'created_by'].every((key) => isUuid(value[key]))) return false
  if (!['name', 'timezone', 'task_key'].every((key) => typeof value[key] === 'string' && value[key].length > 0)) return false
  if (typeof value.kind !== 'string' || !SCHEDULE_KINDS.has(value.kind)) return false
  if (typeof value.status !== 'string' || !SCHEDULE_STATUSES.has(value.status)) return false
  if (!isInteger(value.run_count, 0) || !isInteger(value.missed_count, 0) || !isInteger(value.row_version, 1)) return false
  if (value.max_runs !== null && !isInteger(value.max_runs, 1)) return false
  if (!['run_at', 'end_at', 'next_run_at', 'last_run_at'].every((key) => value[key] === null || isApiTimestamp(value[key]))) return false
  if (!isApiTimestamp(value.created_at) || !isApiTimestamp(value.updated_at)) return false
  if (value.last_run_id !== null && !isUuid(value.last_run_id)) return false
  if (value.last_outcome !== null && (typeof value.last_outcome !== 'string' || !SCHEDULE_OUTCOMES.has(value.last_outcome))) return false
  if (value.last_error !== null && typeof value.last_error !== 'string') return false
  if (value.cron_expression !== null && typeof value.cron_expression !== 'string') return false
  // last_* は異なる occurrence の摘要になり得るため、相互の同一性を推測しない。
  return isRecord(value.input) && isRecord(value.sources) && Object.values(value.sources).every((item) => typeof item === 'string')
}
