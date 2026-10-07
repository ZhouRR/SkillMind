// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DocumentFolderInput } from '../../src/components/DocumentFolderInput'
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

/** 実 input の native setter を通して React の編集イベントを発火する。 */
async function type(input: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }))
  })
}

/** 親が所有する upload 先をそのまま返し、実際の再 render を含めて検証する。 */
function Field({ changes, folders = ['specs/api', 'assets'], disabled = false }: {
  changes: (value: string) => void; folders?: string[]; disabled?: boolean
}) {
  const [value, setValue] = useState('')
  return <LanguageProvider language="en"><form onSubmit={(event) => event.preventDefault()}>
    <DocumentFolderInput value={value} folders={folders} disabled={disabled}
      onValueChange={(next) => { changes(next); setValue(next) }} />
    <button type="button">After</button>
  </form></LanguageProvider>
}

describe('upload folder autocomplete', () => {
  it('opens a DOM list with root and hierarchy, selects once, and preserves input focus', async () => {
    const changes = vi.fn()
    await act(async () => root.render(<Field changes={changes} />))
    const input = container.querySelector<HTMLInputElement>('input[role="combobox"]')!
    expect(input).not.toBeNull()
    expect(input.hasAttribute('list')).toBe(false)
    expect(container.querySelector('datalist')).toBeNull()
    await act(async () => input.focus())
    await press(input, 'ArrowDown')
    expect(input.getAttribute('aria-expanded')).toBe('true')
    expect([...document.querySelectorAll<HTMLElement>('[role="option"]')].map((node) => node.dataset.folderPath))
      .toEqual(['', 'assets', 'specs', 'specs/api'])
    await act(async () => document.querySelector<HTMLElement>('[data-folder-path="specs/api"]')!.click())
    expect(input.value).toBe('specs/api')
    expect(changes).toHaveBeenCalledExactlyOnceWith('specs/api')
    expect(input.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(input)
    await press(input, 'ArrowDown')
    await act(async () => document.querySelector<HTMLElement>('[data-folder-path=""]')!.click())
    expect(input.value).toBe('')
    expect(changes).toHaveBeenLastCalledWith('')
  })

  it('keeps arbitrary new paths through typing, Escape, and blur without selecting a suggestion', async () => {
    const changes = vi.fn()
    await act(async () => root.render(<Field changes={changes} />))
    const input = container.querySelector<HTMLInputElement>('input[role="combobox"]')!
    await act(async () => input.focus())
    await type(input, 'new/custom-folder')
    expect(input.value).toBe('new/custom-folder')
    expect(changes).toHaveBeenCalledExactlyOnceWith('new/custom-folder')
    expect(input.getAttribute('aria-expanded')).toBe('true')
    await press(input, 'Escape')
    expect(input.value).toBe('new/custom-folder')
    await act(async () => container.querySelector<HTMLButtonElement>('button[type="button"]:last-child')!.focus())
    expect(input.value).toBe('new/custom-folder')
    expect(changes).toHaveBeenCalledExactlyOnceWith('new/custom-folder')
  })

  it('retains popup and item identity on same-candidate parent renders and async folder updates', async () => {
    const changes = vi.fn()
    await act(async () => root.render(<Field changes={changes} />))
    const input = container.querySelector<HTMLInputElement>('input[role="combobox"]')!
    await act(async () => input.focus())
    await press(input, 'ArrowDown')
    const popup = document.querySelector('[role="listbox"]')!
    const item = document.querySelector('[data-folder-path="specs/api"]')!
    for (let index = 0; index < 4; index++) await press(input, 'ArrowDown')
    expect(item.hasAttribute('data-highlighted')).toBe(true)
    const highlightedId = input.getAttribute('aria-activedescendant')
    await act(async () => root.render(<Field changes={changes} folders={['assets', 'specs/api']} />))
    expect(document.querySelector('[role="listbox"]')).toBe(popup)
    expect(document.querySelector('[data-folder-path="specs/api"]')).toBe(item)
    expect(input.getAttribute('aria-activedescendant')).toBe(highlightedId)
    expect(item.hasAttribute('data-highlighted')).toBe(true)
    expect(document.activeElement).toBe(input)
    expect(input.value).toBe('')
    await act(async () => root.render(<Field changes={changes} folders={['assets', 'specs/api', 'new/child']} />))
    expect(document.querySelector('[role="listbox"]')).toBe(popup)
    expect(document.querySelector('[data-folder-path="specs/api"]')).toBe(item)
    expect(input.getAttribute('aria-expanded')).toBe('true')
    expect(changes).not.toHaveBeenCalled()
  })

  it('opens and closes with the arrow without submitting or clearing the typed path', async () => {
    const changes = vi.fn()
    await act(async () => root.render(<Field changes={changes} />))
    const input = container.querySelector<HTMLInputElement>('input[role="combobox"]')!
    const trigger = container.querySelector<HTMLButtonElement>('.documentFolderTrigger')!
    const submit = vi.fn((event: Event) => event.preventDefault())
    container.querySelector('form')!.addEventListener('submit', submit)
    await type(input, 'specs')
    await press(input, 'Escape')
    await act(async () => { trigger.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 })); trigger.click() })
    expect(input.getAttribute('aria-expanded')).toBe('true')
    await act(async () => { trigger.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 })); trigger.click() })
    expect(input.getAttribute('aria-expanded')).toBe('false')
    expect(input.value).toBe('specs')
    expect(submit).not.toHaveBeenCalled()
  })

  it('disables both input and arrow while preserving a typed destination', async () => {
    const changes = vi.fn()
    await act(async () => root.render(<Field changes={changes} />))
    const input = container.querySelector<HTMLInputElement>('input[role="combobox"]')!
    await act(async () => input.focus())
    await type(input, 'new/path')
    expect(input.getAttribute('aria-expanded')).toBe('true')
    await act(async () => root.render(<Field changes={changes} disabled />))
    expect(input.disabled).toBe(true)
    expect(container.querySelector<HTMLButtonElement>('.documentFolderTrigger')!.disabled).toBe(true)
    expect(input.value).toBe('new/path')
    expect(input.getAttribute('aria-expanded')).toBe('false')
  })
  it('keeps the typed path through candidate disappearance and restores suggestions without changing it', async () => {
    const changes = vi.fn()
    await act(async () => root.render(<Field changes={changes} />))
    const input = container.querySelector<HTMLInputElement>('input[role="combobox"]')!
    await type(input, 'specs/api')
    await press(input, 'ArrowDown')
    await act(async () => root.render(<Field changes={changes} folders={[]} />))
    expect(input.value).toBe('specs/api')
    expect(document.querySelector('[data-folder-path=""]')).not.toBeNull()
    await act(async () => root.render(<Field changes={changes} />))
    expect(input.value).toBe('specs/api')
    expect(changes).toHaveBeenCalledExactlyOnceWith('specs/api')
  })

  it('selects with keyboard without submitting, and Tab closes without changing the draft', async () => {
    const changes = vi.fn()
    await act(async () => root.render(<Field changes={changes} />))
    const input = container.querySelector<HTMLInputElement>('input[role="combobox"]')!
    await act(async () => input.focus())
    await press(input, 'ArrowDown')
    await press(input, 'ArrowDown')
    await press(input, 'ArrowDown')
    expect(document.querySelector('[data-highlighted]')?.getAttribute('data-folder-path')).toBe('assets')
    await press(input, 'Enter')
    expect(input.value).toBe('assets')
    expect(changes).toHaveBeenCalledExactlyOnceWith('assets')
    await press(input, 'ArrowDown')
    await press(input, 'Tab')
    await act(async () => container.querySelector<HTMLButtonElement>('button:last-child')!.focus())
    expect(input.getAttribute('aria-expanded')).toBe('false')
    expect(input.value).toBe('assets')
  })

  it.each(['ja', 'zh', 'en'] as const)('associates the label and supports root-only input in %s', async (language) => {
    const changes = vi.fn()
    await act(async () => root.render(<LanguageProvider language={language}>
      <DocumentFolderInput value="" folders={[]} disabled={false} onValueChange={changes} />
    </LanguageProvider>))
    const input = container.querySelector<HTMLInputElement>('input[role="combobox"]')!
    expect(input.labels?.length).toBe(1)
    expect(input.labels?.[0]?.textContent).not.toContain(input.placeholder)
    await act(async () => input.focus())
    await press(input, 'ArrowDown')
    expect(document.querySelector('[role="option"]')?.textContent).toBe(input.placeholder)
    expect(changes).not.toHaveBeenCalled()
  })

})
