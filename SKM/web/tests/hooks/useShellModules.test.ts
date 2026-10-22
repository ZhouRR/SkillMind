import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError, loadProjectModules, type ProjectModuleRecord } from '../../src/api'
import { useShellModules } from '../../src/hooks/useShellModules'
import { commitHooks, deferred, hookMicrotasks, hookPhases, unmountHooks } from '../fixtures/hookHarness'

vi.mock('react', async () => (await import('../fixtures/hookHarness')).hookReact)
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(), loadProjectModules: vi.fn() }))
let project = 'fixture-project'
let session = 'fixture-session'
let current = true
const ended = vi.fn()
/** 既存 UI の module 契約を満たす架空の記録。 */
const module: ProjectModuleRecord = {
  module_id: '00000000-0000-4000-8000-000000000070', project_id: '00000000-0000-4000-8000-000000000020',
  name: 'Review', description: '', skills: [],
  created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
}
/** shell と同じ会話判定を渡し、非同期結果だけを制御する。 */
function render() {
  hookPhases.cursor = 0
  return useShellModules(project, session, () => current, ended)
}
/** 現在の一覧応答を hook へ反映する。 */
async function settle() {
  for (let i = 0; i < 4; i++) { await hookMicrotasks(); render(); commitHooks() }
  return render()
}
beforeEach(() => {
  vi.resetAllMocks(); project = 'fixture-project'; session = 'fixture-session'; current = true
  vi.mocked(loadProjectModules).mockResolvedValue([module])
})
afterEach(() => { unmountHooks(); vi.restoreAllMocks() })

describe('shell module ownership', () => {
  it('selects the first module while preserving explicit selection on rerender', async () => {
    render(); commitHooks(); const value = await settle()
    expect(value.moduleId).toBe(module.module_id)
    value.select('chosen'); expect(render().moduleId).toBe('chosen')
    expect(loadProjectModules).toHaveBeenCalledTimes(1)
  })
  it('rejects an old selection and response before effect cleanup', async () => {
    const late = deferred<ProjectModuleRecord[]>()
    vi.mocked(loadProjectModules).mockReturnValueOnce(late.promise)
    const previous = render(); commitHooks()
    project = 'other-project'; render()
    previous.select('stale'); late.resolve([{ ...module, name: 'Old module' }]); await hookMicrotasks()
    expect(render().moduleId).toBe(''); commitHooks()
    expect((await settle()).modules[0]?.name).toBe('Review')
  })
  it('does not end a new session on a late 401 from the old session', async () => {
    const late = deferred<ProjectModuleRecord[]>()
    vi.mocked(loadProjectModules).mockReturnValueOnce(late.promise)
    render(); commitHooks(); session = 'new-session'; render()
    late.reject(new ApiProblemError('expired', 401)); await hookMicrotasks(); commitHooks(); await settle()
    expect(ended).not.toHaveBeenCalled()
  })
  it('retains navigation on a current module read failure', async () => {
    vi.mocked(loadProjectModules).mockRejectedValue(new Error('unavailable'))
    render(); commitHooks(); const value = await settle()
    expect(value.modules).toEqual([]); expect(ended).not.toHaveBeenCalled()
  })
  it('ends only the current unauthorized session', async () => {
    vi.mocked(loadProjectModules).mockRejectedValue(new ApiProblemError('expired', 401))
    render(); commitHooks(); await settle(); expect(ended).toHaveBeenCalledTimes(1)
  })
})
