import type { PublishedTaskRecord, ScheduleRecord, ScheduleStatus } from '../api'
import { sameUuid } from './validation'

/** 一つの task に複数状態が共存する。未設定は archived を含む履歴もない場合に限る。 */
export type TaskScheduleStatusFilter = '' | 'UNCONFIGURED' | ScheduleStatus

/** exact version/task で結び、最新 Skill へ過去の予定を移し替えない。 */
export function schedulesForTask(task: Pick<PublishedTaskRecord, 'skill_version_id' | 'task_key'>, schedules: ScheduleRecord[]): ScheduleRecord[] {
  return schedules.filter((schedule) => sameUuid(schedule.skill_version_id, task.skill_version_id) && schedule.task_key === task.task_key)
}

/** 名称と task key/ID の literal 部分一致。正規表現やワイルドカードとして解釈しない。 */
export function matchesTaskScheduleFilter(task: Pick<PublishedTaskRecord, 'title' | 'task_key' | 'task_id'> | null,
  schedules: ScheduleRecord[], query: string, status: TaskScheduleStatusFilter): boolean {
  const term = query.trim().toLocaleLowerCase()
  const names = task ? [task.title, task.task_key, task.task_id] : schedules.flatMap((schedule) => [schedule.name, schedule.task_key])
  if (term && !names.some((name) => name.toLocaleLowerCase().includes(term))) return false
  return status === '' || (status === 'UNCONFIGURED' ? schedules.length === 0 : schedules.some((schedule) => schedule.status === status))
}

/** Catalog から消えた元 task の予定も、停止・原設定の核対ができる単位にまとめる。 */
export function unavailableScheduledTasks(tasks: PublishedTaskRecord[], schedules: ScheduleRecord[]): ScheduleRecord[][] {
  const groups = new Map<string, ScheduleRecord[]>()
  for (const schedule of schedules) {
    if (tasks.some((task) => sameUuid(task.skill_version_id, schedule.skill_version_id) && task.task_key === schedule.task_key)) continue
    const key = `${schedule.skill_version_id.toLowerCase()}::${schedule.task_key}`
    groups.set(key, [...(groups.get(key) ?? []), schedule])
  }
  return [...groups.values()]
}
