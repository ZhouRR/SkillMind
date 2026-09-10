import { API_BASE, exactFields, isRecord, requestApiJson } from './http'
import { isStrictEvaluation, type CreateEvaluationInput, type EvaluationRecord } from './evaluations'
import { apiTimestampMicroseconds, isNonNilUuid, sameUuid } from '../lib/validation'
import { isJsonValue, sameJsonValue } from '../lib/jsonValue'

/** 一回の追加評価を識別する公開入力。key は呼出側で明示的に固定する。 */
export interface EvaluationSubmissionInput extends CreateEvaluationInput {
  submission_key: string
  result_id: string
}

/** 原 actor/Result に保存された独立受付記録。現在の一覧からは推定しない。 */
export interface EvaluationSubmissionReceipt {
  project_id: string
  run_id: string
  submission_key: string
  evaluation: EvaluationRecord
}

/** Server cursor の一ページ。全件数や一貫した snapshot は表さない。 */
export interface EvaluationPage {
  project_id: string
  run_id: string
  result_id: string
  items: EvaluationRecord[]
  next_cursor: string | null
}

/** 原 key/payload を一度だけ POST し、201 と保存済み原要求の 200 だけを受理する。 */
export async function submitEvaluation(projectId: string, runId: string, input: EvaluationSubmissionInput,
  csrfToken: string, signal?: AbortSignal): Promise<EvaluationSubmissionReceipt> {
  const path = evaluationPath(projectId, runId)
  if (!isEvaluationSubmissionInput(input)) throw new Error('Evaluation submission input is invalid')
  const body = JSON.stringify(input)
  const original = JSON.parse(body) as EvaluationSubmissionInput
  signal?.throwIfAborted()
  const value = await requestApiJson(`${path}/evaluation-submissions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body, signal,
  }, { statuses: [200, 201], validate: () => {} })
  signal?.throwIfAborted()
  const receipt = parseReceipt(value, projectId, runId, original.result_id, original.submission_key)
  const saved = receipt.evaluation
  if (saved.rating !== original.rating || saved.verdict !== original.verdict || saved.comment !== original.comment
    || !sameJsonValue(saved.revisions.map(({ pointer, suggested_value, reason }) => ({ pointer, suggested_value, reason })), original.revisions)) {
    throw new Error('Evaluation receipt does not match the submitted content')
  }
  return receipt
}

/** 確認は GET のみ。404 は原 POST が未受理という証明ではない。 */
export async function loadEvaluationSubmission(projectId: string, runId: string, resultId: string,
  submissionKey: string, signal?: AbortSignal): Promise<EvaluationSubmissionReceipt> {
  const path = evaluationPath(projectId, runId)
  if (![resultId, submissionKey].every(isNonNilUuid)) throw new Error('Invalid evaluation submission identity')
  signal?.throwIfAborted()
  const value = await requestApiJson(`${path}/evaluation-submissions/${encodeURIComponent(submissionKey)}?result_id=${encodeURIComponent(resultId)}`,
    { signal, cache: 'no-store' }, 200)
  signal?.throwIfAborted()
  return parseReceipt(value, projectId, runId, resultId, submissionKey)
}

/** ページの scope/order/cursor を検証し、別 Result の履歴を混ぜない。 */
export async function loadEvaluationPage(projectId: string, runId: string, resultId: string,
  after: string | null = null, limit = 20, signal?: AbortSignal): Promise<EvaluationPage> {
  const path = evaluationPath(projectId, runId)
  if (!isNonNilUuid(resultId) || after !== null && !isNonNilUuid(after)
    || !Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error('Invalid evaluation page request')
  const query = new URLSearchParams({ limit: String(limit) })
  if (after !== null) query.set('after', after)
  signal?.throwIfAborted()
  const value = await requestApiJson(`${path}/evaluations/page?${query}`, { signal, cache: 'no-store' }, 200)
  signal?.throwIfAborted()
  if (!isRecord(value) || !exactFields(value, ['project_id', 'run_id', 'result_id', 'items', 'next_cursor'])
    || !sameScope(value, projectId, runId) || !isNonNilUuid(value.result_id) || !sameUuid(value.result_id, resultId)
    || !Array.isArray(value.items) || value.items.length > limit
    || !value.items.every((item): item is EvaluationRecord => isStrictEvaluation(item)
      && sameUuid(item.run_id, runId) && sameUuid(item.result_id, resultId))
    || new Set(value.items.map((item) => item.evaluation_id.toLowerCase())).size !== value.items.length
    || value.items.some((item, index, items) => sameUuid(item.evaluation_id, after ?? '')
      || index > 0 && compareEvaluations(items[index - 1]!, item) >= 0)
    || value.next_cursor !== null && (!isNonNilUuid(value.next_cursor) || value.items.length !== limit
      || !sameUuid(value.items.at(-1)!.evaluation_id, value.next_cursor))) {
    throw new Error('Evaluation page did not match its contract')
  }
  return value as unknown as EvaluationPage
}

/** 新入力の厳格な形と JSON 互換性を HTTP 前に確認する。 */
export function isEvaluationSubmissionInput(value: unknown): value is EvaluationSubmissionInput {
  return isRecord(value) && exactFields(value, ['submission_key', 'result_id', 'rating', 'verdict', 'comment', 'revisions'])
    && isNonNilUuid(value.submission_key) && isNonNilUuid(value.result_id)
    && typeof value.rating === 'number' && Number.isInteger(value.rating) && value.rating >= 1 && value.rating <= 5
    && typeof value.verdict === 'string' && ['accurate', 'partially_accurate', 'inaccurate', 'uncertain'].includes(value.verdict)
    && typeof value.comment === 'string' && [...value.comment].length <= 4000
    && Array.isArray(value.revisions) && value.revisions.length <= 100 && isJsonValue(value)
    && value.revisions.every((revision) => isRecord(revision) && exactFields(revision, ['pointer', 'suggested_value', 'reason'])
      && typeof revision.pointer === 'string' && revision.pointer.startsWith('/') && [...revision.pointer].length <= 512
      && !/~(?:[^01]|$)/.test(revision.pointer)
      && typeof revision.reason === 'string' && [...revision.reason].length >= 1 && [...revision.reason].length <= 1000)
    && new Set(value.revisions.map((revision) => (revision as { pointer: string }).pointer)).size === value.revisions.length
}

/** 保存時刻の microsecond を保った server と同じ昇順を使う。 */
export function compareEvaluations(left: EvaluationRecord, right: EvaluationRecord): number {
  const a = apiTimestampMicroseconds(left.created_at)
  const b = apiTimestampMicroseconds(right.created_at)
  return a === b ? left.evaluation_id.toLowerCase().localeCompare(right.evaluation_id.toLowerCase()) : a < b ? -1 : 1
}

/** 認証済み原要求の公開受付記録だけを返す。本文の一致は呼出側の不変 intent でも照合する。 */
function parseReceipt(value: unknown, projectId: string, runId: string, resultId: string, key: string): EvaluationSubmissionReceipt {
  if (!isRecord(value) || !exactFields(value, ['project_id', 'run_id', 'submission_key', 'evaluation'])
    || !sameScope(value, projectId, runId) || !isNonNilUuid(value.submission_key) || !sameUuid(value.submission_key, key)
    || !isStrictEvaluation(value.evaluation) || !sameUuid(value.evaluation.run_id, runId)
    || !sameUuid(value.evaluation.result_id, resultId)) throw new Error('Evaluation receipt did not match its contract')
  return value as unknown as EvaluationSubmissionReceipt
}

/** API が公開する scope の形と現在の要求との一致を分けず確認する。 */
function sameScope(value: Record<string, unknown>, projectId: string, runId: string): boolean {
  return isNonNilUuid(value.project_id) && isNonNilUuid(value.run_id)
    && sameUuid(value.project_id, projectId) && sameUuid(value.run_id, runId)
}

/** Model/URL 入力から任意 endpoint を構成しない。 */
function evaluationPath(projectId: string, runId: string): string {
  if (![projectId, runId].every(isNonNilUuid)) throw new Error('Invalid evaluation scope')
  return `${API_BASE}/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}`
}
