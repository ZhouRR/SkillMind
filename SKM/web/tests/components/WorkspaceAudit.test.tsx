// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../../src/api'
import { WorkspaceQueue } from '../../src/components/WorkspaceQueue'
import { PendingActionsPanel } from '../../src/components/PendingActionsPanel'
import { RunDeletionDialog } from '../../src/components/RunDeletionDialog'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { interactionRun } from '../fixtures/interaction'

vi.mock('../../src/api', async (load) => ({ ...await load<typeof import('../../src/api')>(),
  loadRunHistory: vi.fn(), loadPendingRunPage: vi.fn(), previewRunDeletion: vi.fn(), changeRunDeletion: vi.fn() }))
vi.mock('../../src/components/WorkspaceReports', () => ({ WorkspaceReports: () => <p>Reports</p> }))
let container: HTMLDivElement
let root: Root
/** 完了を制御できる読取で、refresh 中の DOM 継続を検証する。 */
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done }); return { promise, resolve } }
/** 実業務データを含まない待機履歴。 */
function historyItem(): api.RunHistoryItemRecord { return { ...interactionRun(), task_title: 'Frozen long task title', input: {}, selected_sources: {}, started_at: null, finished_at: null, result_summary: null, result_confidence: null, result_needs_review: null } }
const page = () => ({ items: [historyItem()], has_more: false, limit: 10, offset: 0 })
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.clearAllMocks()
  vi.mocked(api.loadRunHistory).mockResolvedValue(page())
  vi.mocked(api.loadPendingRunPage).mockResolvedValue(page())
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.useRealTimers(); vi.unstubAllGlobals() })
describe('Workspace queue and pending UI', () => {
  it('retains the focused link through an automatic refresh and failed refresh', async () => {
    vi.useFakeTimers()
    await act(async () => root.render(<WorkspaceQueue projectId={historyItem().project_id} tasks={[]} csrfToken="synthetic" actorId="actor" projectReadOnly={false} onSessionExpired={vi.fn()} />))
    const link = container.querySelector<HTMLAnchorElement>('.pendingItem')!
    link.focus()
    const refresh = deferred<api.RunHistoryPageRecord>()
    vi.mocked(api.loadRunHistory).mockReturnValueOnce(refresh.promise)
    await act(async () => vi.advanceTimersByTime(30000))
    expect(container.querySelector('.pendingItem')).toBe(link)
    expect(document.activeElement).toBe(link)
    expect(container.textContent).toContain(MESSAGES.zh.uiAuditWorkspace.updating)
    await act(async () => refresh.resolve(page()))
    expect(container.querySelector('.pendingItem')).toBe(link)
    vi.mocked(api.loadRunHistory).mockRejectedValueOnce(new Error('Read unavailable'))
    await act(async () => vi.advanceTimersByTime(30000))
    expect(container.querySelector('.pendingItem')).toBe(link)
    expect(container.textContent).toContain('Read unavailable')
  })
  it('implements roving keyboard tabs and matching panel relationships', async () => {
    await act(async () => root.render(<WorkspaceQueue projectId={historyItem().project_id} tasks={[]} csrfToken="synthetic" actorId="actor" projectReadOnly={false} onSessionExpired={vi.fn()} />))
    const tabs = [...container.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
    expect(tabs.map((tab) => tab.tabIndex)).toEqual([0, -1, -1])
    for (const [key, index] of [['ArrowRight', 1], ['End', 2], ['Home', 0]] as const) {
      const current = container.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]')!
      await act(async () => current.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true })))
      expect(document.activeElement).toBe(tabs[index])
      expect(tabs[index]!.getAttribute('aria-selected')).toBe('true')
      const panel = document.getElementById(tabs[index]!.getAttribute('aria-controls')!)!
      expect(panel.getAttribute('aria-labelledby')).toBe(tabs[index]!.id)
      expect(panel.hidden).toBe(false)
    }
  })
  it('shows bounded pending count, view-all entry, and retry after an error', async () => {
    vi.mocked(api.loadPendingRunPage).mockRejectedValueOnce(new Error('Temporary failure'))
    await act(async () => root.render(<PendingActionsPanel projectId={historyItem().project_id} />))
    expect(container.textContent).toContain('Temporary failure')
    vi.mocked(api.loadPendingRunPage).mockResolvedValue({ ...page(), items: Array.from({ length: 10 }, (_, index) => ({ ...historyItem(), run_id: `run-${index}` })), has_more: true })
    await act(async () => container.querySelector<HTMLButtonElement>('button')!.click())
    expect(container.querySelector('.eventCount')!.textContent).toBe('10+')
    expect(container.textContent).toContain(MESSAGES.zh.uiAuditWorkspace.pendingTruncated)
    expect(container.querySelector('a')!.getAttribute('href')).toContain('#/workspace?project=')
  })
  it('isolates a new project from retained pending items before its read resolves', async () => {
    await act(async () => root.render(<PendingActionsPanel projectId={historyItem().project_id} />))
    expect(container.textContent).toContain('Frozen long task title')
    const next = deferred<api.RunHistoryPageRecord>()
    vi.mocked(api.loadPendingRunPage).mockReturnValueOnce(next.promise)
    await act(async () => root.render(<PendingActionsPanel projectId="another-project" />))
    expect(container.textContent).not.toContain('Frozen long task title')
    await act(async () => next.resolve({ ...page(), items: [] }))
    expect(container.textContent).toContain(MESSAGES.zh.pending.empty)
  })
  it('shows the frozen run target and collapses large deletion output lists', async () => {
    vi.mocked(api.previewRunDeletion).mockResolvedValue({ run_id: historyItem().run_id,
      output_count: 12, protected_output_count: 1, cleanup_pending: 0, deleted: false, outputs: Array.from({ length: 12 }, (_, index) => ({ document_id: `doc-${index}`, name: `file-${index}.md`, folder: 'reports', protected: index === 0 })) })
    await act(async () => root.render(<RunDeletionDialog projectId={historyItem().project_id} runId={historyItem().run_id} run={historyItem()} csrfToken="synthetic" action="TRASH" onClose={vi.fn()} onChanged={vi.fn()} />))
    expect(container.textContent).toContain('Frozen long task title')
    expect(container.textContent).toContain(historyItem().run_id)
    expect(container.querySelector('time')!.dateTime).toBe(historyItem().created_at)
    expect(container.querySelector('details')!.open).toBe(false)
    expect(container.querySelector('.runDeletionOutputs ul')!.getAttribute('tabindex')).toBe('0')
    expect(api.changeRunDeletion).not.toHaveBeenCalled()
  })
})
