import { describe, expect, it } from 'vitest'

import { ApiProblemError } from '../../src/api'
import { classifyScheduleReadFailure, exactScheduleTask, schedulePageOffset, scheduleTaskEditability } from '../../src/lib/scheduleManager'
import { documentTask } from '../fixtures/documentTask'
import { scheduleFixture } from '../fixtures/schedule'

describe('schedule read and exact task policy', () => {
  it.each([[401, 'sessionExpired'], [403, 'accessUnavailable'], [404, 'accessUnavailable'], [500, 'loadFailed'], [422, 'loadFailed']])(
    'does not expose the server body for HTTP %s', (status, key) => {
      expect(classifyScheduleReadFailure(new ApiProblemError('private proxy body', Number(status)))).toEqual({ key })
    },
  )
  it('classifies unknown transport failures without guessing absence', () => {
    expect(classifyScheduleReadFailure(new Error('private network body'))).toEqual({ key: 'loadFailed' })
  })
  it('matches UUID casing but never substitutes another task or latest version', () => {
    const task = { ...documentTask(), skill_version_id: 'abcdefab-0000-4000-8000-000000000061' }
    const schedule = scheduleFixture({ skill_version_id: task.skill_version_id.toUpperCase() })
    expect(exactScheduleTask(schedule, [task])).toBe(task)
    expect(exactScheduleTask(schedule, [{ ...task, task_key: 'replacement' }])).toBeNull()
    expect(exactScheduleTask(schedule, [{ ...task, skill_version_id: 'abcdefab-0000-4000-8000-000000000062' }])).toBeNull()
    expect(exactScheduleTask(schedule, [task, { ...task }])).toBeNull()
  })
  it('keeps task existence separate from proven editability', () => {
    const task = documentTask()
    expect(scheduleTaskEditability(null)).toBe('missing')
    expect(scheduleTaskEditability({ ...task, readiness: null })).toBe('unconfirmed')
    expect(scheduleTaskEditability({ ...task, readiness: { level: 'GUIDANCE_ONLY', requirements: [] } })).toBe('guidanceOnly')
    for (const level of ['RUNNABLE', 'ACTIONABLE', 'CONFIGURATION_REQUIRED'] as const) {
      expect(scheduleTaskEditability({ ...task, readiness: { level, requirements: [] } })).toBe('ready')
    }
  })
})

describe('server-total pagination', () => {
  it.each([[100, 101, 25, 100], [100, 100, 25, 75], [100, 70, 25, 50], [25, 0, 25, 0], [0, 0, 25, 0]])(
    'adjusts %s against %s records with page size %s', (offset, total, limit, expected) => {
      expect(schedulePageOffset(offset, total, limit)).toBe(expected)
    },
  )
})
