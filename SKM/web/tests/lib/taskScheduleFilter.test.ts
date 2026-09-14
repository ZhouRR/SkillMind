import { describe, expect, it } from 'vitest'
import { matchesTaskScheduleFilter, schedulesForTask, unavailableScheduledTasks } from '../../src/lib/taskScheduleFilter'
import { documentTask } from '../fixtures/documentTask'
import { scheduleFixture } from '../fixtures/schedule'

const task = documentTask()

describe('task schedule filters', () => {
  it('matches literal title, task key and API task ID without interpreting patterns', () => {
    for (const q of ['DOCUMENT', 'analy', task.task_id.toUpperCase()]) {
      expect(matchesTaskScheduleFilter(task, [], q, '')).toBe(true)
    }
    expect(matchesTaskScheduleFilter(task, [], '.*', '')).toBe(false)
    expect(matchesTaskScheduleFilter(task, [], '%', '')).toBe(false)
  })
  it('requires both the name and state and matches any schedule of the same task', () => {
    const plans = [scheduleFixture(), scheduleFixture({ status: 'PAUSED' })]
    expect(matchesTaskScheduleFilter(task, plans, 'document', 'PAUSED')).toBe(true)
    expect(matchesTaskScheduleFilter(task, plans, 'other', 'PAUSED')).toBe(false)
    expect(matchesTaskScheduleFilter(task, plans, '', 'ERROR')).toBe(false)
  })
  it('distinguishes unconfigured from paused, completed and archived histories', () => {
    expect(matchesTaskScheduleFilter(task, [], '', 'UNCONFIGURED')).toBe(true)
    for (const status of ['PAUSED', 'COMPLETED', 'ARCHIVED'] as const) {
      const plans = [scheduleFixture({ status })]
      expect(matchesTaskScheduleFilter(task, plans, '', 'UNCONFIGURED')).toBe(false)
      expect(matchesTaskScheduleFilter(task, plans, '', status)).toBe(true)
    }
  })
  it('keeps exact versions and archived plans instead of attaching them to latest', () => {
    const current = scheduleFixture({ status: 'ARCHIVED' })
    const old = scheduleFixture({ skill_version_id: '00000000-0000-4000-8000-000000000099' })
    expect(schedulesForTask(task, [current, old])).toEqual([current])
    expect(unavailableScheduledTasks([task], [current, old])).toEqual([[old]])
    expect(matchesTaskScheduleFilter(null, [old], old.task_key, 'ACTIVE')).toBe(true)
  })
})
