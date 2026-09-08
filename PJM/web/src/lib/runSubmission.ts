import { ApiProblemError, type CreateTaskRunInput } from '../api'
import type { TaskDraft } from './taskDraft'

/** 作成要求を送った利用者と Project。認証情報そのものは保存しない。 */
export interface RunSubmissionScope {
  readonly actorId: string
  readonly projectId: string
}

/** 編集中の草稿から切り離した一回の送信。本文を文字列に固定し、可変 object を共有しない。 */
export interface FrozenRunSubmission extends RunSubmissionScope {
  readonly idempotencyKey: string
  readonly body: string
  readonly taskTitle: string
  readonly capability: string
}

/** 未確認の送信状態。再試行の拒否は、それ以前の不明な commit を否定しない。 */
export interface PendingRunSubmission {
  readonly request: FrozenRunSubmission
  readonly phase: 'sending' | 'unknown' | 'rejected' | 'conflict'
  readonly mayHaveCreated: boolean
  readonly detail: string | null
}

/** 新しいユーザー意図だけを固定する。server の hash や resource 展開規則は再実装しない。 */
export function freezeRunSubmission(
  scope: RunSubmissionScope,
  draft: TaskDraft,
  capability: string,
  idempotencyKey: string,
): FrozenRunSubmission {
  if (!scope.actorId || !scope.projectId || !idempotencyKey) {
    throw new Error('Run submission requires an actor, project and request key')
  }
  const payload: CreateTaskRunInput = {
    skill_version_id: draft.skillVersionId,
    task_key: draft.taskKey,
    input: draft.input,
    sources: draft.sources,
  }
  return Object.freeze({
    actorId: scope.actorId,
    projectId: scope.projectId,
    idempotencyKey,
    body: JSON.stringify(payload),
    taskTitle: draft.taskTitle,
    capability,
  })
}

/** 自分で固定した本文の copy を返す。草稿や過去の呼出し側による mutation を再送へ持ち込まない。 */
export function submissionPayload(request: FrozenRunSubmission): CreateTaskRunInput {
  return JSON.parse(request.body) as CreateTaskRunInput
}

/** CSRF の更新と actor/Project の変更を区別し、他の作成身份へ再送しない。 */
export function submissionBelongsTo(request: FrozenRunSubmission, scope: RunSubmissionScope): boolean {
  return request.actorId === scope.actorId && request.projectId === scope.projectId
}

/** 未確認の要求を置き換えるには明示確認を要し、送信中の二重開始は常に拒否する。 */
export function canStartRunSubmission(
  pending: PendingRunSubmission | null,
  scope: RunSubmissionScope,
  acknowledgePrevious: boolean,
): boolean {
  if (!scope.actorId || !scope.projectId) return false
  return pending === null || (
    submissionBelongsTo(pending.request, scope)
    && pending.phase !== 'sending'
    && acknowledgePrevious
  )
}

/** 再送時にも、それまでに結果不明となった事実と原要求を維持する。 */
export function sendingRunSubmission(
  request: FrozenRunSubmission,
  previous: PendingRunSubmission | null = null,
): PendingRunSubmission {
  return {
    request,
    phase: 'sending',
    mayHaveCreated: previous?.request === request && previous.mayHaveCreated,
    detail: null,
  }
}

/** HTTP 拒否と commit 不明を分ける。2xx の解析失敗・timeout・5xx は未作成の証拠ではない。 */
export function failedRunSubmission(
  pending: PendingRunSubmission,
  error: unknown,
): PendingRunSubmission {
  const phase = error instanceof ApiProblemError && error.status === 409
    ? 'conflict'
    : error instanceof ApiProblemError
      && error.status >= 400 && error.status < 500
      && error.status !== 408 && error.status !== 429
      ? 'rejected'
      : 'unknown'
  return {
    request: pending.request,
    phase,
    mayHaveCreated: pending.mayHaveCreated || phase === 'unknown' || phase === 'conflict',
    // Transport/JSON error は実装詳細を描画せず、表示層の三語案内に集約する。
    detail: error instanceof ApiProblemError ? error.message : null,
  }
}
