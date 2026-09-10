import { ApiProblemError, type PublishedTaskRecord, type ScheduleRecord } from '../api'
import type { ResourceRequestPolicy } from '../hooks/useResourceRequest'
import { sameUuid } from './validation'

/** 読取拒否は原書込の成否とは別に扱い、Server の本文を表示しない。 */
export interface ScheduleReadFailure { key: 'sessionExpired' | 'accessUnavailable' | 'loadFailed' }

/** 一覧・精確詳細・編集時の照会で同じ安全な分類を使う。 */
export function classifyScheduleReadFailure(error: unknown): ScheduleReadFailure {
  if (error instanceof ApiProblemError && error.status === 401) return { key: 'sessionExpired' }
  if (error instanceof ApiProblemError && [403, 404].includes(error.status)) return { key: 'accessUnavailable' }
  return { key: 'loadFailed' }
}

/** 共有 query に期限と拒否分類だけを渡し、並行 request engine を作らない。 */
export const SCHEDULE_READ_POLICY: ResourceRequestPolicy<ScheduleReadFailure> = {
  classify: classifyScheduleReadFailure,
  readTimeout: { key: 'loadFailed' }, writeTimeout: { key: 'loadFailed' },
  blocks: () => false,
}

/** 題名や latest で代替せず、唯一の精確 version/task だけを編集に利用する。 */
export function exactScheduleTask(schedule: ScheduleRecord, tasks: PublishedTaskRecord[]): PublishedTaskRecord | null {
  const matches = tasks.filter((task) => sameUuid(task.skill_version_id, schedule.skill_version_id)
    && task.task_key === schedule.task_key)
  return matches.length === 1 ? matches[0]! : null
}

/** Task の存在と自動実行の編集資格を分離し、未確認を実行可能と推測しない。 */
export function scheduleTaskEditability(task: PublishedTaskRecord | null): 'missing' | 'guidanceOnly' | 'unconfirmed' | 'ready' {
  if (!task) return 'missing'
  if (!task.readiness) return 'unconfirmed'
  return task.readiness.level === 'GUIDANCE_ONLY' ? 'guidanceOnly' : 'ready'
}

/** 全件数が減った場合だけ最後の有効頁へ戻す。空の一覧を削除の証拠としない。 */
export function schedulePageOffset(offset: number, total: number, limit: number): number {
  return offset === 0 || offset < total ? offset : Math.max(0, Math.ceil(total / limit) - 1) * limit
}
