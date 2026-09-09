import { ApiProblemError, type InteractionAnswerInput, type RespondedInteractionRecord, type RunRecord, type UserInteractionDetail } from '../api'
import type { ResourceRequestPolicy } from '../hooks/useResourceRequest'
import { sameUuid as sameInteractionIdentity } from './validation'

// HTTP と画面で UUID の表記差を同じ規則で扱い、原要求の値自体は保持する。
export { sameInteractionIdentity }

/** 答復の原 actor と対象。Session token は key/body や公開表示へ含めない。 */
export interface InteractionScope {
  readonly actorId: string
  readonly projectId: string
  readonly runId: string
}

/** 編集可能な草稿と切り離した memory-only の原答復。 */
export interface FrozenInteractionResponse extends InteractionScope {
  readonly interactionId: string
  readonly version: number
  readonly body: string
  readonly idempotencyKey: string
}

/** 新規送信と同じ要求の確認を区別し、過去の unknown は既知拒否でも消さない。 */
export interface PendingInteractionResponse {
  request: FrozenInteractionResponse
  phase: 'sending' | 'unknown' | 'rejected' | 'conflict' | 'expired' | 'confirmed'
  uncertain: boolean
  failure: InteractionFailure | null
  receipt: RespondedInteractionRecord | null
}

/** Server 本文は公開せず、正確な Problem code だけを意味へ変換する。 */
export interface InteractionFailure {
  key: 'sessionExpired' | 'csrfRejected' | 'projectArchived' | 'forbidden' | 'notFound'
    | 'invalidAnswer' | 'conflict' | 'expired' | 'unknown' | 'loadFailed'
}

/** 明確な参照/Session 拒否だけを、同じ owner の書込資格へ伝える。 */
export interface InteractionAccessFailure {
  key: Extract<InteractionFailure['key'], 'sessionExpired' | 'csrfRejected' | 'projectArchived' | 'forbidden' | 'notFound'>
}

/** 一般の通信失敗を撤権と混同せず、翻訳済み文案からも意味を逆算しない。 */
export function interactionAccessFailure(failure: InteractionFailure | null | undefined): InteractionAccessFailure | null {
  switch (failure?.key) {
    case 'sessionExpired': case 'csrfRejected': case 'projectArchived': case 'forbidden': case 'notFound':
      return { key: failure.key }
    default: return null
  }
}

/** 同じ識別子の選択肢を一つへ潰さず、曖昧な歴史 CHOICE は読取専用にする。 */
export function hasDuplicateChoiceOptionKeys(interaction: UserInteractionDetail): boolean {
  if (interaction.interaction_type !== 'CHOICE') return false
  const keys = interaction.options.map((option) => option.key).filter((key): key is string => typeof key === 'string')
  return new Set(keys).size !== keys.length
}

/** 推薦/required=false を自動選択や空回答へ変換せず、曖昧な選択肢から新要求を作らない。 */
export function interactionAnswer(
  interaction: UserInteractionDetail, text: string, selected: readonly string[],
): InteractionAnswerInput | null {
  if (interaction.interaction_type === 'EFFECT_APPROVAL' || hasDuplicateChoiceOptionKeys(interaction)
    || [...text.trim()].length > 10_000) return null
  const answerText = text.trim()
  if (interaction.interaction_type !== 'CHOICE') return answerText ? { text: answerText } : null
  const allowed = new Set(interaction.options.map((option) => option.key))
  if (!selected.length || new Set(selected).size !== selected.length
    || selected.some((key) => !allowed.has(key))
    || (interaction.prompt.allow_multiple !== true && selected.length !== 1)) return null
  return { selected_option_keys: [...selected], ...(answerText ? { text: answerText } : {}) }
}

/** 新たに明示された一回だけ key を割り当て、配列順を含む原本文を固定する。 */
export function freezeInteractionResponse(
  scope: InteractionScope, interaction: UserInteractionDetail,
  answer: InteractionAnswerInput, idempotencyKey: string,
): FrozenInteractionResponse {
  return Object.freeze({ ...scope, interactionId: interaction.interaction_id,
    version: interaction.version, body: JSON.stringify(answer), idempotencyKey })
}

/** 原本文の copy を返し、草稿や前回 transport の mutation を次の確認へ持ち込まない。 */
export function interactionPayload(request: FrozenInteractionResponse): InteractionAnswerInput {
  return JSON.parse(request.body) as InteractionAnswerInput
}

/** 書込では未分類 HTTP/断連/解析失敗を rollback の証拠にしない。 */
export function interactionFailure(error: unknown, mutation: boolean): InteractionFailure {
  if (error instanceof ApiProblemError) {
    if (error.status === 401) return { key: 'sessionExpired' }
    if (error.status === 403 && error.code === 'csrf_rejected') return { key: 'csrfRejected' }
    if (error.status === 409 && error.code === 'project_archived') return { key: 'projectArchived' }
    if (error.status === 403) return { key: 'forbidden' }
    if (error.status === 404 && ['project_not_found', 'run_not_found'].includes(error.code ?? '')) return { key: 'notFound' }
    if (error.status === 409 && error.code === 'interaction_conflict') return { key: 'conflict' }
    if (error.status === 410 && error.code === 'interaction_expired') return { key: 'expired' }
    if (error.status === 422 && ['interaction_response_invalid', 'validation_error'].includes(error.code ?? '')) return { key: 'invalidAnswer' }
  }
  return { key: mutation ? 'unknown' : 'loadFailed' }
}

/** 同期防重/期限/旧応答の制御は既存 request hook を一つだけ利用する。 */
export const INTERACTION_REQUEST_POLICY: ResourceRequestPolicy<InteractionFailure> = {
  classify: interactionFailure,
  readTimeout: { key: 'loadFailed' },
  writeTimeout: { key: 'unknown' },
  blocks: (failure) => ['unknown', 'conflict', 'expired'].includes(failure.key),
}

/** 身份/権限の拒否を button 状態だけでなく domain guard としても維持する。 */
export function canConfirmInteraction(pending: PendingInteractionResponse): boolean {
  return ['unknown', 'rejected'].includes(pending.phase)
    && !interactionAccessFailure(pending.failure)
}

/** 原回执の現在 snapshot が SSE より古い場合、Run を過去状態へ戻さない。 */
export function applyInteractionSnapshot(current: RunRecord, response: RespondedInteractionRecord): RunRecord {
  if (!sameInteractionIdentity(current.run_id, response.run_id)
    || !sameInteractionIdentity(current.project_id, response.project_id)
    || response.row_version < current.row_version) return current
  return { ...current, status: response.status, row_version: response.row_version }
}
