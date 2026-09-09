import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { PublishedTaskRecord, ScheduleRecord, TaskLastRunRecord } from '../../src/api'
import { TasksPage, buildRows } from '../../src/pages/TasksPage'

const VERSION_A = '00000000-0000-4000-8000-000000000061'
const VERSION_B = '00000000-0000-4000-8000-000000000062'
const TASK_ID_A = '00000000-0000-4000-8000-0000000000a1'
const TASK_ID_B = '00000000-0000-4000-8000-0000000000a2'

/** テスト用の published task descriptor を生成する。 */
function task(overrides: Partial<PublishedTaskRecord> = {}): PublishedTaskRecord {
  return {
    skill_id: '00000000-0000-4000-8000-000000000060',
    skill_version_id: VERSION_A,
    skill_key: 'repository-review',
    skill_name: 'Repository Review',
    version: '1.0.0',
    task_key: 'review',
    task_id: TASK_ID_A,
    capability: 'repository.review/v1',
    title: '仓库评审',
    task_type: 'analysis',
    input_schema: { type: 'object' },
    output_schema: { type: 'object' },
    input_schema_checksum: `sha256:${'1'.repeat(64)}`,
    output_schema_checksum: `sha256:${'2'.repeat(64)}`,
    task_output_schema: null,
    task_output_schema_checksum: null,
    workflow: 'agent',
    view: 'standard',
    default_view: 'standard',
    compatibility_level: 'adapted',
    tool_requirements: [],
    published_at: '2026-07-20T00:00:00Z',
    readiness: { level: 'RUNNABLE', requirements: [] },
    last_run: null,
    ...overrides,
  }
}

/** テスト用の schedule record を生成する。 */
function schedule(overrides: Partial<ScheduleRecord> = {}): ScheduleRecord {
  return {
    schedule_id: '00000000-0000-4000-8000-000000000091',
    project_id: '00000000-0000-4000-8000-000000000020',
    name: 'nightly',
    kind: 'CRON',
    status: 'ACTIVE',
    timezone: 'Asia/Tokyo',
    cron_expression: '0 3 * * *',
    run_at: null,
    end_at: null,
    max_runs: null,
    skill_version_id: VERSION_A,
    task_key: 'review',
    input: {},
    sources: {},
    next_run_at: '2026-07-27T18:00:00Z',
    last_run_at: null,
    last_run_id: null,
    last_outcome: null,
    last_error: null,
    run_count: 0,
    missed_count: 0,
    created_by: '00000000-0000-4000-8000-000000000030',
    row_version: 1,
    created_at: '2026-07-26T09:00:00Z',
    updated_at: '2026-07-26T09:00:00Z',
    ...overrides,
  }
}

/** テスト用の最新 Run 要約を生成する。 */
function lastRun(overrides: Partial<TaskLastRunRecord> = {}): TaskLastRunRecord {
  return {
    run_id: '00000000-0000-4000-8000-0000000000b1',
    status: 'SUCCEEDED',
    created_at: '2026-07-25T09:00:00Z',
    finished_at: '2026-07-25T09:02:00Z',
    result_summary: '完成',
    ...overrides,
  }
}

describe('buildRows', () => {
  it('takes the last run from the descriptor instead of a history window', () => {
    // 以前はここで Run 履歴の先頭 N 件と突き合わせていたため、N 件より古い task が
    // 「未実行」と表示された——欠落ではなく誤った値。件数上限に依存しない形は server 側の投影だけ。
    const tasks = [
      task({ last_run: lastRun() }),
      task({ skill_version_id: VERSION_B, task_key: 'other', task_id: TASK_ID_B, title: '别的任务' }),
    ]
    const rows = buildRows(tasks, { tasks, schedules: [] })

    expect(rows[0]?.task.last_run?.status).toBe('SUCCEEDED')
    expect(rows[1]?.task.last_run).toBeNull()
  })

  it('matches schedules on the exact version and task key', () => {
    const tasks = [task()]
    const rows = buildRows(tasks, {
      tasks,
      schedules: [
        schedule(),
        schedule({ schedule_id: 'x', task_key: 'other' }),
        schedule({ schedule_id: 'y', skill_version_id: VERSION_B }),
      ],
    })

    expect(rows[0]?.schedules).toHaveLength(1)
  })

  it('hides archived schedules from the task row', () => {
    // 归档済みは「もう走らない」ので、行の「定时执行」件数に混ぜない。
    const tasks = [task()]
    const rows = buildRows(tasks, { tasks, schedules: [schedule({ status: 'ARCHIVED' })] })

    expect(rows[0]?.schedules).toHaveLength(0)
  })
})

describe('TasksPage', () => {
  it('asks for a project before showing anything else', () => {
    const html = renderToStaticMarkup(
      <TasksPage csrfToken={'s'.repeat(32)} moduleId="" projectId="" />,
    )

    expect(html).toContain('请先在左侧选择一个项目')
  })

  it('does not retain task configuration before a target is selected', () => {
    // task/Project の切替に草稿を持ち越さない。表面の三語は DocumentSources で実 task を渡して守る。
    const html = renderToStaticMarkup(
      <TasksPage csrfToken={'s'.repeat(32)} moduleId="" projectId="00000000-0000-4000-8000-000000000020" />,
    )

    expect(html).not.toContain('modalOverlay')
    expect(html).not.toContain('scheduleConfiguration')
    expect(html).toContain('data-schedules-manager-link')
    expect(html).toContain('href="#/schedules?project=00000000-0000-4000-8000-000000000020"')
  })
})
