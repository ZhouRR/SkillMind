// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { Select } from '../../src/components/Select'
import { ModalDialog } from '../../src/components/PageElements'
import { LanguageProvider } from '../../src/i18n'

let container: HTMLDivElement
let root: Root

/** Browser layout を代用せず、DOM の操作・フォーム契約を隔離して検証する。 */
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
  Element.prototype.scrollIntoView = vi.fn()
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)
})
afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

/** React と primitive の非同期 lifecycle を最後まで反映する。 */
async function press(target: Element, key: string) {
  await act(async () => {
    target.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }))
    target.dispatchEvent(new KeyboardEvent('keyup', { key, bubbles: true, cancelable: true }))
  })
}

/** Select の公開 controlled value 契約と native form entry を組み合わせる。 */
function Controlled({ changes }: { changes: (value: string) => void }) {
  const [value, setValue] = useState('')
  return <form onReset={() => setValue('')}>
    <label htmlFor="choice">Choose project</label>
    <Select id="choice" name="project" required value={value} onValueChange={(next) => { changes(next); setValue(next) }}>
      <option value="" disabled>Choose a project</option>
      <option value="alpha">Alpha</option>
      <optgroup label="Archived" disabled><option value="archived">Archived project</option></optgroup>
      <option value="beta">Beta</option>
    </Select>
    <button type="reset">Reset</button>
  </form>
}

describe('shared cross-browser Select', () => {
  it('applies compact density to the language trigger and its portal without changing default selects', async () => {
    const changes = vi.fn()
    await act(async () => root.render(<>
      <Select id="language" density="compact" defaultValue="ja" onValueChange={changes}>
        <option value="ja">日本語</option><option value="zh">中文</option><option value="en">English</option>
      </Select>
      <Select id="project"><option value="project">Project</option></Select>
    </>))
    const trigger = container.querySelector<HTMLElement>('#language')!
    expect(trigger.dataset.density).toBe('compact')
    expect(trigger.hasAttribute('density')).toBe(false)
    expect(container.querySelector<HTMLElement>('#project')!.dataset.density).toBe('default')
    await act(async () => trigger.focus())
    await press(trigger, 'ArrowDown')
    const popup = document.querySelector<HTMLElement>('.selectPopup')!
    expect(popup.dataset.density).toBe('compact')
    expect(popup.querySelectorAll('[role="option"]')).toHaveLength(3)
    expect(popup.querySelectorAll('.selectItemIndicator')).toHaveLength(3)
    await press(trigger, 'Escape')
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(trigger)
    expect(changes).not.toHaveBeenCalled()
    await press(trigger, 'ArrowDown')
    const english = document.querySelector<HTMLElement>('[data-value="en"][role="option"]')!
    await act(async () => english.click())
    expect(changes).toHaveBeenCalledExactlyOnceWith('en')
    expect(trigger.getAttribute('data-value')).toBe('en')
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
  })

  it('renders a real DOM popup, preserves required/name, changes exactly once, and resets', async () => {
    const changes = vi.fn()
    await act(async () => root.render(<Controlled changes={changes} />))
    const trigger = container.querySelector<HTMLButtonElement>('[role="combobox"]')!
    const form = container.querySelector('form')!
    expect(trigger).not.toBeNull()
    expect(trigger.textContent).toContain('Choose a project')
    expect(form.checkValidity()).toBe(false)
    await act(async () => trigger.focus())
    await press(trigger, 'ArrowDown')
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    const options = Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))
    expect(options.map((option) => option.textContent)).toEqual(expect.arrayContaining(['Alpha', 'Beta']))
    expect(options.find((option) => option.dataset.value === 'archived')?.getAttribute('aria-disabled')).toBe('true')
    await act(async () => options.find((option) => option.dataset.value === 'beta')!.click())
    expect(changes).toHaveBeenCalledExactlyOnceWith('beta')
    expect(trigger.dataset.value).toBe('beta')
    expect(trigger.textContent).toContain('Beta')
    expect(form.checkValidity()).toBe(true)
    expect(new FormData(form).get('project')).toBe('beta')
    await act(async () => form.reset())
    expect(trigger.dataset.value).toBe('')
    expect(form.checkValidity()).toBe(false)
  })

  it('keeps a checkmark slot on every option when selection changes', async () => {
    const render = (value: string) => <Select aria-label="Project" value={value}>
      <option value="alpha">Alpha</option><option value="beta">Beta</option>
      <option value="disabled" disabled>Unavailable</option>
    </Select>
    await act(async () => root.render(render('alpha')))
    const trigger = container.querySelector<HTMLElement>('[role="combobox"]')!
    await press(trigger, 'ArrowDown')
    const options = Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))
    const indicators = options.map((option) => option.querySelector('.selectItemIndicator'))
    expect(indicators.every(Boolean)).toBe(true)
    expect(indicators.map((indicator) => indicator!.hasAttribute('data-selected'))).toEqual([true, false, false])
    expect(indicators.every((indicator) => indicator!.getAttribute('aria-hidden') === 'true')).toBe(true)
    await act(async () => root.render(render('beta')))
    expect(options.map((option) => option.querySelector('.selectItemIndicator'))).toEqual(indicators)
    expect(indicators.map((indicator) => indicator!.hasAttribute('data-selected'))).toEqual([false, true, false])
  })

  it('leaves disabled fields out of form data and prevents opening', async () => {
    const changes = vi.fn()
    await act(async () => root.render(<form><label>Unavailable
      <Select name="disabled" disabled value="alpha" onValueChange={changes}><option value="alpha">Alpha</option></Select>
    </label></form>))
    const trigger = container.querySelector<HTMLButtonElement>('[role="combobox"]')!
    expect(trigger.disabled).toBe(true)
    await press(trigger, 'ArrowDown')
    expect(document.querySelector('[role="listbox"]')).toBeNull()
    expect(new FormData(container.querySelector('form')!).has('disabled')).toBe(false)
    expect(changes).not.toHaveBeenCalled()
  })

  it('normalizes numeric values and updates translated selected labels without changing value', async () => {
    const render = (label: string) => <Select aria-label="Page" value={2} onValueChange={vi.fn()}><option value={2}>{label}</option><option value={3}>3</option></Select>
    await act(async () => root.render(render('Page two')))
    let trigger = container.querySelector<HTMLElement>('[role="combobox"]')!
    expect(trigger.dataset.value).toBe('2')
    expect(trigger.textContent).toContain('Page two')
    await act(async () => root.render(render('第二页')))
    trigger = container.querySelector<HTMLElement>('[role="combobox"]')!
    expect(trigger.dataset.value).toBe('2')
    expect(trigger.textContent).toContain('第二页')
  })

  it('dismisses Escape without changing the controlled value or notifying the parent key handler', async () => {
    const changes = vi.fn()
    const escaped = vi.fn()
    await act(async () => root.render(<div onKeyDown={(event) => { if (event.key === 'Escape') escaped() }}>
      <Select aria-label="Project" value="alpha" onValueChange={changes}><option value="alpha">Alpha</option><option value="beta">Beta</option></Select>
    </div>))
    const trigger = container.querySelector<HTMLButtonElement>('[role="combobox"]')!
    await act(async () => trigger.focus())
    await press(trigger, 'ArrowDown')
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    await press(document.activeElement!, 'Escape')
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(trigger.dataset.value).toBe('alpha')
    expect(changes).not.toHaveBeenCalled()
    expect(escaped).not.toHaveBeenCalled()
  })

  it('keeps native wrapping and explicit label association on the visible trigger', async () => {
    await act(async () => root.render(<><label htmlFor="explicit">Explicit label</label>
      <Select id="explicit" value="alpha"><option value="alpha">Alpha</option></Select>
      <label>Wrapped label<Select value="beta"><option value="beta">Beta</option></Select></label>
    </>))
    const triggers = container.querySelectorAll<HTMLButtonElement>('[role="combobox"]')
    expect(triggers[0]!.labels?.[0]?.textContent).toBe('Explicit label')
    expect(triggers[1]!.labels?.[0]?.textContent).toContain('Wrapped label')
    await act(async () => triggers[1]!.focus())
    await press(triggers[1]!, 'ArrowDown')
    expect(document.querySelector('[role="listbox"]')?.getAttribute('aria-labelledby')).toBe(triggers[1]!.id)
  })

  it('supports keyboard navigation and prevents activating disabled options and groups', async () => {
    const changes = vi.fn()
    await act(async () => root.render(<Select aria-label="Project" defaultValue="alpha" onValueChange={changes}>
      <option value="alpha">Alpha</option><option value="blocked" disabled>Blocked</option>
      <optgroup label="Archived" disabled><option value="archived">Archived</option></optgroup>
      <option value="beta">Beta</option><option value="gamma">Gamma</option>
    </Select>))
    const trigger = container.querySelector<HTMLButtonElement>('[role="combobox"]')!
    await act(async () => trigger.focus())
    await press(trigger, 'ArrowDown')
    await press(document.activeElement!, 'End')
    expect(document.activeElement?.getAttribute('data-value')).toBe('gamma')
    await press(document.activeElement!, 'Home')
    expect(document.activeElement?.getAttribute('data-value')).toBe('alpha')
    await press(document.activeElement!, 'ArrowDown')
    expect(document.activeElement?.getAttribute('data-value')).toBe('blocked')
    await press(document.activeElement!, 'Enter')
    expect(changes).not.toHaveBeenCalled()
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    await press(document.activeElement!, 'ArrowDown')
    expect(document.activeElement?.getAttribute('data-value')).toBe('archived')
    await press(document.activeElement!, 'Enter')
    expect(changes).not.toHaveBeenCalled()
    await press(document.activeElement!, 'g')
    expect(document.activeElement?.getAttribute('data-value')).toBe('gamma')
    await press(document.activeElement!, 'Enter')
    expect(trigger.dataset.value).toBe('gamma')
    expect(changes).toHaveBeenCalledExactlyOnceWith('gamma')
  })

  it('resets uncontrolled state without synthesizing change, and honors cancelled reset', async () => {
    const changes = vi.fn()
    await act(async () => root.render(<form><Select name="project" defaultValue="alpha" onValueChange={changes}>
      <option value="alpha">Alpha</option><option value="beta">Beta</option>
    </Select></form>))
    const trigger = container.querySelector<HTMLButtonElement>('[role="combobox"]')!
    const form = container.querySelector('form')!
    await act(async () => trigger.focus())
    await press(trigger, 'ArrowDown')
    await act(async () => document.querySelector<HTMLElement>('[role="option"][data-value="beta"]')!.click())
    const cancelReset = (event: Event) => event.preventDefault()
    form.addEventListener('reset', cancelReset)
    await act(async () => form.reset())
    expect(trigger.dataset.value).toBe('beta')
    form.removeEventListener('reset', cancelReset)
    await act(async () => form.reset())
    expect(trigger.dataset.value).toBe('alpha')
    expect(new FormData(form).get('project')).toBe('alpha')
    expect(changes).toHaveBeenCalledExactlyOnceWith('beta')
  })

  it('keeps popup within the modal and consumes Escape before the outer window handler', async () => {
    const close = vi.fn()
    await act(async () => root.render(<LanguageProvider language="en"><ModalDialog open title="Edit" onClose={close}>
      <label>Project<Select value="alpha"><option value="alpha">Alpha</option><option value="beta">Beta</option></Select></label>
    </ModalDialog></LanguageProvider>))
    const trigger = container.querySelector<HTMLButtonElement>('[role="combobox"]')!
    await act(async () => trigger.focus())
    await press(trigger, 'ArrowDown')
    expect(document.querySelector('[role="listbox"]')?.closest('.modalDialog')).not.toBeNull()
    await press(document.activeElement!, 'Escape')
    expect(close).not.toHaveBeenCalled()
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    await press(trigger, 'Escape')
    expect(close).toHaveBeenCalledOnce()
  })


  it('does not clear controlled state when asynchronous options are temporarily missing', async () => {
    const changes = vi.fn()
    const render = (loaded: boolean) => <Select aria-label="Project" value="beta" onValueChange={changes}>
      <option value="alpha">Alpha</option>{loaded && <option value="beta">Beta</option>}
    </Select>
    await act(async () => root.render(render(true)))
    const trigger = container.querySelector<HTMLButtonElement>('[role="combobox"]')!
    await act(async () => trigger.focus())
    await press(trigger, 'ArrowDown')
    await act(async () => root.render(render(false)))
    expect(changes).not.toHaveBeenCalled()
    expect(trigger.dataset.value).toBe('beta')
    await act(async () => root.render(render(true)))
    expect(trigger.textContent).toContain('Beta')
  })


  it('closes and blocks changes when an enclosing fieldset locks an already-open popup', async () => {
    const changes = vi.fn()
    const render = (disabled: boolean) => <fieldset disabled={disabled}>
      <label>Project<Select value="alpha" onValueChange={changes}><option value="alpha">Alpha</option><option value="beta">Beta</option></Select></label>
    </fieldset>
    await act(async () => root.render(render(false)))
    const trigger = container.querySelector<HTMLButtonElement>('[role="combobox"]')!
    await act(async () => trigger.focus())
    await press(trigger, 'ArrowDown')
    const beta = document.querySelector<HTMLElement>('[role="option"][data-value="beta"]')!
    await act(async () => {
      container.querySelector('fieldset')!.disabled = true
      beta.click()
    })
    expect(changes).not.toHaveBeenCalled()
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(trigger.disabled).toBe(true)
    await act(async () => { container.querySelector('fieldset')!.disabled = false })
    expect(trigger.disabled).toBe(false)
    await press(trigger, 'ArrowDown')
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
  })

  it('respects the native first-legend exception and explicit disabled toggling', async () => {
    const render = (disabled: boolean) => <fieldset disabled><legend><Select aria-label="Legend choice" disabled={disabled} value="alpha"><option value="alpha">Alpha</option></Select></legend></fieldset>
    await act(async () => root.render(render(false)))
    const trigger = container.querySelector<HTMLButtonElement>('[role="combobox"]')!
    expect(trigger.disabled).toBe(false)
    await act(async () => root.render(render(true)))
    expect(trigger.disabled).toBe(true)
    await act(async () => root.render(render(false)))
    expect(trigger.disabled).toBe(false)
  })


  it.each([false, true])('returns modal popup Tab to the adjacent field (shift=%s)', async (shiftKey) => {
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue({ length: 1 } as DOMRectList)
    await act(async () => root.render(<LanguageProvider language="en"><ModalDialog open title="Edit" onClose={vi.fn()}>
      <button>Before</button><label>Project<Select value="alpha"><option value="alpha">Alpha</option><option value="beta">Beta</option></Select></label><button>After</button>
    </ModalDialog></LanguageProvider>))
    const trigger = container.querySelector<HTMLButtonElement>('[role="combobox"]')!
    await act(async () => trigger.focus())
    await press(trigger, 'ArrowDown')
    const option = document.activeElement!
    const tab = new KeyboardEvent('keydown', { key: 'Tab', shiftKey, bubbles: true, cancelable: true })
    await act(async () => option.dispatchEvent(tab))
    expect(tab.defaultPrevented).toBe(true)
    expect(document.activeElement?.textContent).toBe(shiftKey ? 'Before' : 'After')
  })


  it('does not restore the initial option when the newer controlled option disappears', async () => {
    const changes = vi.fn()
    const render = (value: string, loaded: boolean) => <Select aria-label="Project" value={value} onValueChange={changes}>
      <option value="alpha">Alpha</option>{loaded && <option value="beta">Beta</option>}
    </Select>
    await act(async () => root.render(render('alpha', true)))
    const trigger = container.querySelector<HTMLButtonElement>('[role="combobox"]')!
    await act(async () => trigger.focus())
    await press(trigger, 'ArrowDown')
    await act(async () => root.render(render('beta', true)))
    await act(async () => root.render(render('beta', false)))
    expect(changes).not.toHaveBeenCalled()
    expect(trigger.dataset.value).toBe('beta')
    await press(document.activeElement!, 'Escape')
    await press(trigger, 'a')
    expect(changes).toHaveBeenCalledExactlyOnceWith('alpha')
  })


  it('wraps Tab from a final modal select and never focuses background controls', async () => {
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue({ length: 1 } as DOMRectList)
    await act(async () => root.render(<LanguageProvider language="en"><ModalDialog open title="Edit" onClose={vi.fn()}>
      <label>Project<Select value="alpha"><option value="alpha">Alpha</option><option value="beta">Beta</option></Select></label>
    </ModalDialog><button>Outside</button></LanguageProvider>))
    const trigger = container.querySelector<HTMLButtonElement>('[role="combobox"]')!
    await act(async () => trigger.focus())
    await press(trigger, 'ArrowDown')
    await press(document.activeElement!, 'Tab')
    expect(document.activeElement?.textContent).toBe('Close')
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
  })


  it('wraps Shift+Tab from a first modal select to the final control', async () => {
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue({ length: 1 } as DOMRectList)
    await act(async () => root.render(<LanguageProvider language="en"><ModalDialog open hideClose title="Edit" onClose={vi.fn()}>
      <Select aria-label="Project" value="alpha"><option value="alpha">Alpha</option></Select><button>Last</button>
    </ModalDialog><button>Outside</button></LanguageProvider>))
    const trigger = container.querySelector<HTMLButtonElement>('[role="combobox"]')!
    await act(async () => trigger.focus())
    await press(trigger, 'ArrowDown')
    await act(async () => document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true })))
    expect(document.activeElement?.textContent).toBe('Last')
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
  })

})
