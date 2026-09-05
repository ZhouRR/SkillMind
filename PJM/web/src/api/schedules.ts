import { API_BASE, hasStrings, isRecord, parseItemList, requestApiJson } from './http'

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

/** Project 内 schedule 一覧を取得する。 */
export async function loadProjectSchedules(
  projectId: string,
  signal?: AbortSignal,
): Promise<ScheduleRecord[]> {
  const payload = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/schedules?limit=100`,
    { signal },
  )
  return parseItemList(payload, 'schedules', isScheduleRecord, 'Schedule list is invalid')
}

/** 保存前に次回発火時刻を確認する。空配列は返らず、不正な定義は Problem になる。 */
export async function previewSchedule(
  projectId: string,
  definition: ScheduleDefinitionInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<string[]> {
  const payload = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/schedules/preview`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ definition }),
      signal,
    },
  )
  const occurrences: unknown = isRecord(payload) ? payload.occurrences : null
  if (!Array.isArray(occurrences) || !occurrences.every((item) => typeof item === 'string')) {
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
  return parseSchedule(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/schedules`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify(input),
      signal,
    },
  ))
}

/** schedule の定義と凍結入力を差し替える。 */
export async function updateSchedule(
  projectId: string,
  scheduleId: string,
  input: UpdateScheduleInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ScheduleRecord> {
  return parseSchedule(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/schedules/${encodeURIComponent(scheduleId)}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify(input),
      signal,
    },
  ))
}

/** 暂停・恢复・归档を適用する。遷移の可否は server の状態機が判定する。 */
export async function changeScheduleStatus(
  projectId: string,
  scheduleId: string,
  status: ScheduleStatus,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<ScheduleRecord> {
  return parseSchedule(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/schedules/${encodeURIComponent(scheduleId)}/status`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify({ status }),
      signal,
    },
  ))
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

/** schedule record の契約を実行時に検証する。 */
function isScheduleRecord(value: unknown): value is ScheduleRecord {
  if (!isRecord(value)) return false
  if (!hasStrings(value, ['schedule_id', 'project_id', 'name', 'timezone', 'task_key'])) {
    return false
  }
  if (typeof value.kind !== 'string' || !SCHEDULE_KINDS.has(value.kind)) return false
  if (typeof value.status !== 'string' || !SCHEDULE_STATUSES.has(value.status)) return false
  if (typeof value.run_count !== 'number' || typeof value.missed_count !== 'number') return false
  if (typeof value.row_version !== 'number') return false
  return isRecord(value.input) && isRecord(value.sources)
}
