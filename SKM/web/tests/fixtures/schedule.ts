import type { ScheduleRecord } from '../../src/api'
import { ALL_DOCUMENTS_SELECTION } from '../../src/lib/documentSelection'
import { DEMO_PROJECT } from '../fixtures'
import { documentTask } from './documentTask'

/** 公開 metadata をすべて持ち、実際の Project や予定へ接続しない合成記録。 */
export function scheduleFixture(overrides: Partial<ScheduleRecord> = {}): ScheduleRecord {
  return {
    schedule_id: 'abcdefab-0000-4000-8000-000000000091', project_id: DEMO_PROJECT.project_id,
    name: 'Original schedule', kind: 'CRON', status: 'ACTIVE', timezone: 'Asia/Tokyo', cron_expression: '0 3 * * *',
    run_at: null, end_at: null, max_runs: 10, skill_version_id: documentTask().skill_version_id,
    task_key: documentTask().task_key, input: { objective: 'Original objective' }, sources: { documents: ALL_DOCUMENTS_SELECTION },
    next_run_at: '2035-01-01T18:00:00Z', last_run_at: '2034-12-31T18:00:00Z',
    last_run_id: 'abcdefab-0000-4000-8000-000000000301', last_outcome: 'SKIPPED_OVERLAP', last_error: null,
    run_count: 2, missed_count: 3, created_by: 'abcdefab-0000-4000-8000-000000000030', row_version: 7,
    created_at: '2034-12-20T00:00:00Z', updated_at: '2034-12-31T18:00:01Z', ...overrides,
  }
}
