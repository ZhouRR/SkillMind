import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useSchedules } from '../../src/hooks/useSchedules'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES, type UiLanguage } from '../../src/lib/i18n/messages'
import { ScheduleDetails, SchedulesPage } from '../../src/pages/SchedulesPage'
import { DEMO_PROJECT, demoUser } from '../fixtures'
import { documentTask } from '../fixtures/documentTask'
import { scheduleFixture } from '../fixtures/schedule'
import { scheduleActivityFixture } from '../fixtures/scheduleActivity'

type ManagerState = ReturnType<typeof useSchedules>
const manager = vi.hoisted(() => ({ state: null as ManagerState | null }))
vi.mock('../../src/hooks/useSchedules', () => ({ useSchedules: () => manager.state }))

/** UI の投影だけを検証する。実 request 世代は useSchedules tests が検証する。 */
function query<T>(data: T) { return { data, pending: false, failure: null, completed: 0, revision: 0, refresh: vi.fn() } }
/** 本番 shape の全件数と精確詳細を用い、上限外履歴を一覧から切り落とさない。 */
function state(overrides: Partial<ManagerState> = {}): ManagerState {
  const record = scheduleFixture()
  return {
    filter: { q: '', status: '', offset: 100, revision: 0 }, selection: { id: record.schedule_id, revision: 1 }, limit: 25,
    list: query({ schedules: [record], total: 101, limit: 25, offset: 100 }), catalog: query({ tasks: [documentTask()] }),
    detail: query(record), activity: query(scheduleActivityFixture()), record, task: documentTask(), taskEligibility: 'ready', readDenied: null, canWrite: true,
    canEdit: vi.fn(() => true), select: vi.fn(), search: vi.fn(), turnPage: vi.fn(), refreshDetail: vi.fn(), refreshFacts: vi.fn(), refreshCatalog: vi.fn(), ...overrides,
  }
}
/** 同じ元記録を三語で描画し、歴史の意味と機密値の非表示を確認する。 */
function render(node: ReactNode, language: UiLanguage = 'zh'): string {
  return renderToStaticMarkup(<LanguageProvider language={language}>{node}</LanguageProvider>)
}
/** actor/Project は synthetic fixture の現在所有者だけを渡す。 */
function page(project = DEMO_PROJECT) {
  return <SchedulesPage projectId={DEMO_PROJECT.project_id} currentProject={project} actorId={demoUser().user_id} csrfToken="synthetic-csrf" />
}
beforeEach(() => { manager.state = state() })

describe('project-wide schedule management', () => {
  it.each(['zh', 'ja', 'en'] as const)('shows full server total and the last page in %s', (language) => {
    const html = render(page(), language)
    const labels = MESSAGES[language].scheduleManager
    expect(html).toContain(labels.total(101))
    expect(html).toContain(labels.page(100, 25, 101))
    expect(html).not.toContain(labels.scopeHint)
    expect(html).toContain('data-schedule-next="true" disabled=""')
    expect(html).toContain('data-schedule-row=')
    expect(html).toContain(`class="scheduleRows" tabindex="0" role="list" aria-label="${labels.listTitle}"`)
    expect(html).toContain('value="ARCHIVED"')
    expect(html).not.toContain('synthetic-csrf')
  })
  it('keeps archived rules and missing exact tasks readable', () => {
    const record = scheduleFixture({ status: 'ARCHIVED' })
    manager.state = state({ record, detail: query(record), task: null, taskEligibility: 'missing', canWrite: false })
    const html = render(page())
    expect(html).toContain(record.name)
    expect(html).toContain(MESSAGES.zh.scheduleManager.taskUnavailable)
    expect(html).toContain('data-schedule-edit="true" disabled=""')
  })
  it('does not erase schedule details when the catalog read fails', () => {
    manager.state = state({ catalog: { ...query({ tasks: [] }), failure: { key: 'loadFailed' } }, task: null, taskEligibility: 'missing', canWrite: false })
    const html = render(page())
    expect(html).toContain(scheduleFixture().name)
    expect(html).toContain(MESSAGES.zh.scheduleManager.catalogUnavailable)
    expect(html).not.toContain(MESSAGES.zh.scheduleManager.taskUnavailable)
  })
  it.each(['guidanceOnly', 'unconfirmed'] as const)('retains the exact task name while %s disables edits', (taskEligibility) => {
    manager.state = state({ taskEligibility, canWrite: false })
    const html = render(page())
    expect(html).toContain(documentTask().title)
    expect(html).toContain(MESSAGES.zh.scheduleManager[taskEligibility === 'guidanceOnly' ? 'guidanceOnly' : 'readinessUnconfirmed'])
    expect(html).not.toContain(MESSAGES.zh.scheduleManager.taskUnavailable)
    expect(html).toContain('data-schedule-edit="true" disabled=""')
  })
  it('marks an archived Project read-only without hiding its list', () => {
    const html = render(page({ ...DEMO_PROJECT, status: 'ARCHIVED' }))
    expect(html).toContain('data-schedule-row=')
    expect(html).toContain(MESSAGES.zh.scheduleManager.readOnlyProject)
    expect(html).toContain('data-schedule-edit="true" disabled=""')
  })
  it('closes writing after an independent list access rejection', () => {
    manager.state = state({ canWrite: false, readDenied: { key: 'accessUnavailable' } })
    const html = render(page())
    expect(html).toContain('data-schedule-read-denied')
    expect(html).toContain('data-schedule-edit="true" disabled=""')
  })
  it('does not render another Project as the selected authorized context', () => {
    const html = render(page({ ...DEMO_PROJECT, project_id: 'abcdefab-0000-4000-8000-000000000099' }))
    expect(html).toContain(MESSAGES.zh.scheduleManager.needProject)
    expect(html).not.toContain('data-schedule-row=')
  })
})

describe('original schedule facts', () => {
  it.each(['zh', 'ja', 'en'] as const)('separates source/input from counts and incomplete summaries in %s', (language) => {
    const record = scheduleFixture({ input: { objective: '<script>not executable</script>' } })
    const html = render(<ScheduleDetails schedule={record} />, language)
    const labels = MESSAGES[language].scheduleManager
    expect(html).toContain(labels.summaryHint)
    expect(html).toContain(labels.fields.runCount)
    expect(html).toContain(labels.outcomes.SKIPPED_OVERLAP)
    expect(html).toContain('UTC+09:00')
    expect(html).toContain(record.skill_version_id)
    expect(html).toContain(record.last_run_id)
    expect(html).toContain('&lt;script&gt;not executable&lt;/script&gt;')
    expect(html).not.toContain('<script>')
  })
})
