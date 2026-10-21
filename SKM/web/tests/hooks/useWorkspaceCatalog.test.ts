import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { loadProjectModules, loadProjectTasks } from '../../src/api'
import { useWorkspaceCatalog } from '../../src/hooks/useWorkspaceCatalog'
import { documentTask } from '../fixtures/documentTask'
import { commitHooks, deferred, hookMicrotasks, hookPhases, unmountHooks } from '../fixtures/hookHarness'

vi.mock('react', async () => (await import('../fixtures/hookHarness')).hookReact)
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(), loadProjectModules: vi.fn(), loadProjectTasks: vi.fn() }))
let project = 'fixture-project'
let session = {}
let revision = 0
/** Project/会話/再読取を実際の hook と同じ commit 境界で切り替える。 */
function render() {
  hookPhases.cursor = 0
  return useWorkspaceCatalog(project, session, revision, { selectProjectFirst: 'Select project', modulesFailed: 'Modules failed' })
}
/** 独立する二つの API 応答を model や timer を使わず収束させる。 */
async function settle() {
  for (let i = 0; i < 4; i++) { await hookMicrotasks(); render(); commitHooks() }
  return render()
}
beforeEach(() => {
  vi.resetAllMocks(); project = 'fixture-project'; session = {}; revision = 0
  vi.mocked(loadProjectTasks).mockResolvedValue({ tasks: [documentTask()] })
  vi.mocked(loadProjectModules).mockResolvedValue([])
})
afterEach(() => { unmountHooks(); vi.restoreAllMocks() })

describe('workspace catalog ownership', () => {
  it('keeps task reads independent of failed module reads', async () => {
    vi.mocked(loadProjectModules).mockRejectedValue(new Error('module unavailable'))
    render(); commitHooks(); const value = await settle()
    expect(value.tasks).toHaveLength(1); expect(value.tasksLoaded).toBe(true)
    expect(value.modulesLoaded).toBe(false); expect(value.modulesError).toBe('module unavailable')
  })
  it.each(['project', 'session', 'reload'] as const)('discards late responses across %s changes', async (kind) => {
    const late = deferred<Awaited<ReturnType<typeof loadProjectTasks>>>()
    vi.mocked(loadProjectTasks).mockReturnValueOnce(late.promise)
    render(); commitHooks()
    if (kind === 'project') project = 'other-project'
    if (kind === 'session') session = {}
    if (kind === 'reload') revision++
    const changing = render()
    late.resolve({ tasks: [{ ...documentTask(), title: 'Stale task' }] })
    await hookMicrotasks()
    expect(changing.tasksLoaded).toBe(false)
    commitHooks(); const value = await settle()
    expect(value.tasks[0]?.title).toBe('Document analysis')
    expect(vi.mocked(loadProjectTasks).mock.calls[0]?.[1]?.aborted).toBe(true)
  })
  it('does not fetch without an explicit project', async () => {
    project = ''; const first = render(); commitHooks(); const value = await settle()
    expect(value.tasksError).toBe('Select project')
    // 未取得の一覧も参照を保ち、依存する effect の再実行を増やさない。
    expect(value.tasks).toBe(first.tasks); expect(value.modules).toBe(first.modules)
    expect(loadProjectTasks).not.toHaveBeenCalled(); expect(loadProjectModules).not.toHaveBeenCalled()
  })
  it('aborts both reads on unmount and ignores their late errors', async () => {
    const late = deferred<Awaited<ReturnType<typeof loadProjectTasks>>>()
    vi.mocked(loadProjectTasks).mockReturnValueOnce(late.promise)
    render(); commitHooks()
    const signal = vi.mocked(loadProjectTasks).mock.calls[0]?.[1]
    unmountHooks(); late.reject(new Error('late failure')); await hookMicrotasks()
    expect(signal?.aborted).toBe(true)
  })
})
