// @vitest-environment jsdom
import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiProblemError, decideChangeProposal, loadRunDetail } from '../../src/api'
import { ReportPresentation } from '../../src/components/ReportPresentation'
import { PendingActionsSection } from '../../src/components/RunControlledEffects'
import { RunResultPanel, type RunDetailState } from '../../src/components/RunResultPanel'
import { OutcomeEnvelopeResult } from '../../src/components/RunOutcome'
import { ToolCallSummary, toolStatusTone } from '../../src/components/ToolCallSummary'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES } from '../../src/lib/i18n/messages'
import auditStyles from '../../src/styles/execution-audit.css?raw'
import workspaceStyles from '../../src/styles/workspace.css?raw'
import { approvalFixture, executionDetail, PROPOSAL_SCOPE as S, proposalFixture, toolFixture } from '../fixtures/executionAudit'
import { evaluationResult } from '../fixtures/evaluation'

vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(), decideChangeProposal: vi.fn(), loadRunDetail: vi.fn() }))
vi.mock('../../src/components/RunArtifacts', () => ({ RunArtifacts: () => null }))
vi.mock('../../src/components/EvaluationSection', () => ({ RunEvaluations: () => null }))
let container: HTMLDivElement
let root: Root
let style: HTMLStyleElement
const labels = MESSAGES.zh.uiAuditWorkspace
beforeEach(() => {
  vi.clearAllMocks(); vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
  style = document.createElement('style'); style.textContent = `${workspaceStyles}\n${auditStyles}`; document.head.append(style)
  window.sessionStorage.clear()
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); style.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

/** 実 React owner と Provider の再調停を検証し、layout pixel は模倣しない。 */
async function render(node: ReactNode): Promise<void> { await act(async () => root.render(node)) }
/** 対象を一意な翻訳名で探し、操作の取り違えを失敗にする。 */
function button(label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll<HTMLButtonElement>('button')].filter((element) => element.textContent === label)
  expect(found).toHaveLength(1); return found[0]!
}
/** 親 owner を保持し、detail だけを変更する。 */
function panel(state: RunDetailState, options: { readOnly?: boolean; actorId?: string; onRetry?: () => void } = {}) {
  return <RunResultPanel actorId={options.actorId ?? S.actorId} projectId={S.projectId} runId={S.runId}
    projectReadOnly={options.readOnly} csrfToken="synthetic-session" state={state} onRetryDetail={options.onRetry} />
}

describe('execution audit DOM contracts', () => {
  it('keeps complete approval facts and named keyboard-scroll regions', async () => {
    const detail = executionDetail()
    await render(<PendingActionsSection detail={detail} proposals={detail.change_proposals} actorId={S.actorId} csrfToken="synthetic-session" />)
    const target = container.querySelector<HTMLElement>('.proposalTarget dd')!
    expect(target.textContent).toBe(detail.change_proposals[0]!.target.display)
    const computed = getComputedStyle(target)
    expect(computed.whiteSpace).toBe('normal'); expect(computed.overflow).toBe('visible'); expect(computed.overflowWrap).toBe('anywhere')
    expect(getComputedStyle(target.parentElement!).gridColumn).toBe('1 / -1')
    const diff = container.querySelector<HTMLElement>('.proposalFileBody')!
    expect(diff.tabIndex).toBe(0); expect(diff.getAttribute('role')).toBe('region'); expect(diff.getAttribute('aria-label')).toBe('src/fixture.ts')
    expect(container.querySelector('.proposalFacts')!.textContent).toContain('2099')
    expect(auditStyles).toContain('@media (max-width: 600px)')
    expect(auditStyles).toContain('.proposalCard .proposalFacts { grid-template-columns: minmax(0, 1fr); }')
  })

  it('preserves the original through version changes, settled moves and no-detail states when only setItem fails', async () => {
    const setItem = vi.spyOn(Object.getPrototypeOf(window.sessionStorage) as Storage, 'setItem').mockImplementation(() => { throw new DOMException('Quota full', 'QuotaExceededError') })
    // Node 26 の native Storage と JSDOM の storage realm は同じ prototype とは限らない。
    expect(window.sessionStorage.setItem).toBe(setItem)
    expect(window.sessionStorage.getItem('quota-fixture-unwritten')).toBeNull()
    vi.mocked(decideChangeProposal).mockRejectedValue(new TypeError('network'))
    let detail = executionDetail(); await render(panel({ status: 'ready', detail }))
    const approve = button(MESSAGES.zh.runResult.approveAndApply)
    await act(async () => { approve.click(); approve.click() })
    expect(decideChangeProposal).toHaveBeenCalledTimes(1); expect(setItem).toHaveBeenCalledTimes(1)
    const first = vi.mocked(decideChangeProposal).mock.calls[0]!, originalKey = first[5]
    const original = container.querySelector('.proposalOriginal')!
    expect(original.textContent).toContain(originalKey); expect(container.textContent).toContain(labels.proposalUnknown)
    expect(container.textContent).toContain(labels.proposalStorageUnavailable); expect(window.sessionStorage.length).toBe(0)
    detail = { ...detail, change_proposals: [{ ...proposalFixture(), version: 5, checksum: 'new-checksum' }] }
    await render(panel({ status: 'ready', detail }))
    expect(container.querySelector('.proposalOriginal')).toBe(original)
    expect(container.querySelector('.proposalOriginal')!.textContent).toContain(originalKey); expect(container.querySelector('.proposalDecision')).toBeNull()
    detail = { ...detail, change_proposals: [{ ...detail.change_proposals[0]!, status: 'APPLIED' }] }
    await render(panel({ status: 'ready', detail }))
    expect(container.querySelector('.proposalOriginal')).not.toBe(original)
    expect(container.querySelector('.proposalOriginal')!.closest('.resultCollapse')).not.toBeNull()
    expect(container.querySelector('.proposalOriginal')!.textContent).toContain(originalKey)
    expect(container.textContent).toContain(labels.proposalStorageUnavailable)
    await render(panel({ status: 'loading' })); await render(panel({ status: 'ready', detail: executionDetail() }))
    expect(container.querySelector('.proposalOriginal')!.textContent).toContain(originalKey); expect(container.querySelector('.proposalDecision')).toBeNull()
    vi.mocked(loadRunDetail).mockResolvedValue({ ...executionDetail(), approvals: [approvalFixture('APPROVED', first[4])] })
    await act(async () => button(labels.proposalCheck).click())
    expect(container.textContent).toContain(labels.proposalObserved); expect(container.textContent).not.toContain(labels.proposalConfirmed)
    expect(decideChangeProposal).toHaveBeenCalledTimes(1); expect(loadRunDetail).toHaveBeenCalledTimes(1)
  })

  it('does not expose the previous actor memory to a different owner', async () => {
    vi.spyOn(Object.getPrototypeOf(window.sessionStorage) as Storage, 'setItem').mockImplementation(() => { throw new Error('quota') })
    vi.mocked(decideChangeProposal).mockRejectedValue(new TypeError('network'))
    await render(panel({ status: 'ready', detail: executionDetail() })); await act(async () => button(MESSAGES.zh.runResult.approveAndApply).click())
    const originalKey = vi.mocked(decideChangeProposal).mock.calls[0]![5]
    await render(panel({ status: 'ready', detail: executionDetail() }, { actorId: '00000000-0000-4000-8000-000000000999', readOnly: true }))
    expect(container.textContent).not.toContain(originalKey); expect(button(MESSAGES.zh.runResult.approveAndApply).disabled).toBe(true)
    expect(container.textContent).toContain(labels.proposalReadOnly); expect(decideChangeProposal).toHaveBeenCalledTimes(1)
  })

  it('keeps original read-back available in archived projects without enabling new decisions', async () => {
    vi.mocked(decideChangeProposal).mockRejectedValue(new TypeError('network'))
    await render(panel({ status: 'ready', detail: executionDetail() })); await act(async () => button(MESSAGES.zh.runResult.rejectAndContinue).click())
    await render(panel({ status: 'ready', detail: executionDetail() }, { readOnly: true }))
    expect(button(labels.proposalCheck).disabled).toBe(false); expect(container.querySelector('.proposalDecision')).toBeNull()
    vi.mocked(loadRunDetail).mockResolvedValue(executionDetail()); await act(async () => button(labels.proposalCheck).click())
    expect(container.textContent).toContain(labels.proposalPending); expect(decideChangeProposal).toHaveBeenCalledTimes(1)
  })

  it('allows explicit correction only after a known refusal and fresh pending read', async () => {
    vi.mocked(decideChangeProposal).mockRejectedValue(new ApiProblemError('invalid reason', 422))
    await render(panel({ status: 'ready', detail: executionDetail() })); await act(async () => button(MESSAGES.zh.runResult.approveAndApply).click())
    expect(container.textContent).toContain(labels.proposalRejected); expect(container.textContent).not.toContain(labels.proposalEditRejected)
    vi.mocked(loadRunDetail).mockResolvedValue(executionDetail()); await act(async () => button(labels.proposalCheck).click())
    await act(async () => button(labels.proposalEditRejected).click())
    expect(container.querySelector('.proposalOriginal')).toBeNull(); expect(button(MESSAGES.zh.runResult.approveAndApply).disabled).toBe(false)
    expect(decideChangeProposal).toHaveBeenCalledTimes(1)
  })

  it('provides independent detail retry and retains the same report DOM while stale or updating', async () => {
    const retry = vi.fn(); await render(panel({ status: 'error', message: 'Read failed' }, { onRetry: retry }))
    await act(async () => button(labels.detailRetry).click()); expect(retry).toHaveBeenCalledTimes(1)
    const detail = { ...executionDetail(), result: evaluationResult() }
    await render(panel({ status: 'ready', detail }, { onRetry: retry })); const report = container.querySelector('.resultReportBody')
    await render(panel({ status: 'loading', detail }, { onRetry: retry }))
    expect(container.querySelector('.resultReportBody')).toBe(report); expect(container.textContent).toContain(labels.detailUpdating)
    expect(button(MESSAGES.zh.runResult.approveAndApply).disabled).toBe(true)
    await render(panel({ status: 'error', message: 'Second read failed', detail }, { onRetry: retry }))
    expect(container.querySelector('.resultReportBody')).toBe(report); expect(container.textContent).toContain(labels.detailStale)
    expect(container.textContent).toContain('Original')
    const toggle = button(MESSAGES.zh.runResult.technicalDetails)
    expect(toggle.closest('.resultReportBody')).toBe(report); expect(toggle.closest('[role="dialog"]')).toBeNull()
    await act(async () => toggle.click()); expect(button(MESSAGES.zh.runResult.hideTechnicalDetails).getAttribute('aria-pressed')).toBe('true')
  })

  it.each(['zh', 'ja', 'en'] as const)('translates failed tools with full accessible arguments in %s', async (language) => {
    const tool = toolFixture()
    await render(<LanguageProvider language={language}><ul className="toolSummaryList"><ToolCallSummary tool={tool} /></ul></LanguageProvider>)
    expect(container.querySelector('.toolStatus')!.textContent).toBe(MESSAGES[language].uiAuditWorkspace.toolStatusFailed)
    expect(container.querySelector('.toolStatus-failed')).not.toBeNull()
    const full = container.querySelector<HTMLElement>('.toolArguments pre')!
    expect(full.tabIndex).toBe(0); expect(full.getAttribute('role')).toBe('region'); expect(full.textContent).toContain(tool.arguments_summary.path)
    expect(container.querySelector('summary')!.textContent).toBe(MESSAGES[language].uiAuditWorkspace.toolArguments)
  })

  it('keeps unknown and non-successful status values out of the success color', async () => {
    for (const [status, expected] of [['FAILED', 'failed'], ['RUNNING', 'running'], ['PENDING', 'pending'], ['CANCELLED', 'cancelled'], ['NEW_STATUS', 'unknown'], ['SUCCEEDED', 'succeeded']]) {
      expect(toolStatusTone(status!)).toBe(expected)
    }
    await render(<ToolCallSummary tool={toolFixture('NEW_STATUS')} />)
    expect(container.querySelector('.toolStatus')!.textContent).toBe('NEW_STATUS'); expect(container.querySelector('.toolStatus-succeeded')).toBeNull()
  })

  it('uses shared sanitized HTML preview and its large-document source fallback', async () => {
    const outcome = (content: string) => <OutcomeEnvelopeResult data={{ status: 'COMPLETED', deliverables: [{ title: 'Fixture report', content }] }}
      schema={null} showTechnicalDetails={false} onArtifact={vi.fn()} onEvidence={vi.fn()} />
    await render(outcome('<html><body><p>Allowed report</p><script>blocked()</script></body></html>'))
    const frame = container.querySelector('iframe')!
    expect(frame.getAttribute('sandbox')).toBe(''); expect(frame.getAttribute('srcdoc')).toContain('Allowed report'); expect(frame.getAttribute('srcdoc')).not.toContain('<script>')
    await render(outcome(`<html><body>${'x'.repeat(1_000_001)}</body></html>`))
    expect(container.querySelector('iframe')).toBeNull(); expect(container.textContent).toContain(MESSAGES.zh.documentsPanel.htmlSourcePages)
    expect(container.querySelector('pre')).not.toBeNull()
  })

  it('explains report fallback and retries only presentation while escaping the preserved result', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {}); let fail = true
    /** 同じ原データの render だけを再試行する合成失敗。 */
    function Report() { if (fail) throw new Error('fixture layout failure'); return <p>Readable report restored</p> }
    const value = { summary: '<script>blocked()</script>', count: 1 }
    await render(<ReportPresentation value={value}><Report /></ReportPresentation>)
    expect(container.textContent).toContain(labels.reportFallback); expect(container.querySelector('.rawResultBody')!.textContent).toContain(value.summary)
    expect(container.querySelector('script')).toBeNull(); expect(container.querySelector<HTMLElement>('.rawResultBody')!.tabIndex).toBe(0)
    fail = false; await act(async () => button(labels.reportRetry).click())
    expect(container.textContent).toContain('Readable report restored'); expect(container.querySelector('.rawResultBody')).toBeNull()
    expect(decideChangeProposal).not.toHaveBeenCalled(); expect(loadRunDetail).not.toHaveBeenCalled()
  })
})
