import example from '../../../contracts/examples/task-schedule-activity.v1.json'

import type { ScheduleActivity } from '../../src/api'
import { scheduleFixture } from './schedule'

/** 公開 example を共有し、選択中 Schedule の合成 identity だけ揃える。 */
export function scheduleActivityFixture(overrides: Partial<ScheduleActivity> = {}): ScheduleActivity {
  const schedule = scheduleFixture()
  return { ...structuredClone(example), tracking: 'TRACKED', schedule_id: schedule.schedule_id,
    project_id: schedule.project_id, ...overrides }
}
