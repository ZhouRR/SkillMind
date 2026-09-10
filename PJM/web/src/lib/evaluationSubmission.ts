import { ApiProblemError, type CreateEvaluationInput, type EvaluationRecord, type EvaluationSubmissionInput,
  type EvaluationSubmissionReceipt } from '../api'
import { isEvaluationSubmissionInput } from '../api/evaluationSubmissions'
import type { ResourceRequestPolicy } from '../hooks/useResourceRequest'
import { isJsonValue, sameJsonValue } from './jsonValue'
import { isNonNilUuid, sameUuid } from './validation'

/** Result を含む評価所有者。Session secret は原 payload と公開表示に含めない。 */
export interface EvaluationScope { actorId: string; projectId: string; runId: string; resultId: string }

/** Form の独立行。pointer と JSON text は確定まで編集可能な memory-only 草稿。 */
export interface EvaluationRevisionDraft { id: number; pointer: string; value: string; reason: string }

/** 全修訂を一つの追加式評価として送る草稿。 */
export interface EvaluationDraft {
  rating: number
  verdict: CreateEvaluationInput['verdict']
  comment: string
  revisions: EvaluationRevisionDraft[]
}

/** 原内容の無い手入力 key は GET 専用。受付記録から再送用の入力を捏造しない。 */
export interface FrozenEvaluationSubmission {
  readonly scope: Readonly<EvaluationScope>
  readonly key: string
  readonly body: string | null
  readonly resultData: string
}

/** 後続の明確な拒否でも、それ以前の unknown を消さない。 */
export interface PendingEvaluationSubmission {
  request: FrozenEvaluationSubmission
  phase: 'sending' | 'checking' | 'unknown' | 'refused' | 'confirmed'
  uncertain: boolean
  failure: EvaluationFailure | null
  receipt: EvaluationSubmissionReceipt | null
}

/** 機密の Problem 本文ではなく契約の分類だけを catalog に渡す。 */
export interface EvaluationFailure {
  key: 'sessionExpired' | 'csrfRejected' | 'forbidden' | 'notFound' | 'projectArchived'
    | 'resultUnavailable' | 'resultMismatch' | 'invalidRequest' | 'invalidRevision' | 'conflict'
    | 'notSeen' | 'unavailable' | 'cursorInvalid' | 'unknown' | 'loadFailed' | 'readTimeout'
}

/** 入力の問題は行を示し、null と不在、文字列と JSON parse 失敗を混同しない。 */
export interface EvaluationDraftError { key: 'invalidJson' | 'invalidPointer' | 'duplicatePointer' | 'invalidReason' | 'invalidDraft'; row?: number }

/** JSON Pointer の既存 target だけを解決する。prototype と array append は対象外。 */
export function evaluationOriginal(data: unknown, pointer: string): { found: boolean; value?: unknown } {
  if (!pointer.startsWith('/') || /~(?:[^01]|$)/.test(pointer)) return { found: false }
  let current = data
  for (const raw of pointer.slice(1).split('/')) {
    const token = raw.replace(/~1/g, '/').replace(/~0/g, '~')
    if (Array.isArray(current)) {
      if (!/^(0|[1-9]\d*)$/.test(token) || !Number.isSafeInteger(Number(token)) || Number(token) >= current.length) return { found: false }
      current = current[Number(token)]
    } else if (typeof current === 'object' && current !== null && Object.hasOwn(current, token)) {
      current = (current as Record<string, unknown>)[token]
    } else return { found: false }
  }
  return { found: true, value: current }
}

/** 全行を検証してから返し、一部の修訂だけを黙って送らない。 */
export function evaluationInput(draft: EvaluationDraft, resultData: unknown):
  { input: CreateEvaluationInput; error: null } | { input: null; error: EvaluationDraftError } {
  if (!Number.isInteger(draft.rating) || draft.rating < 1 || draft.rating > 5
    || !['accurate', 'partially_accurate', 'inaccurate', 'uncertain'].includes(draft.verdict)
    || [...draft.comment].length > 4000 || draft.revisions.length > 100) return { input: null, error: { key: 'invalidDraft' } }
  const revisions: CreateEvaluationInput['revisions'] = []
  const pointers = new Set<string>()
  for (const [row, revision] of draft.revisions.entries()) {
    if ([...revision.pointer].length > 512 || !evaluationOriginal(resultData, revision.pointer).found) return { input: null, error: { key: 'invalidPointer', row } }
    if (pointers.has(revision.pointer)) return { input: null, error: { key: 'duplicatePointer', row } }
    pointers.add(revision.pointer)
    if (![...revision.reason].length || [...revision.reason].length > 1000) return { input: null, error: { key: 'invalidReason', row } }
    let value: unknown
    try { value = JSON.parse(revision.value) as unknown } catch { return { input: null, error: { key: 'invalidJson', row } } }
    if (!isJsonValue(value)) return { input: null, error: { key: 'invalidJson', row } }
    revisions.push({ pointer: revision.pointer, suggested_value: value, reason: revision.reason })
  }
  const input = { rating: draft.rating, verdict: draft.verdict, comment: draft.comment, revisions }
  return isJsonValue(input) ? { input, error: null } : { input: null, error: { key: 'invalidDraft' } }
}

/** 不変 Result と送信内容を await 前に切り離し、transport ごとに copy を渡す。 */
export function freezeEvaluationSubmission(scope: EvaluationScope, key: string, data: unknown,
  input: CreateEvaluationInput | null): FrozenEvaluationSubmission {
  const payload = input === null ? null : { ...input, submission_key: key, result_id: scope.resultId }
  if (!Object.values(scope).every(isNonNilUuid) || !isNonNilUuid(key)
    || !isJsonValue(data) || payload !== null && !isEvaluationSubmissionInput(payload)) throw new Error('Invalid evaluation intent')
  return Object.freeze({ scope: Object.freeze({ ...scope }), key,
    body: payload === null ? null : JSON.stringify(payload), resultData: JSON.stringify(data) })
}

/** 原 payload だけを再送し、現在の草稿や受付記録から再生成しない。 */
export function evaluationPayload(request: FrozenEvaluationSubmission): EvaluationSubmissionInput {
  if (request.body === null) throw new Error('The original evaluation payload is unavailable')
  return JSON.parse(request.body) as EvaluationSubmissionInput
}

/** 独立受付記録の identity・原値・元入力をすべて照合してから確認済みにする。 */
export function matchesEvaluationReceipt(receipt: EvaluationSubmissionReceipt, request: FrozenEvaluationSubmission): boolean {
  const scope = request.scope
  const item = receipt.evaluation
  if (!sameUuid(receipt.project_id, scope.projectId) || !sameUuid(receipt.run_id, scope.runId)
    || !sameUuid(receipt.submission_key, request.key) || !sameUuid(item.run_id, scope.runId)
    || !sameUuid(item.result_id, scope.resultId) || !sameUuid(item.user_id, scope.actorId)) return false
  const data = JSON.parse(request.resultData) as unknown
  if (!item.revisions.every((revision) => {
    const original = evaluationOriginal(data, revision.pointer)
    return original.found && sameJsonValue(original.value, revision.original_value)
  })) return false
  if (request.body === null) return true
  const input = evaluationPayload(request)
  return item.rating === input.rating && item.verdict === input.verdict && item.comment === input.comment
    && item.revisions.length === input.revisions.length && item.revisions.every((revision, index) => {
      const expected = input.revisions[index]!
      return revision.pointer === expected.pointer && revision.reason === expected.reason
        && sameJsonValue(revision.suggested_value, expected.suggested_value)
    })
}

/** 旧ページで保存済み受付記録を上書きせず、不変 ID の矛盾も黙って採用しない。 */
export function mergeEvaluationRecords(previous: readonly EvaluationRecord[], incoming: readonly EvaluationRecord[]): EvaluationRecord[] {
  const merged = new Map(previous.map((item) => [item.evaluation_id.toLowerCase(), item]))
  for (const item of incoming) {
    const old = merged.get(item.evaluation_id.toLowerCase())
    if (old && !sameJsonValue(old, item)) throw new Error('Conflicting evaluation history')
    merged.set(item.evaluation_id.toLowerCase(), old ?? item)
  }
  return [...merged.values()]
}

/** 明確な身份/参照拒否のみ書込 gate を閉じ、通信失敗とは分ける。 */
export function evaluationAccessFailure(failure: EvaluationFailure | null | undefined): EvaluationFailure | null {
  return failure && ['sessionExpired', 'csrfRejected', 'forbidden', 'notFound', 'projectArchived', 'resultMismatch', 'resultUnavailable'].includes(failure.key) ? failure : null
}

/** 帰档/CSRF の書込拒否は読取権を奪わないが、現在の身份/対象拒否は確認も閉じる。 */
export function evaluationReadBlocked(failure: EvaluationFailure | null | undefined): boolean {
  return Boolean(failure && ['sessionExpired', 'forbidden', 'notFound', 'resultMismatch', 'resultUnavailable'].includes(failure.key))
}

/** 未分類の write 応答は成功でも rollback でもなく unknown。 */
export function evaluationFailure(error: unknown, mutation: boolean): EvaluationFailure {
  if (error instanceof ApiProblemError) {
    if (error.status === 401) return { key: 'sessionExpired' }
    if (error.status === 403) return { key: error.code === 'csrf_rejected' ? 'csrfRejected' : 'forbidden' }
    if (error.status === 404 && error.code === 'evaluation_submission_not_found') return { key: 'notSeen' }
    if (error.status === 404 && ['project_not_found', 'run_not_found'].includes(error.code ?? '')) return { key: 'notFound' }
    if (error.status === 409) {
      const keys = { project_archived: 'projectArchived', result_not_available: 'resultUnavailable',
        evaluation_result_mismatch: 'resultMismatch', evaluation_submission_conflict: 'conflict' } as const
      const key = keys[error.code as keyof typeof keys]
      if (key) return { key }
    }
    if (error.status === 400 && error.code === 'invalid_evaluation_revision') return { key: 'invalidRevision' }
    if (error.status === 422 && ['invalid_evaluation_request', 'validation_error'].includes(error.code ?? '')) return { key: 'invalidRequest' }
    if (error.status === 400 && error.code === 'invalid_evaluation_cursor') return { key: 'cursorInvalid' }
    if (!mutation && error.status === 503) return { key: 'unavailable' }
  }
  return { key: mutation ? 'unknown' : 'loadFailed' }
}

/** 同 tick/期限/旧応答の実装を評価側で作り直さない。 */
export const EVALUATION_REQUEST_POLICY: ResourceRequestPolicy<EvaluationFailure> = {
  classify: evaluationFailure, readTimeout: { key: 'readTimeout' }, writeTimeout: { key: 'unknown' },
  blocks: (failure) => failure.key === 'unknown' || failure.key === 'conflict',
}
