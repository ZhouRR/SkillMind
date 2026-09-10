import { API_BASE, hasStrings, isRecord, parseItemList, requestApiJson } from './http'
import { exactFields } from './http'
import { isApiTimestamp, isNonNilUuid } from '../lib/validation'
import { isJsonValue } from '../lib/jsonValue'

/** Result field に対する人工 revision 入力。 */
export interface EvaluationRevisionInput {
  pointer: string
  suggested_value: unknown
  reason: string
}

/** 人工 Evaluation の作成入力。 */
export interface CreateEvaluationInput {
  rating: number
  verdict: 'accurate' | 'partially_accurate' | 'inaccurate' | 'uncertain'
  comment: string
  revisions: EvaluationRevisionInput[]
}

/** Server が AI 原値を補完した人工 revision。 */
export interface EvaluationRevisionRecord extends EvaluationRevisionInput {
  original_value: unknown
}

/** Result を変更せず追加された人工 Evaluation。 */
export interface EvaluationRecord {
  evaluation_id: string
  result_id: string
  run_id: string
  user_id: string
  rating: number
  verdict: CreateEvaluationInput['verdict']
  comment: string
  revisions: EvaluationRevisionRecord[]
  created_at: string
}

/** 評価者 identity と CSRF token 付きで Project-scoped Evaluation を追加する。 */
export async function createEvaluation(
  projectId: string,
  runId: string,
  input: CreateEvaluationInput,
  csrfToken: string,
  signal?: AbortSignal,
): Promise<EvaluationRecord> {
  return parseEvaluation(await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}/evaluations`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body: JSON.stringify(input),
      signal,
    },
  ))
}

/** Project-scoped Run Result の人工評価履歴を取得する。 */
export async function loadEvaluations(
  projectId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<EvaluationRecord[]> {
  const value = await requestApiJson(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/runs/${encodeURIComponent(runId)}/evaluations`,
    { signal },
  )
  return parseItemList(
    value,
    'items',
    isEvaluation,
    'Evaluation history response did not match its contract',
  )
}

/** Unknown JSON を追加式 Evaluation response へ制限する。 */
function parseEvaluation(value: unknown): EvaluationRecord {
  if (!isEvaluation(value)) throw new Error('Evaluation response did not match its contract')
  return value
}

/** Unknown object が AI 原値を含む Evaluation contract か確認する。 */
function isEvaluation(value: unknown): value is EvaluationRecord {
  return isRecord(value)
    && hasStrings(value, [
      'evaluation_id', 'result_id', 'run_id', 'user_id', 'verdict', 'comment', 'created_at',
    ])
    && typeof value.verdict === 'string'
    && ['accurate', 'partially_accurate', 'inaccurate', 'uncertain'].includes(value.verdict)
    && typeof value.rating === 'number'
    && Number.isInteger(value.rating)
    && value.rating >= 1
    && value.rating <= 5
    && Array.isArray(value.revisions)
    && value.revisions.every(isEvaluationRevision)
}

/** Unknown object が revision の原値・提案値・理由をすべて持つか確認する。 */
function isEvaluationRevision(value: unknown): value is EvaluationRevisionRecord {
  return isRecord(value)
    && hasStrings(value, ['pointer', 'reason'])
    && Object.hasOwn(value, 'original_value')
    && Object.hasOwn(value, 'suggested_value')
}

/** 新しい受付記録/ページだけを厳格化し、旧 API の互換形状には遡及しない。 */
export function isStrictEvaluation(value: unknown): value is EvaluationRecord {
  return isRecord(value) && isEvaluation(value) && exactFields(value, [
    'evaluation_id', 'result_id', 'run_id', 'user_id', 'rating', 'verdict', 'comment', 'revisions', 'created_at',
  ]) && [value.evaluation_id, value.result_id, value.run_id, value.user_id].every(isNonNilUuid)
    && isApiTimestamp(value.created_at) && isJsonValue(value) && [...value.comment].length <= 4000
    && value.revisions.length <= 100
    && new Set(value.revisions.map((revision) => revision.pointer)).size === value.revisions.length
    && value.revisions.every((revision) => isRecord(revision) && exactFields(revision, ['pointer', 'original_value', 'suggested_value', 'reason'])
      && revision.pointer.startsWith('/') && [...revision.pointer].length <= 512
      && !/~(?:[^01]|$)/.test(revision.pointer)
      && [...revision.reason].length >= 1 && [...revision.reason].length <= 1000
      && isJsonValue(revision.original_value) && isJsonValue(revision.suggested_value))
}
