// @vitest-environment jsdom
import { act, useEffect, useRef } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../../src/api'
import { WorkspacePage } from '../../src/pages/WorkspacePage'
import { TasksPage } from '../../src/pages/TasksPage'
import { HomePage } from '../../src/pages/HomePage'
import type { RunDetailState } from '../../src/components/RunResultPanel'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { taskCatalogId } from '../../src/lib/taskDraft'
import { documentTask } from '../fixtures/documentTask'
import { scheduleFixture } from '../fixtures/schedule'
import { interactionDetail, interactionRun } from '../fixtures/interaction'
import { DEMO_PROJECT } from '../fixtures'

vi.mock('../../src/api', async (load) => ({ ...await load<typeof import('../../src/api')>(),
  loadProjectTasks: vi.fn(), loadProjectModules: vi.fn(), loadProjectSchedules: vi.fn(), loadRun: vi.fn(), loadRunDetail: vi.fn(), loadRunHistory: vi.fn(), loadPendingRunPage: vi.fn(),
  subscribeRunEvents: vi.fn(() => ({ close: vi.fn() })) }))
const submissionStart = vi.fn()
vi.mock('../../src/hooks/useRunSubmission', () => ({ useRunSubmission: () => ({ pending: null, start: submissionStart, retry: vi.fn() }) }))
vi.mock('../../src/components/RunResultPanel', () => ({ RunResultPanel: ({ state, onRetryDetail }: { state: RunDetailState; onRetryDetail?: () => void }) => <div data-detail-state={state.status}><button data-detail-retry onClick={onRetryDetail}>Retry detail</button></div> }))
vi.mock('../../src/components/TaskScheduleDetails', () => ({ TaskScheduleDetails: function ScheduleDetail() { const ref = useRef<HTMLHeadingElement>(null); useEffect(() => ref.current?.focus(), []); return <h2 ref={ref} tabIndex={-1}>Schedule details</h2> } }))
let container: HTMLDivElement
let root: Root
/** 意図的に遅延した read を制御し、空態と待機を区別する。 */
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done }); return { promise, resolve } }
const task = () => ({ ...documentTask(false), readiness: null })
const catalog = () => ({ project_id: DEMO_PROJECT.project_id, tasks: [task()] })
const props = { actorId: 'actor', projectId: DEMO_PROJECT.project_id, moduleId: '', csrfToken: 'synthetic' }
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true); vi.clearAllMocks()
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  Element.prototype.scrollIntoView = vi.fn()
  vi.mocked(api.loadProjectTasks).mockResolvedValue(catalog())
  vi.mocked(api.loadProjectModules).mockResolvedValue([])
  vi.mocked(api.loadProjectSchedules).mockResolvedValue([])
  vi.mocked(api.loadRunHistory).mockResolvedValue({ items: [], limit: 10, offset: 0, has_more: false })
  vi.mocked(api.loadPendingRunPage).mockResolvedValue({ items: [], limit: 10, offset: 0, has_more: false })
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals() })
describe('Workspace audit page state', () => {
  it('shows catalog loading without a false empty state, and module failure without widening scope', async () => {
    const wait = deferred<Awaited<ReturnType<typeof api.loadProjectTasks>>>()
    vi.mocked(api.loadProjectTasks).mockReturnValueOnce(wait.promise)
    await act(async () => root.render(<WorkspacePage {...props} moduleId="unread-module" />))
    expect(container.querySelector('.runLauncher [role="status"]')?.getAttribute('aria-label')).toBe(MESSAGES.zh.tasks.loading)
    expect(container.textContent).not.toContain(MESSAGES.zh.workspace.noPublishedTasks)
    expect(container.textContent).not.toContain(MESSAGES.zh.workspace.noModuleTasks)
    vi.mocked(api.loadProjectModules).mockRejectedValueOnce(new Error('Module offline'))
    await act(async () => wait.resolve(catalog()))
    await act(async () => root.render(<WorkspacePage {...props} moduleId="unread-module" csrfToken="new-session" />))
    expect(container.textContent).toContain('Module offline')
    expect(container.textContent).not.toContain(MESSAGES.zh.workspace.noModuleTasks)
    expect([...container.querySelectorAll('button')].find((button) => button.textContent === MESSAGES.zh.workspace.openNewRun)?.disabled).toBe(true)
  })
  it('does not show project-wide tasks after a module read fails, and retries the exact scope', async () => {
    vi.mocked(api.loadProjectModules).mockRejectedValueOnce(new Error('Module offline'))
    await act(async () => root.render(<TasksPage {...props} moduleId="missing-module" />))
    expect(container.textContent).toContain('Module offline')
    expect(container.querySelector('[data-task-card]')).toBeNull()
    await act(async () => container.querySelector<HTMLButtonElement>('[data-task-refresh]')!.click())
    expect(container.textContent).toContain(MESSAGES.zh.uiAuditWorkspace.moduleUnavailable)
    expect(container.querySelector('[data-task-card]')).toBeNull()
  })
  it('disables new launches in archived projects including a direct launch link', async () => {
    await act(async () => root.render(<WorkspacePage {...props} projectReadOnly initialTaskId={taskCatalogId(task())} />))
    expect(container.textContent).toContain(MESSAGES.zh.uiAuditWorkspace.readOnlyLaunch)
    const submit = container.querySelector<HTMLButtonElement>('.runForm button[type="submit"]')!
    expect(submit.disabled).toBe(true)
    await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
    expect(submissionStart).not.toHaveBeenCalled()
    await act(async () => root.render(<TasksPage {...props} projectReadOnly />))
    const launch = container.querySelector<HTMLAnchorElement>('.taskCardActions .primaryButton')!
    expect(launch.getAttribute('href')).toBeNull()
    expect(launch.getAttribute('aria-disabled')).toBe('true')
  })
  it('retries terminal detail independently and displays the frozen original input and sources', async () => {
    const run = { ...interactionRun(), project_id: DEMO_PROJECT.project_id, status: 'SUCCEEDED' as const }
    vi.mocked(api.loadRun).mockResolvedValue(run)
    vi.mocked(api.loadRunDetail).mockRejectedValueOnce(new Error('Temporary detail failure')).mockResolvedValue({ ...interactionDetail(), ...run,
      input: { objective: 'Frozen original objective' }, selected_sources: { repository: { provider: 'original-repository' } }, task_title: 'Frozen task' })
    await act(async () => root.render(<WorkspacePage {...props} detailView initialRunId={run.run_id} />))
    expect(container.querySelector('[data-detail-state]')!.getAttribute('data-detail-state')).toBe('error')
    await act(async () => container.querySelector<HTMLButtonElement>('[data-detail-retry]')!.click())
    expect(api.loadRun).toHaveBeenCalledTimes(1)
    expect(api.loadRunDetail).toHaveBeenCalledTimes(2)
    expect(container.textContent).toContain('Frozen original objective')
    expect(container.textContent).toContain('original-repository')
    const conversation = [...container.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find((tab) => tab.textContent === MESSAGES.zh.workspace.tabConversation)!
    await act(async () => conversation.click())
    await act(async () => container.querySelector<HTMLButtonElement>('[data-detail-retry]')!.click())
    expect(conversation.getAttribute('aria-selected')).toBe('true')
    expect(container.querySelector('.conversation')!.getAttribute('tabindex')).toBe('0')
  })
  it('returns focus to the original schedule-management trigger', async () => {
    vi.mocked(api.loadProjectSchedules).mockResolvedValue([scheduleFixture()])
    await act(async () => root.render(<TasksPage {...props} currentProject={DEMO_PROJECT} />))
    const trigger = container.querySelector<HTMLButtonElement>('[data-task-schedule-manage]')!
    await act(async () => trigger.click())
    expect(document.activeElement?.textContent).toBe('Schedule details')
    await act(async () => container.querySelector<HTMLButtonElement>('[data-task-schedule-close]')!.click())
    expect(document.activeElement).toBe(trigger)
  })
  it('retries the recent-run section in place', async () => {
    vi.mocked(api.loadRunHistory).mockRejectedValueOnce(new Error('History unavailable'))
    await act(async () => root.render(<HomePage projectId={DEMO_PROJECT.project_id} project={DEMO_PROJECT} metaState={{ status: 'loading' }} />))
    expect(container.textContent).toContain('History unavailable')
    await act(async () => container.querySelector<HTMLButtonElement>('.homeRuns button')!.click())
    expect(api.loadRunHistory).toHaveBeenCalledTimes(2)
    expect(container.textContent).not.toContain('History unavailable')
  })
})
