// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { loadProjectModules, loadProjectTasks, loadUsers, type TaskCatalogRecord } from '../../src/api'
import { UserDirectory } from '../../src/components/UserDirectory'
import { ProjectModulesPanel } from '../../src/components/ProjectModulesPanel'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { DEMO_PROJECT, demoSession } from '../fixtures'
import { deferred } from '../fixtures/hookHarness'
import base from '../../src/styles/base.css?raw'
import shell from '../../src/styles/shell.css?raw'
import pages from '../../src/styles/pages.css?raw'
import shared from '../../src/styles/shared-audit.css?raw'
import project from '../../src/styles/project-management.css?raw'
import members from '../../src/styles/project-members.css?raw'

vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(), loadProjectModules: vi.fn(), loadProjectTasks: vi.fn(), loadUsers: vi.fn() }))
let container: HTMLDivElement
let root: Root
let style: HTMLStyleElement
/** 宣言と React 状態だけを検証し、browser の幾何を通過済みと扱わない。 */
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
  style = document.createElement('style'); style.textContent = [base, shell, pages, project, members, shared].join('\n'); document.head.append(style)
  vi.mocked(loadProjectModules).mockResolvedValue([])
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); style.remove(); vi.resetAllMocks(); vi.unstubAllGlobals() })

/** Project の候補読取を意図的に保留し、真の空と区別する。 */
async function renderModules() {
  await act(async () => root.render(<LanguageProvider language="zh"><ProjectModulesPanel projectId={DEMO_PROJECT.project_id} session={demoSession('ADMIN')} /></LanguageProvider>))
}
describe('shared audit protections', () => {
  it('places user creation before the long list and keeps its draft mounted when collapsed', async () => {
    vi.mocked(loadUsers).mockResolvedValue({ items: [], limit: 25, offset: 0, total: 0 })
    await act(async () => root.render(<LanguageProvider language="zh"><UserDirectory session={demoSession('ADMIN')} revision={0} onSessionEnded={() => {}} onChanged={() => {}} /></LanguageProvider>))
    const disclosure = container.querySelector<HTMLDetailsElement>('.accountCreateDisclosure')!
    const input = disclosure.querySelector<HTMLInputElement>('input[type="email"]')!
    expect(disclosure.open).toBe(false)
    const open = [...container.querySelectorAll<HTMLButtonElement>('.panelHeader button')].find((button) => button.textContent === MESSAGES.zh.account.create)!
    await act(async () => open.click())
    expect(disclosure.open).toBe(true)
    expect(document.activeElement).toBe(input)
    await act(async () => { disclosure.open = false })
    expect(disclosure.querySelector('input[type="email"]')).toBe(input)
    expect(disclosure.compareDocumentPosition(container.querySelector('[data-account-form="search"]')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
  it.each(['checkbox', 'radio'])('keeps %s out of text input dimensions', (type) => {
    container.innerHTML = `<label class="memberAcknowledgment"><input type="${type}">Confirm</label>`
    const control = getComputedStyle(container.querySelector('input')!)
    expect(control.width).toBe('18px'); expect(control.height).toBe('18px'); expect(Number.parseFloat(control.minHeight)).toBe(0); expect(control.padding).toBe('0px')
  })
  it('balances custom sidebar padding without losing native arrow space', () => {
    container.innerHTML = '<aside class="sidebar"><button class="selectTrigger">Project</button><select><option>A</option></select></aside>'
    const custom = getComputedStyle(container.querySelector('button')!)
    expect(custom.paddingLeft).toBe(custom.paddingRight)
  })
  it('uses wrapping actions and role-independent complete project names', () => {
    container.innerHTML = '<div class="buttonRow"></div><div class="projectList"><button class="projectSelect"><strong>Long name</strong></button></div>'
    expect(getComputedStyle(container.querySelector('.buttonRow')!).flexWrap).toBe('wrap')
    expect(getComputedStyle(container.querySelector('strong')!).whiteSpace).toBe('normal')
    expect(getComputedStyle(container.querySelector('strong')!).overflowWrap).toBe('anywhere')
  })
  it('does not call pending or failed candidates an empty skill library and offers a read-only retry', async () => {
    const pending = deferred<TaskCatalogRecord>()
    vi.mocked(loadProjectTasks).mockReturnValueOnce(pending.promise).mockResolvedValueOnce({ tasks: [] })
    await renderModules()
    expect(container.textContent).not.toContain(MESSAGES.zh.projects.modules.noPublished)
    expect(container.querySelector('fieldset')?.disabled).toBe(true)
    await act(async () => pending.reject(new Error('Synthetic read failure')))
    expect(container.textContent).toContain('Synthetic read failure')
    expect(container.textContent).not.toContain(MESSAGES.zh.projects.modules.noPublished)
    const retry = [...container.querySelectorAll('button')].find((button) => button.textContent === MESSAGES.zh.account.refresh)!
    await act(async () => retry.click())
    expect(loadProjectTasks).toHaveBeenCalledTimes(2)
    expect(container.textContent).toContain(MESSAGES.zh.projects.modules.noPublished)
  })
  it('ignores old project candidate responses after switching projects', async () => {
    const old = deferred<TaskCatalogRecord>()
    vi.mocked(loadProjectTasks).mockReturnValueOnce(old.promise).mockResolvedValueOnce({ tasks: [] })
    await renderModules()
    await act(async () => root.render(<LanguageProvider language="zh"><ProjectModulesPanel projectId="00000000-0000-4000-8000-000000000011" session={demoSession('ADMIN')} /></LanguageProvider>))
    await act(async () => old.reject(new Error('Stale failure')))
    expect(container.textContent).not.toContain('Stale failure')
    expect(container.textContent).toContain(MESSAGES.zh.projects.modules.noPublished)
  })
})
