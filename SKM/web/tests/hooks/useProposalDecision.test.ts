import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError, decideChangeProposal, loadRunDetail } from '../../src/api'
import { useProposalDecision } from '../../src/hooks/useProposalDecision'
import { freezeProposalDecision, observeProposalDecision, proposalFailurePhase, restoreProposalDecision, saveProposalDecision } from '../../src/lib/proposalDecision'
import { approvalFixture, executionDetail, PROPOSAL_SCOPE as S, proposalFixture, proposalReceipt } from '../fixtures/executionAudit'
import { commitHooks, deferred, hookMicrotasks, hookPhases, unmountHooks } from '../fixtures/hookHarness'

vi.mock('react', async () => (await import('../fixtures/hookHarness')).hookReact)
vi.mock('../../src/components/ProposalDecisionOwner', () => ({ useProposalDecisionMemory: () => null,
  proposalMemoryKey: (scope: typeof S) => JSON.stringify(scope) }))
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(),
  decideChangeProposal: vi.fn(), loadRunDetail: vi.fn() }))

const saved = vi.fn()
const expired = vi.fn()
let storage: Map<string, string>

/** Hook の owner を保ったまま props 更新を明示する。 */
function render(overrides: Partial<Parameters<typeof useProposalDecision>[0]> = {}) {
  hookPhases.cursor = 0
  return useProposalDecision({ scope: S, proposal: proposalFixture(), csrfToken: 'synthetic-session', writable: true,
    onDecided: saved, onSessionExpired: expired, ...overrides })
}

/** 読取/書込 promise だけを完了させ、期限は個別テストから進める。 */
async function settle(overrides: Partial<Parameters<typeof useProposalDecision>[0]> = {}) {
  await hookMicrotasks(); const current = render(overrides); commitHooks(); return current
}

beforeEach(() => {
  vi.clearAllMocks(); vi.useFakeTimers()
  storage = new Map()
  vi.stubGlobal('window', { sessionStorage: {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  } })
})
afterEach(() => { unmountHooks(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals() })

describe('proposal original approval identity', () => {
  it('submits once per owner and freezes the original decision, reason, version and key', async () => {
    vi.mocked(decideChangeProposal).mockImplementation(async (_p, _r, _proposal, decision, reason) => proposalReceipt(decision, reason))
    const hook = render(); commitHooks()
    void hook.start('APPROVED', '  Original reason  '); void hook.start('REJECTED', 'New reason')
    const current = await settle()
    expect(decideChangeProposal).toHaveBeenCalledTimes(1)
    expect(current.pending).toMatchObject({ phase: 'confirmed', request: { ...S, version: 4, checksum: proposalFixture().checksum,
      decision: 'APPROVED', reason: 'Original reason', key: expect.any(String) } })
    expect(saved).toHaveBeenCalledTimes(1)
    expect(storage.size).toBe(1)
    expect([...storage.values()][0]).not.toContain('synthetic-session')
    void current.start('REJECTED', 'Change after save'); await settle()
    expect(decideChangeProposal).toHaveBeenCalledTimes(1)
  })

  it('keeps an unknown original request through repeated GETs and never treats GET as its receipt', async () => {
    vi.mocked(decideChangeProposal).mockRejectedValue(new TypeError('network'))
    const hook = render(); commitHooks(); void hook.start('APPROVED', 'Original reason')
    let current = await settle(); const original = current.pending!.request
    expect(current.pending?.phase).toBe('unknown')
    vi.mocked(loadRunDetail).mockResolvedValue(executionDetail())
    void current.check(); void current.check(); void current.start('REJECTED', 'Edited')
    current = await settle({ proposal: { ...proposalFixture(), version: 5, checksum: 'new-checksum' } })
    expect(loadRunDetail).toHaveBeenCalledTimes(1)
    expect(current.pending).toMatchObject({ phase: 'unknown', request: original })
    vi.mocked(loadRunDetail).mockResolvedValue({ ...executionDetail(), approvals: [approvalFixture()] })
    void current.check(); current = await settle()
    expect(current.pending).toMatchObject({ phase: 'observed', request: original })
    expect(saved).not.toHaveBeenCalled()
    expect(decideChangeProposal).toHaveBeenCalledTimes(1)
  })

  it('restores the same request after unmount without resending, including in a read-only project', async () => {
    vi.mocked(decideChangeProposal).mockRejectedValue(new TypeError('network'))
    const hook = render(); commitHooks(); void hook.start('REJECTED', 'Keep unchanged')
    const current = await settle(); const original = current.pending!.request
    unmountHooks()
    const recovered = render({ writable: false }); commitHooks()
    expect(recovered.pending).toMatchObject({ phase: 'unknown', request: original })
    vi.mocked(loadRunDetail).mockResolvedValue({ ...executionDetail(), approvals: [approvalFixture('REJECTED', 'Keep unchanged')] })
    void recovered.check(); const checked = await settle({ writable: false })
    expect(checked.pending?.phase).toBe('observed')
    expect(decideChangeProposal).toHaveBeenCalledTimes(1)
    expect(loadRunDetail).toHaveBeenCalledTimes(1)
  })

  it('isolates saved requests between actors, projects and runs', () => {
    const request = freezeProposalDecision(S, proposalFixture(), 'APPROVED', 'Original reason', 'original-key')
    expect(saveProposalDecision(request)).toBe(true)
    expect(restoreProposalDecision(S).pending?.request).toEqual(request)
    for (const field of ['actorId', 'projectId', 'runId', 'proposalId'] as const) {
      expect(restoreProposalDecision({ ...S, [field]: 'different-id' }).pending).toBeNull()
    }
  })

  it.each([new TypeError('offline'), new Error('malformed success'), new ApiProblemError('gateway', 502),
    new ApiProblemError('non-json success', 200)])('classifies uncertain transport or response as unknown: %s', (error) => {
    expect(proposalFailurePhase(error)).toBe('unknown')
  })

  it.each([400, 401, 403, 404, 410, 422])('distinguishes an explicit %s refusal without allowing another write', async (status) => {
    vi.mocked(decideChangeProposal).mockRejectedValue(new ApiProblemError('refused', status))
    const hook = render(); commitHooks(); void hook.start('APPROVED', 'Original reason')
    const current = await settle(); expect(current.pending?.phase).toBe('rejected')
    void current.start('REJECTED', 'New reason'); await settle()
    expect(decideChangeProposal).toHaveBeenCalledTimes(1)
  })

  it('keeps conflicting decisions separate from a matching observation', () => {
    const request = freezeProposalDecision(S, proposalFixture(), 'APPROVED', 'Original reason', 'original-key')
    expect(observeProposalDecision({ ...executionDetail(), approvals: [approvalFixture('REJECTED')] }, request).phase).toBe('conflict')
    for (const patch of [{ proposal_version: 99 }, { proposal_checksum: 'other' }, { proposal_id: 'other' }, { run_id: 'other' }]) {
      expect(observeProposalDecision({ ...executionDetail(), approvals: [{ ...approvalFixture(), ...patch }] }, request).phase).toBe('unknown')
    }
    expect(observeProposalDecision({ ...executionDetail(), project_id: 'other', approvals: [approvalFixture()] }, request).phase).toBe('unknown')
    expect(observeProposalDecision({ ...executionDetail(), approvals: [{ ...approvalFixture(), actor_id: 'other' }] }, request).phase).toBe('conflict')
  })

  it('times out independently of abort support and ignores the late write response', async () => {
    const response = deferred<ReturnType<typeof proposalReceipt>>()
    vi.mocked(decideChangeProposal).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); void hook.start('APPROVED', 'Original reason')
    vi.advanceTimersByTime(30_000)
    expect(render().pending?.phase).toBe('unknown')
    response.resolve(proposalReceipt()); await settle()
    expect(render().pending?.phase).toBe('unknown')
    expect(saved).not.toHaveBeenCalled()
  })

  it('retains the original identity after a failed or timed-out read', async () => {
    vi.mocked(decideChangeProposal).mockRejectedValue(new TypeError('network'))
    const hook = render(); commitHooks(); void hook.start('APPROVED', 'Original reason')
    let current = await settle(); const original = current.pending!.request
    vi.mocked(loadRunDetail).mockRejectedValueOnce(new TypeError('offline'))
    void current.check(); current = await settle()
    expect(current.readFailed).toBe(true); expect(current.pending!.request).toEqual(original)
    const read = deferred<ReturnType<typeof executionDetail>>()
    vi.mocked(loadRunDetail).mockReturnValue(read.promise)
    void current.check(); vi.advanceTimersByTime(30_000)
    current = render(); expect(current.checking).toBe(false); expect(current.readFailed).toBe(true)
    read.resolve({ ...executionDetail(), approvals: [approvalFixture()] }); await settle()
    expect(render().pending?.phase).toBe('unknown')
    expect(decideChangeProposal).toHaveBeenCalledTimes(1)
  })

  it('does not accept a late response into a new actor owner', async () => {
    const response = deferred<ReturnType<typeof proposalReceipt>>()
    vi.mocked(decideChangeProposal).mockReturnValue(response.promise)
    const hook = render(); commitHooks(); void hook.start('APPROVED', 'Original reason')
    const signal = vi.mocked(decideChangeProposal).mock.calls[0]![7]!
    unmountHooks(); render({ scope: { ...S, actorId: 'other' } }); commitHooks()
    expect(signal.aborted).toBe(true)
    response.resolve(proposalReceipt()); await hookMicrotasks()
    expect(render({ scope: { ...S, actorId: 'other' } }).pending).toBeNull()
    expect(saved).not.toHaveBeenCalled()
  })

  it('fails closed on corrupt recovery state and warns when storage fails after sending', async () => {
    const request = freezeProposalDecision(S, proposalFixture(), 'APPROVED', 'Original reason', 'original-key')
    saveProposalDecision(request); storage.set([...storage.keys()][0]!, '{bad')
    let current = render(); commitHooks(); void current.start('APPROVED', 'Original reason')
    expect(current.locked).toBe(true); expect(current.storageUnavailable).toBe(true)
    expect(decideChangeProposal).not.toHaveBeenCalled()
    unmountHooks(); storage.clear()
    vi.stubGlobal('window', { sessionStorage: { getItem: () => null, setItem: () => { throw new Error('quota') } } })
    vi.mocked(decideChangeProposal).mockRejectedValue(new TypeError('network'))
    current = render(); commitHooks(); void current.start('APPROVED', 'Original reason')
    current = await settle(); expect(current.storageUnavailable).toBe(true); expect(current.pending?.phase).toBe('unknown')
    expect(current.pending?.request.key).toBeTruthy()
  })

  it.each([400, 422])('allows a new explicit draft only after a known %s refusal and fresh pending read', async (status) => {
    vi.mocked(decideChangeProposal).mockRejectedValueOnce(new ApiProblemError('invalid reason', status))
    const hook = render(); commitHooks(); void hook.start('APPROVED', 'Original reason')
    let current = await settle(); const firstKey = current.pending!.request.key
    expect(storage.size).toBe(0)
    current.editRejected(); expect(render().pending).not.toBeNull()
    vi.mocked(loadRunDetail).mockResolvedValue(executionDetail())
    void current.check(); current = await settle()
    expect(current.pending).toMatchObject({ phase: 'rejected', editable: true })
    expect(decideChangeProposal).toHaveBeenCalledTimes(1)
    current.editRejected(); current = render()
    expect(current.pending).toBeNull()
    vi.mocked(decideChangeProposal).mockImplementationOnce(async (_p, _r, _proposal, decision, reason) => proposalReceipt(decision, reason))
    void current.start('REJECTED', 'Corrected reason'); current = await settle()
    expect(current.pending?.phase).toBe('confirmed')
    expect(current.pending?.request.key).not.toBe(firstKey)
    expect(decideChangeProposal).toHaveBeenCalledTimes(2)
  })

  it('never unlocks unknown requests after empty GET, or refusals after access is lost', async () => {
    vi.mocked(decideChangeProposal).mockRejectedValueOnce(new TypeError('network'))
    const hook = render(); commitHooks(); void hook.start('APPROVED', 'Original reason')
    let current = await settle()
    vi.mocked(loadRunDetail).mockResolvedValue(executionDetail())
    void current.check(); current = await settle(); current.editRejected()
    expect(render().pending?.phase).toBe('unknown')
    expect(decideChangeProposal).toHaveBeenCalledTimes(1)
  })

  it('blocks new writes while readonly or expired but still permits original read-back', async () => {
    let hook = render({ writable: false }); commitHooks(); void hook.start('APPROVED', 'Original reason')
    hook = render({ proposal: { ...proposalFixture(), expires_at: '2020-01-01T00:00:00Z' } }); void hook.start('APPROVED', 'Original reason')
    expect(decideChangeProposal).not.toHaveBeenCalled()
  })
})
