// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SchemaTaskInput } from '../../src/components/SchemaTaskInput'
import { parseInputObject } from '../../src/lib/taskDraft'

let container: HTMLDivElement
let root: Root
/** 不正な中間文字列も親の原文 state に伝達する編集 harness。 */
function Editor({ requiredObject = false }: { requiredObject?: boolean }) {
  const [raw, setRaw] = useState('{"object":{"old":1},"array":[1],"title":"keep"}')
  return <><SchemaTaskInput schema={{ type: 'object', required: requiredObject ? ['object'] : [], properties: { object: { type: 'object' }, array: { type: 'array' }, title: { type: 'string' } } }}
    value={parseInputObject(raw)} rawValue={raw} onRawChange={setRaw} onChange={(value) => setRaw(JSON.stringify(value))} />
    <output data-valid>{String(parseInputObject(raw) !== null)}</output></>
}
beforeEach(() => { vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true); container = document.createElement('div'); document.body.append(container); root = createRoot(container) })
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals() })
/** React value tracker を迂回して実入力 event を再現する。 */
async function type(element: HTMLTextAreaElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(element, value)
    element.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
describe('Schema task raw drafts', () => {
  it('retains incomplete object and array edits, focus, and whitespace while blocking invalid submission', async () => {
    await act(async () => root.render(<Editor />))
    const [object, array] = [...container.querySelectorAll<HTMLTextAreaElement>('textarea')]
    object!.focus()
    await type(object!, '{')
    expect(object!.value).toBe('{')
    expect(document.activeElement).toBe(object)
    expect(container.querySelector('[data-valid]')!.textContent).toBe('false')
    expect(object!.getAttribute('aria-invalid')).toBe('true')
    await act(async () => {
      const title = container.querySelector<HTMLInputElement>('input')!
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(title, 'edited during invalid JSON')
      title.dispatchEvent(new Event('input', { bubbles: true }))
    })
    expect(container.querySelector<HTMLInputElement>('input')!.value).toBe('edited during invalid JSON')
    await type(array!, '[1,')
    await type(object!, '{ "new": 2 }')
    expect(object!.value).toBe('{ "new": 2 }')
    expect(array!.value).toBe('[1,')
    expect(container.querySelector('[data-valid]')!.textContent).toBe('false')
    await type(array!, '[1, 2]')
    expect(container.querySelector('[data-valid]')!.textContent).toBe('true')
    expect(container.querySelector<HTMLInputElement>('input')!.value).toBe('edited during invalid JSON')
  })
  it('blocks a malformed field that would otherwise inject a valid sibling into the combined object', async () => {
    await act(async () => root.render(<Editor />))
    const object = container.querySelector<HTMLTextAreaElement>('textarea')!
    await type(object, '{}, \"other\": 123')
    expect(object.getAttribute('aria-invalid')).toBe('true')
    expect(container.querySelector('[data-valid]')!.textContent).toBe('false')
    expect(container.querySelector<HTMLInputElement>('input')!.value).toBe('keep')
    await type(object, '{}')
    expect(container.querySelector('[data-valid]')!.textContent).toBe('true')
    expect(container.querySelector('.jsonInput')!.textContent).not.toContain('other')
  })
  it.each([false, true])('treats whitespace consistently for a required=%s complex field', async (required) => {
    await act(async () => root.render(<Editor requiredObject={required} />))
    const object = container.querySelector<HTMLTextAreaElement>('textarea')!
    await type(object, '   ')
    expect(object.getAttribute('aria-invalid')).toBe(required ? 'true' : null)
    expect(container.querySelector('[data-valid]')!.textContent).toBe(String(!required))
    if (!required) expect(parseInputObject(container.querySelector<HTMLTextAreaElement>('.jsonInput')!.value)).not.toHaveProperty('object')
  })
  it('keeps advanced JSON mounted and open through invalid JSON and non-object values', async () => {
    await act(async () => root.render(<Editor />))
    const details = container.querySelector('details')!
    details.open = true
    const raw = details.querySelector('textarea')!
    raw.focus()
    for (const text of ['{', '[]', '{"title":"changed"}']) {
      await type(raw, text)
      expect(container.querySelector('details textarea')).toBe(raw)
      expect(document.activeElement).toBe(raw)
      expect(details.open).toBe(true)
      expect(raw.value).toBe(text)
    }
    expect(container.querySelector<HTMLInputElement>('input')!.value).toBe('changed')
  })
})
