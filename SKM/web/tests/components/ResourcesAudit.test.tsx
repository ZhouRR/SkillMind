// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ResourceTabButton, ScopeSubsetPicker } from '../../src/components/ResourceFormFields'
import { ResourcesPage } from '../../src/pages/ResourcesPage'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { scopeDraftFromScope } from '../../src/lib/resourceConfig'
import type { ResourceTab } from '../../src/lib/resourceDrafts'

const api = vi.hoisted(() => ({
  loadSecretReferences: vi.fn(), loadIntegrations: vi.fn(), loadResourceBindings: vi.fn(),
  loadEffectPreauthorizations: vi.fn(), loadProjectTasks: vi.fn(), createSecretReference: vi.fn(), createIntegration: vi.fn(),
}))
vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(), ...api }))

let root: Root
let container: HTMLDivElement
let session = 0
const labels = MESSAGES.zh
/** Test が応答時点を決める合成 transport。 */
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (value: unknown) => void
  const promise = new Promise<T>((accept, refuse) => { resolve = accept; reject = refuse })
  return { promise, resolve, reject }
}
/** Native input の user event で controlled draft を更新する。 */
async function type(input: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
function button(text: string, within: ParentNode = container): HTMLButtonElement {
  const found = [...within.querySelectorAll<HTMLButtonElement>('button')].find((item) => item.textContent === text)
  if (!found) throw new Error(`Missing button: ${text}`)
  return found
}
async function mountPage() {
  await act(async () => root.render(<ResourcesPage projectId="fixture-project" csrfToken={`resource-ui-${session}`} />))
}
beforeEach(() => {
  session += 1; vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
  for (const mock of Object.values(api)) mock.mockReset().mockResolvedValue([])
  api.loadProjectTasks.mockResolvedValue({ tasks: [] })
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals() })

/** 同一 tablist のみを keyboard が動かし、panel との参照を維持する。 */
function Tabs() {
  const [current, setCurrent] = useState<ResourceTab>('secret')
  return <><div role="tablist">{(['secret', 'binding', 'policy'] as const).map((tab) =>
    <ResourceTabButton key={tab} id={`tab-${tab}`} panelId={`panel-${tab}`} current={current} tab={tab} onSelect={setCurrent}>{tab}</ResourceTabButton>)}</div>
    {(['secret', 'binding', 'policy'] as const).map((tab) => <section key={tab} id={`panel-${tab}`} role="tabpanel" aria-labelledby={`tab-${tab}`} hidden={current !== tab} />)}</>
}

describe('resource audit accessible controls', () => {
  it('uses one tab stop and Arrow/Home/End selection with linked panels', async () => {
    await act(async () => root.render(<Tabs />))
    const tabs = [...container.querySelectorAll<HTMLButtonElement>('[role=tab]')]
    expect(tabs.map((tab) => tab.tabIndex)).toEqual([0, -1, -1])
    await act(async () => tabs[0]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true })))
    expect(tabs[2]!.getAttribute('aria-selected')).toBe('true'); expect(document.activeElement).toBe(tabs[2])
    await act(async () => tabs[2]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true })))
    expect(tabs[0]!.tabIndex).toBe(0)
    await act(async () => tabs[0]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true })))
    expect(tabs[2]!.tabIndex).toBe(0)
    for (const tab of tabs) {
      const panel = document.getElementById(tab.getAttribute('aria-controls')!)!
      expect(panel.getAttribute('aria-labelledby')).toBe(tab.id)
    }
  })
  it('gives every narrowing input a visible localized label and contextual unrestricted choices', async () => {
    const entries = scopeDraftFromScope({ paths: ['*'], tables: ['*'] }, { keepAll: false })
    await act(async () => root.render(<ScopeSubsetPicker allowWildcard entries={entries} legend="Fixture scope" onChange={() => {}} />))
    const texts = [...container.querySelectorAll<HTMLInputElement>('input:not([type=checkbox])')]
    expect(texts).toHaveLength(2)
    for (const input of texts) {
      expect(input.labels?.length).toBe(1)
      expect(input.labels?.[0]?.textContent).toContain('收窄')
      expect(document.getElementById(input.getAttribute('aria-describedby')!)?.textContent).toBe(labels.resources.scopeNarrowHint)
    }
    const choices = [...container.querySelectorAll<HTMLInputElement>('input[type=checkbox]')]
    expect(choices.map((input) => input.labels?.[0]?.textContent)).toEqual([
      labels.resourcesAudit.scopeKeepAllLabel(labels.resources.scopeKeyLabels.paths!),
      labels.resourcesAudit.scopeKeepAllLabel(labels.resources.scopeKeyLabels.tables!),
    ])
  })
})

describe('resource audit page states', () => {
  it('renders only pending until read success, then shows the confirmed empty guide', async () => {
    const read = deferred<never[]>()
    api.loadIntegrations.mockReturnValueOnce(read.promise)
    await mountPage()
    expect(container.textContent).not.toContain(labels.resources.connectGuide)
    expect(button(labels.resources.connectTitle).disabled).toBe(true)
    await act(async () => read.resolve([]))
    expect(container.textContent).toContain(labels.resources.connectGuide)
    expect(button(labels.resources.connectTitle).disabled).toBe(false)
  })
  it('offers retry on catalog failure without reporting no connections', async () => {
    api.loadIntegrations.mockRejectedValueOnce(new Error('fixture unavailable'))
    await mountPage()
    expect(container.textContent).not.toContain(labels.resources.connectGuide)
    expect(container.querySelector('.resourceLoadError')?.textContent).toContain('fixture unavailable')
    await act(async () => button(labels.resourcesAudit.refresh, container.querySelector('.resourceLoadError')!).click())
    expect(container.textContent).toContain(labels.resources.connectGuide)
  })
  it('requires explicit technical fallback after task-catalog failure', async () => {
    api.loadProjectTasks.mockRejectedValueOnce(new Error('fixture task failure'))
    await mountPage()
    await act(async () => button(labels.resources.newBinding, container.querySelector('[id$=panel-binding] .panelHeaderActions')!).click())
    const dialog = document.querySelector<HTMLElement>('.modalOverlay:not([hidden])')!
    expect(dialog.textContent).toContain(labels.resourcesAudit.taskLoadFailed)
    expect(dialog.textContent).not.toContain(labels.resourcesAudit.noTasks)
    expect(dialog.textContent).not.toContain(labels.resources.requirementKeyInputLabel)
    expect(button(labels.resources.saveBinding, dialog).disabled).toBe(true)
    await act(async () => button(labels.resourcesAudit.manualTaskEntry, dialog).click())
    expect(dialog.textContent).toContain(labels.resources.requirementKeyInputLabel)
    expect(dialog.textContent).toContain(labels.resourcesAudit.manualTaskHint)
  })
  it('locks the submitted snapshot, lets Close stop waiting, and never retries an unknown partial connection', async () => {
    const write = deferred<unknown>()
    api.createSecretReference.mockResolvedValue({ secret_reference_id: 'fixture-secret', project_id: 'fixture-project',
      name: 'Submitted connection', provider: 'http', resolver: 'MANAGED', key_version: 'v1', status: 'ACTIVE',
      updated_at: '2026-01-01T00:00:00Z' })
    api.createIntegration.mockReturnValue(write.promise)
    await mountPage()
    await act(async () => button(labels.resources.connectTitle).click())
    const dialog = document.querySelector<HTMLElement>('.modalOverlay:not([hidden])')!
    const form = dialog.querySelector<HTMLFormElement>('form')!
    await type(form.querySelector<HTMLInputElement>('input[maxlength="200"]')!, 'Submitted connection')
    await type(form.querySelector<HTMLInputElement>('input[type=url]')!, 'https://fixture.example.test')
    await type(form.querySelector<HTMLInputElement>('input[type=password]')!, 'synthetic-test-value')
    await act(async () => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })
    expect(api.createSecretReference).toHaveBeenCalledTimes(1); expect(api.createIntegration).toHaveBeenCalledTimes(1)
    expect(form.querySelector('fieldset')?.disabled).toBe(true)
    expect(form.querySelector('input')?.matches(':disabled')).toBe(true)
    expect(button(labels.resourcesAudit.saving, form).disabled).toBe(true)
    expect(form.querySelector('input[type=password]')?.getAttribute('value')).toBe('')
    await act(async () => button(labels.resourcesAudit.close, form).click())
    expect(document.querySelector('.modalOverlay:not([hidden])')).toBeNull()
    expect(container.textContent).toContain(labels.resourcesAudit.unknownTitle)
    expect(button(labels.resources.connectTitle).disabled).toBe(true)
    await act(async () => write.resolve({}))
    expect(container.textContent).toContain(labels.resourcesAudit.unknownTitle)
    expect(api.createIntegration).toHaveBeenCalledTimes(1)
    const feedback = container.querySelector<HTMLElement>('.resourceAdmin > .resourceRequestFeedback')!
    await act(async () => button(labels.resourcesAudit.refresh, feedback).click())
    expect(button(labels.resourcesAudit.reviewComplete, feedback).disabled).toBe(false)
    await act(async () => button(labels.resourcesAudit.reviewComplete, feedback).click())
    expect(container.textContent).not.toContain(labels.resourcesAudit.unknownTitle)
    await act(async () => button(labels.resources.connectTitle).click())
    const next = document.querySelector<HTMLElement>('.modalOverlay:not([hidden])')!
    expect(next.querySelector<HTMLInputElement>('input[type=password]')?.value).toBe('')
    expect(api.createIntegration).toHaveBeenCalledTimes(1)
  })
})
