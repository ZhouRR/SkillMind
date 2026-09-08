import { API_BASE, hasStrings, isRecord, isStringArray, requestApiJson } from './http'
import { isRunDocumentSnapshots, isRunSourceSummaries, type RunDocumentSnapshotRecord, type RunSourceSummaries } from './runResources'
import {
  isChangeApproval,
  isChangeProposal,
  isEffectExecution,
  type ChangeApprovalRecord,
  type ChangeProposalRecord,
  type EffectExecutionRecord,
} from './effects'

/** Backend state machine と同じ Run status。 */
export type RunStatus =
  | 'QUEUED'
  | 'PREPARING'
  | 'RUNNING'
  | 'RETRY_PENDING'
  | 'WAITING_FOR_INPUT'
  | 'WAITING_FOR_APPROVAL'
  | 'WAITING_PERMISSION'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'CANCELLED'

/** Run 作成と現在状態取得で共有する公開 response。 */
export interface RunRecord {
  run_id: string
  project_id: string
  task_id: string
  status: RunStatus
  row_version: number
  created_at: string
  idempotent_replay: boolean
}

/** Run 取消 API が返す受付状態と現在 snapshot。 */
export interface CancelRunRecord {
  run_id: string
  project_id: string
  status: RunStatus
  row_version: number
  cancellation: 'REQUESTED' | 'CANCELLED'
}

/** PUBLISHED task descriptor から通用 Run を作成する request。input は task の input schema、
 *  sources は data source key ごとの provider を渡し、backend が精確 version と権限を snapshot に固定する。 */
export interface CreateTaskRunInput {
  skill_version_id: string
  task_key: string
  input: Record<string, unknown>
  sources: Record<string, string>
}

/** Project-scoped detail API が返す検証済み Result。 */
export interface RunResultDetail {
  result_id: string
  output_schema: string
  result_kind: 'OUTCOME_ENVELOPE' | 'STRUCTURED_OUTPUT'
  data: Record<string, unknown>
  evidence_refs: string[]
  artifact_refs: string[]
  change_proposal_refs: string[]
  optional_schema_identity: Record<string, unknown>
  summary: string
  confidence: number | null
  needs_review: boolean
  usage: Record<string, unknown>
  cost: Record<string, unknown>
  validation: Record<string, unknown>
  created_at: string
}

/** Raw Tool request/result を含まない audit summary。 */
export interface ToolCallDetail {
  tool_call_id: string
  run_attempt_id: string
  agent_session_id: string
  tool_name: string
  capability: string
  provider: string
  arguments_summary: Record<string, unknown>
  status: string
  duration_ms: number | null
  created_at: string
}

/** Run ownership 確認済み Evidence locator と excerpt。 */
export interface EvidenceDetail {
  evidence_ref: string
  tool_call_id: string
  evidence_type: string
  source_uri: string
  source_locator: Record<string, unknown>
  content_hash: string
  snapshot_uri: string | null
  excerpt: string | null
  metadata: Record<string, unknown>
  created_at: string
}

/**
 * 前 Session を次 Segment へ引き継ぐ監査可能な方式。
 *
 * `BRANCH` だけは継承ではなく併走を表す——扇出の一路が親の下で新規に始まり、親と同時に走る。
 */
export type SessionContinuationMode = 'INITIAL' | 'RESUME' | 'FORK' | 'REPLACE' | 'BRANCH'

/** AgentSession が Run 骨格の本体か、扇出の一路か。 */
export type AgentSessionKind = 'PRIMARY' | 'SUBAGENT'

/** ユーザー応答で増える一つの業務継続区間。 */
export interface RunSegmentDetail {
  run_segment_id: string | null
  segment_no: number
  trigger_type: 'INITIAL' | 'INTERACTION_RESPONSE' | 'INTERACTION_TIMEOUT' | 'APPROVAL_RESPONSE' | 'APPROVAL_TIMEOUT'
  trigger_ref: string | null
  status: 'CREATED' | 'RUNNING' | 'WAITING' | 'COMPLETED' | 'FAILED' | 'CANCELLED'
  objective: Record<string, unknown>
  checkpoint: Record<string, unknown>
  continuation_mode: SessionContinuationMode
  parent_agent_session_id: string | null
  task_brief_checksum: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
}

/** Segment 内だけで増える Worker 技術試行。 */
export interface RunAttemptDetail {
  run_attempt_id: string
  run_segment_id: string | null
  attempt_no: number
  reason: string
  status: string
  worker_id: string | null
  started_at: string | null
  finished_at: string | null
  error: Record<string, unknown> | null
  created_at: string
}

/** 順次実行する AgentSession の lineage と実行 identity。 */
export interface AgentSessionDetail {
  agent_session_id: string
  run_segment_id: string | null
  run_attempt_id: string
  /** SDK が session を開く前に落ちた扇出 branch では null。捏造した ID は返さない。 */
  sdk_session_id: string | null
  parent_session_id: string | null
  continuation_mode: SessionContinuationMode
  session_kind: AgentSessionKind
  checkpoint_checksum: string | null
  engine_options_checksum: string | null
  engine: string
  sdk_version: string
  cli_version: string
  model: string
  status: string
  usage: Record<string, unknown>
  cost: Record<string, unknown>
  created_at: string
  updated_at: string
}

/** Interaction に一度だけ追加された actor 回答。 */
export interface InteractionResponseDetail {
  response_id: string
  actor_id: string
  interaction_version: number
  response: Record<string, unknown>
  created_at: string
}

/** Agent が Run を停止して公開した構造化質問。 */
export interface UserInteractionDetail {
  interaction_id: string
  run_segment_id: string
  agent_session_id: string
  interaction_type: 'CLARIFICATION' | 'CHOICE' | 'REVIEW' | 'EFFECT_APPROVAL'
  prompt: Record<string, unknown>
  options: Array<Record<string, unknown>>
  required: boolean
  expires_at: string
  status: 'OPEN' | 'RESPONDED' | 'EXPIRED' | 'CANCELLED'
  version: number
  continuation_mode: SessionContinuationMode
  checkpoint_checksum: string
  change_proposal_id: string | null
  response: InteractionResponseDetail | null
  created_at: string
}

/** CLARIFICATION/CHOICE/REVIEW へ送る型別回答。 */
export interface InteractionAnswerInput {
  text?: string
  selected_option_keys?: string[]
}

/** 回答受理後に作られた Segment と Run snapshot。 */
export interface RespondedInteractionRecord {
  run_id: string
  project_id: string
  status: RunStatus
  row_version: number
  interaction_id: string
  response_id: string
  run_segment_id: string
  segment_no: number
  continuation_mode: SessionContinuationMode
  idempotent_replay: boolean
}

/** Workspace の Result/Evidence view が利用する Run detail。 */
export interface RunDetailRecord {
  run_id: string
  project_id: string
  task_id: string
  status: RunStatus
  row_version: number
  created_at: string
  input: Record<string, unknown>
  selected_sources: RunSourceSummaries
  document_snapshots: RunDocumentSnapshotRecord[]
  output_schema: Record<string, unknown> | null
  output_schema_checksum: string | null
  result: RunResultDetail | null
  segments: RunSegmentDetail[]
  attempts: RunAttemptDetail[]
  sessions: AgentSessionDetail[]
  interactions: UserInteractionDetail[]
  change_proposals: ChangeProposalRecord[]
  approvals: ChangeApprovalRecord[]
  effect_executions: EffectExecutionRecord[]
  tool_calls: ToolCallDetail[]
  evidence: EvidenceDetail[]
  skill_snapshots: Array<{
    skill_version_id: string
    sort_order: number
    manifest_checksum: string
    config_snapshot: Record<string, unknown>
  }>
}

/** Project Run history の一覧と再表示に必要な一行。 */
export interface RunHistoryItemRecord {
  run_id: string
  project_id: string
  task_id: string
  status: RunStatus
  row_version: number
  created_at: string
  started_at: string | null
  finished_at: string | null
  input: Record<string, unknown>
  selected_sources: RunSourceSummaries
  result_summary: string | null
  result_confidence: number | null
  result_needs_review: boolean | null
}

/** Offset pagination metadata を含む Project Run history。 */
export interface RunHistoryPageRecord {
  items: RunHistoryItemRecord[]
  limit: number
  offset: number
  has_more: boolean
}

/** Backend state machine と共有する Run status の全語彙。lib/runReplay も同じ集合を参照する。 */
export const RUN_STATUSES: ReadonlySet<string> = new Set<RunStatus>([
  'QUEUED',
  'PREPARING',
  'RUNNING',
  'RETRY_PENDING',
  'WAITING_FOR_INPUT',
  'WAITING_FOR_APPROVAL',
  'WAITING_PERMISSION',
  'SUCCEEDED',
  'FAILED',
  'CANCELLED',
])

/** PUBLISHED task を精確 version へ束縛し、schema 検証済み入力で通用 Run を作成する。 */
export async function createTaskRun(
  projectId: string,
  request: CreateTaskRunInput,
  idempotencyKey: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<RunRecord> {
  const value = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/task-runs`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey,
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify(request),
      signal,
    },
  )
  return parseRunRecord(value)
}

/** SSE と独立して現在の Run snapshot を再取得する。 */
export async function loadRun(runId: string, signal?: AbortSignal): Promise<RunRecord> {
  return parseRunRecord(await requestApiJson(`${API_BASE}/runs/${encodeURIComponent(runId)}`, { signal }))
}

/** CSRF token 付きで Run cancellation intent を idempotent に追加する。 */
export async function cancelRun(
  runId: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<CancelRunRecord> {
  return parseCancelRunRecord(await requestApiJson(
    `${API_BASE}/runs/${encodeURIComponent(runId)}/cancel`,
    { method: 'POST', headers: { 'X-CSRF-Token': csrfToken }, signal },
  ))
}

/** Project ownership を含む Run Result、ToolCall、Evidence を取得する。 */
export async function loadRunDetail(
  projectId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<RunDetailRecord> {
  return parseRunDetail(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}/detail`,
    { signal },
  ))
}

/** Project/CSRF/version/idempotency 境界を通して Interaction へ追加式回答を保存する。 */
export async function respondToInteraction(
  projectId: string,
  runId: string,
  interactionId: string,
  interactionVersion: number,
  response: InteractionAnswerInput,
  idempotencyKey: string,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<RespondedInteractionRecord> {
  const value = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}`
      + `/interactions/${encodeURIComponent(interactionId)}/responses`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey,
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({ interaction_version: interactionVersion, response }),
      signal,
    },
  )
  return parseRespondedInteraction(value)
}

/** Project 内の Run history を新しい順に取得する。 */
export async function loadRunHistory(
  projectId: string,
  limit: number,
  offset: number,
  signal?: AbortSignal,
  statuses: readonly RunStatus[] = [],
): Promise<RunHistoryPageRecord> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  // 絞り込みは server 側で行う。先頭 page を client で filter すると、待機中の Run が
  // 古い page にあるときに取りこぼす。
  for (const status of statuses) query.append('status', status)
  return parseRunHistory(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/runs?${query.toString()}`,
    { signal },
  ))
}

/** 利用者の応答・承認を待っている Run の状態。「待你处理」の唯一の定義。 */
export const PENDING_RUN_STATUSES: readonly RunStatus[] = [
  'WAITING_FOR_INPUT',
  'WAITING_FOR_APPROVAL',
]

/** 現在 Project で利用者の手を待っている Run を新しい順に取得する。 */
export async function loadPendingRuns(
  projectId: string,
  limit: number,
  signal?: AbortSignal,
): Promise<RunHistoryItemRecord[]> {
  const page = await loadRunHistory(projectId, limit, 0, signal, PENDING_RUN_STATUSES)
  return page.items
}

/** Unknown JSON から取消 response の公開 field と enum を検証する。 */
function parseCancelRunRecord(value: unknown): CancelRunRecord {
  if (
    !isRecord(value)
    || !hasStrings(value, ['run_id', 'project_id', 'status', 'cancellation'])
    || !RUN_STATUSES.has(value.status as string)
    || !Number.isInteger(value.row_version)
    || (value.cancellation !== 'REQUESTED' && value.cancellation !== 'CANCELLED')
  ) {
    throw new Error('Cancel Run response did not match its contract')
  }
  return value as unknown as CancelRunRecord
}

/** Unknown JSON から Run response の必須 shape と status を検証する。 */
function parseRunRecord(value: unknown): RunRecord {
  if (
    !isRecord(value)
    || !hasStrings(value, ['run_id', 'project_id', 'task_id', 'created_at', 'status'])
    || !RUN_STATUSES.has(value.status as string)
    || typeof value.row_version !== 'number'
    || !Number.isInteger(value.row_version)
    || value.row_version < 1
    || typeof value.idempotent_replay !== 'boolean'
  ) {
    throw new Error('Run response did not match its contract')
  }
  return value as unknown as RunRecord
}

/** Unknown JSON を Result/Evidence の Project-scoped read contract へ制限する。 */
function parseRunDetail(value: unknown): RunDetailRecord {
  if (
    !isRecord(value)
    || !hasStrings(value, ['run_id', 'project_id', 'task_id', 'created_at', 'status'])
    || !RUN_STATUSES.has(value.status as string)
    || !Number.isInteger(value.row_version)
    || !isRecord(value.input)
    || !isRunSourceSummaries(value.selected_sources)
    || !isRunDocumentSnapshots(value.document_snapshots, value.project_id as string, value.selected_sources)
    || !(isRecord(value.output_schema) || value.output_schema === null)
    || !(typeof value.output_schema_checksum === 'string' || value.output_schema_checksum === null)
    || !Array.isArray(value.segments)
    || !value.segments.every(isRunSegmentDetail)
    || !Array.isArray(value.attempts)
    || !value.attempts.every(isRunAttemptDetail)
    || !Array.isArray(value.sessions)
    || !value.sessions.every(isAgentSessionDetail)
    || !Array.isArray(value.interactions)
    || !value.interactions.every(isUserInteractionDetail)
    || !Array.isArray(value.change_proposals)
    || !value.change_proposals.every(isChangeProposal)
    || !Array.isArray(value.approvals)
    || !value.approvals.every(isChangeApproval)
    || !Array.isArray(value.effect_executions)
    || !value.effect_executions.every(isEffectExecution)
    || !Array.isArray(value.tool_calls)
    || !value.tool_calls.every(isToolCallDetail)
    || !Array.isArray(value.evidence)
    || !value.evidence.every(isEvidenceDetail)
    || !Array.isArray(value.skill_snapshots)
    || !value.skill_snapshots.every(isRunSkillSnapshot)
    || (value.result !== null && !isRunResultDetail(value.result))
  ) {
    throw new Error('Run detail response did not match its contract')
  }
  return value as unknown as RunDetailRecord
}

/** Unknown JSON を回答後の Run/Segment snapshot へ制限する。 */
function parseRespondedInteraction(value: unknown): RespondedInteractionRecord {
  if (
    !isRecord(value)
    || !hasStrings(value, [
      'run_id', 'project_id', 'status', 'interaction_id', 'response_id',
      'run_segment_id', 'continuation_mode',
    ])
    || !RUN_STATUSES.has(value.status as string)
    || !SESSION_CONTINUATION_MODES.has(value.continuation_mode as string)
    || !Number.isInteger(value.row_version)
    || !Number.isInteger(value.segment_no)
    || typeof value.idempotent_replay !== 'boolean'
  ) {
    throw new Error('Interaction response did not match its contract')
  }
  return value as unknown as RespondedInteractionRecord
}

const SESSION_CONTINUATION_MODES: ReadonlySet<string> = new Set<SessionContinuationMode>([
  'INITIAL', 'RESUME', 'FORK', 'REPLACE', 'BRANCH',
])
const AGENT_SESSION_KINDS: ReadonlySet<string> = new Set<AgentSessionKind>([
  'PRIMARY', 'SUBAGENT',
])
const RUN_SEGMENT_TRIGGERS = new Set([
  'INITIAL', 'INTERACTION_RESPONSE', 'INTERACTION_TIMEOUT', 'APPROVAL_RESPONSE', 'APPROVAL_TIMEOUT',
])
const RUN_SEGMENT_STATUSES = new Set([
  'CREATED', 'RUNNING', 'WAITING', 'COMPLETED', 'FAILED', 'CANCELLED',
])

/** Unknown object が RunSegment timeline contract を満たすか確認する。 */
function isRunSegmentDetail(value: unknown): value is RunSegmentDetail {
  return isRecord(value)
    && hasStrings(value, ['trigger_type', 'status', 'continuation_mode', 'created_at'])
    && (typeof value.run_segment_id === 'string' || value.run_segment_id === null)
    && (typeof value.trigger_ref === 'string' || value.trigger_ref === null)
    && (typeof value.parent_agent_session_id === 'string' || value.parent_agent_session_id === null)
    && (typeof value.task_brief_checksum === 'string' || value.task_brief_checksum === null)
    && (typeof value.started_at === 'string' || value.started_at === null)
    && (typeof value.finished_at === 'string' || value.finished_at === null)
    && Number.isInteger(value.segment_no)
    && isRecord(value.objective)
    && isRecord(value.checkpoint)
    && RUN_SEGMENT_TRIGGERS.has(value.trigger_type as string)
    && RUN_SEGMENT_STATUSES.has(value.status as string)
    && SESSION_CONTINUATION_MODES.has(value.continuation_mode as string)
}

/** Unknown object が RunAttempt timeline contract を満たすか確認する。 */
function isRunAttemptDetail(value: unknown): value is RunAttemptDetail {
  return isRecord(value)
    && hasStrings(value, ['run_attempt_id', 'reason', 'status', 'created_at'])
    && (typeof value.run_segment_id === 'string' || value.run_segment_id === null)
    && (typeof value.worker_id === 'string' || value.worker_id === null)
    && (typeof value.started_at === 'string' || value.started_at === null)
    && (typeof value.finished_at === 'string' || value.finished_at === null)
    && (isRecord(value.error) || value.error === null)
    && Number.isInteger(value.attempt_no)
}

/** Unknown object が順次 AgentSession lineage contract を満たすか確認する。 */
function isAgentSessionDetail(value: unknown): value is AgentSessionDetail {
  return isRecord(value)
    && hasStrings(value, [
      'agent_session_id', 'run_attempt_id', 'continuation_mode', 'session_kind',
      'engine', 'sdk_version', 'cli_version', 'model', 'status', 'created_at', 'updated_at',
    ])
    && (typeof value.sdk_session_id === 'string' || value.sdk_session_id === null)
    && (typeof value.run_segment_id === 'string' || value.run_segment_id === null)
    && (typeof value.parent_session_id === 'string' || value.parent_session_id === null)
    && (typeof value.checkpoint_checksum === 'string' || value.checkpoint_checksum === null)
    && (typeof value.engine_options_checksum === 'string' || value.engine_options_checksum === null)
    && SESSION_CONTINUATION_MODES.has(value.continuation_mode as string)
    && AGENT_SESSION_KINDS.has(value.session_kind as string)
    && isRecord(value.usage)
    && isRecord(value.cost)
}

/** Unknown object が公開 Interaction と optional response を満たすか確認する。 */
function isUserInteractionDetail(value: unknown): value is UserInteractionDetail {
  return isRecord(value)
    && hasStrings(value, [
      'interaction_id', 'run_segment_id', 'agent_session_id', 'interaction_type',
      'expires_at', 'status', 'continuation_mode', 'checkpoint_checksum', 'created_at',
    ])
    && INTERACTION_TYPES.has(value.interaction_type as string)
    && INTERACTION_STATUSES.has(value.status as string)
    && SESSION_CONTINUATION_MODES.has(value.continuation_mode as string)
    && isRecord(value.prompt)
    && Array.isArray(value.options)
    && value.options.every(isRecord)
    && typeof value.required === 'boolean'
    && Number.isInteger(value.version)
    && (typeof value.change_proposal_id === 'string' || value.change_proposal_id === null)
    && (value.response === null || isInteractionResponseDetail(value.response))
}

const INTERACTION_TYPES: ReadonlySet<string> = new Set([
  'CLARIFICATION', 'CHOICE', 'REVIEW', 'EFFECT_APPROVAL',
])
const INTERACTION_STATUSES: ReadonlySet<string> = new Set([
  'OPEN', 'RESPONDED', 'EXPIRED', 'CANCELLED',
])

/** Unknown object が追加式 InteractionResponse を満たすか確認する。 */
function isInteractionResponseDetail(value: unknown): value is InteractionResponseDetail {
  return isRecord(value)
    && hasStrings(value, ['response_id', 'actor_id', 'created_at'])
    && Number.isInteger(value.interaction_version)
    && isRecord(value.response)
}

/** Unknown object が Run の公開 SkillVersion binding か確認する。 */
function isRunSkillSnapshot(value: unknown): boolean {
  return isRecord(value)
    && hasStrings(value, ['skill_version_id', 'manifest_checksum'])
    && Number.isInteger(value.sort_order)
    && isRecord(value.config_snapshot)
}

/** Unknown JSON を Project Run history pagination contract へ制限する。 */
function parseRunHistory(value: unknown): RunHistoryPageRecord {
  if (
    !isRecord(value)
    || !Array.isArray(value.items)
    || !value.items.every(isRunHistoryItem)
    || !Number.isInteger(value.limit)
    || !Number.isInteger(value.offset)
    || typeof value.has_more !== 'boolean'
  ) {
    throw new Error('Run history response did not match its contract')
  }
  return value as unknown as RunHistoryPageRecord
}

/** Unknown object が再表示可能な Run history item か確認する。 */
function isRunHistoryItem(value: unknown): value is RunHistoryItemRecord {
  return isRecord(value)
    && hasStrings(value, ['run_id', 'project_id', 'task_id', 'status', 'created_at'])
    && RUN_STATUSES.has(value.status as string)
    && Number.isInteger(value.row_version)
    && (typeof value.started_at === 'string' || value.started_at === null)
    && (typeof value.finished_at === 'string' || value.finished_at === null)
    && isRecord(value.input)
    && isRunSourceSummaries(value.selected_sources)
    && (typeof value.result_summary === 'string' || value.result_summary === null)
    && (typeof value.result_confidence === 'number' || value.result_confidence === null)
    && (typeof value.result_needs_review === 'boolean' || value.result_needs_review === null)
}

/** Unknown object が公開 RunResult shape を満たすか確認する。 */
function isRunResultDetail(value: unknown): value is RunResultDetail {
  return isRecord(value)
    && hasStrings(value, ['result_id', 'output_schema', 'result_kind', 'summary', 'created_at'])
    && (value.result_kind === 'OUTCOME_ENVELOPE' || value.result_kind === 'STRUCTURED_OUTPUT')
    && isRecord(value.data)
    && isStringArray(value.evidence_refs)
    && isStringArray(value.artifact_refs)
    && isStringArray(value.change_proposal_refs)
    && isRecord(value.optional_schema_identity)
    && (typeof value.confidence === 'number' || value.confidence === null)
    && typeof value.needs_review === 'boolean'
    && isRecord(value.usage)
    && isRecord(value.cost)
    && isRecord(value.validation)
}

/** Unknown object が raw payload を含まない ToolCall summary か確認する。 */
function isToolCallDetail(value: unknown): value is ToolCallDetail {
  return isRecord(value)
    && hasStrings(value, [
      'tool_call_id', 'run_attempt_id', 'agent_session_id', 'tool_name',
      'capability', 'provider', 'status', 'created_at',
    ])
    && isRecord(value.arguments_summary)
    && (typeof value.duration_ms === 'number' || value.duration_ms === null)
}

/** Unknown object が再定位可能な Evidence shape を満たすか確認する。 */
function isEvidenceDetail(value: unknown): value is EvidenceDetail {
  return isRecord(value)
    && hasStrings(value, [
      'evidence_ref', 'tool_call_id', 'evidence_type', 'source_uri',
      'content_hash', 'created_at',
    ])
    && isRecord(value.source_locator)
    && (typeof value.snapshot_uri === 'string' || value.snapshot_uri === null)
    && (typeof value.excerpt === 'string' || value.excerpt === null)
    && isRecord(value.metadata)
}
