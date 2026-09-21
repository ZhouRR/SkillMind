import { isValidElement, type ReactElement, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ActionMenu, type ActionMenuItem } from '../../src/components/ActionMenu'
import { commitHooks, hookPhases, unmountHooks } from '../fixtures/hookHarness'

vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(),
  ...(await import('../fixtures/hookHarness')).hookReact,
  useId: () => 'action-menu-test',
}))
vi.mock('react-dom', () => ({ createPortal: (children: ReactNode) => <div data-portal>{children}</div> }))

type Element = ReactElement<Record<string, unknown>>
/** 元 callback と commit 境界を検証し、DOM layout の実測は browser 回帰で行う。 */
function find(node: ReactNode, match: (item: Element) => boolean): Element {
  if (isValidElement<Record<string, unknown>>(node)) {
    if (match(node)) return node
    for (const child of [node.props.children].flat()) {
      try { return find(child as ReactNode, match) } catch { /* 次の同階層 node を探す。 */ }
    }
  } else if (Array.isArray(node)) {
    for (const child of node) { try { return find(child, match) } catch { /* 次へ。 */ } }
  }
  throw new Error('Node not found')
}
/** React の handler を同 tick のまま呼び、二重 callback を観測する。 */
function invoke(element: Element, handler: string, event: unknown = {}): void {
  ;(element.props[handler] as (value: unknown) => void)(event)
}

/** focus と event listener の副作用だけを記録する最小 DOM fixture。 */
class FakeNode {
  style: Record<string, string> = {}
  scrollHeight = 130
  children: FakeNode[] = []
  bounds = { top: 120, bottom: 156, right: 750 }
  focus = vi.fn(() => { browserDocument.activeElement = this })
  contains(node: FakeNode): boolean { return node === this || this.children.includes(node) }
  querySelectorAll(): FakeNode[] { return this.children }
  getBoundingClientRect() { return this.bounds }
}
let documentListeners: Map<string, EventListener>
let windowListeners: Map<string, EventListener>
let browserDocument: { body: FakeNode; activeElement: FakeNode | null; addEventListener: unknown; removeEventListener: unknown }
let browserWindow: { innerWidth: number; innerHeight: number; addEventListener: unknown; removeEventListener: unknown }
let button: FakeNode
let menu: FakeNode
let items: ActionMenuItem[]
let called: ReturnType<typeof vi.fn<() => void>>

/** Menu に ref を接続してから layout cleanup/setup を実行する。 */
function render(props: Partial<Parameters<typeof ActionMenu>[0]> = {}, commit = true): ReactNode {
  hookPhases.cursor = 0
  const tree = ActionMenu({ label: '仕様書.md の操作', ownerKey: 'doc-1', items, ...props })
  if (tree) {
    const trigger = find(tree, (node) => node.props.className === 'actionMenuTrigger')
    ;(trigger.props.ref as { current: FakeNode }).current = button
  }
  try { (find(tree, (node) => node.props.role === 'menu').props.ref as { current: FakeNode }).current = menu } catch { /* 閉状態。 */ }
  if (commit) commitHooks()
  return tree
}
function event(key = '') { return { key, preventDefault: vi.fn(), stopPropagation: vi.fn() } }
function open(): ReactNode {
  const tree = render()
  invoke(find(tree, (node) => node.props.className === 'actionMenuTrigger'), 'onClick', event())
  return render()
}
function item(tree: ReactNode, label: string): Element { return find(tree, (node) => node.props.role === 'menuitem' && node.props.children === label) }

beforeEach(() => {
  documentListeners = new Map()
  windowListeners = new Map()
  browserDocument = { body: new FakeNode(), activeElement: null,
    addEventListener: (key: string, handler: EventListener) => documentListeners.set(key, handler),
    removeEventListener: (key: string) => documentListeners.delete(key) }
  browserWindow = { innerWidth: 800, innerHeight: 600,
    addEventListener: (key: string, handler: EventListener) => windowListeners.set(key, handler),
    removeEventListener: (key: string) => windowListeners.delete(key) }
  vi.stubGlobal('Node', FakeNode)
  vi.stubGlobal('document', browserDocument)
  vi.stubGlobal('window', browserWindow)
  button = new FakeNode()
  menu = new FakeNode()
  menu.children = [new FakeNode(), new FakeNode(), new FakeNode()]
  called = vi.fn()
  items = [
    { id: 'download', label: 'Download', href: '/project/doc/original', download: '仕様書.md' },
    { id: 'rename', label: 'Rename', onSelect: called },
    { id: 'locked', label: 'Unavailable', disabled: true, onSelect: called },
    { id: 'delete', label: 'Recycle', danger: true, separatorBefore: true, onSelect: called },
  ]
})
afterEach(() => { unmountHooks(); vi.unstubAllGlobals() })

describe('ActionMenu accessible entry and original actions', () => {
  it('keeps the named trigger visible while closed and no action entry for an empty list', () => {
    const tree = render({ disabled: true })
    const trigger = find(tree, (node) => node.type === 'button')
    expect(trigger.props['aria-label']).toBe('仕様書.md の操作')
    expect(trigger.props['aria-haspopup']).toBe('menu')
    expect(trigger.props['aria-expanded']).toBe(false)
    expect(trigger.props.disabled).toBe(true)
    expect(() => find(tree, (node) => node.props.role === 'menu')).toThrow()
    expect(render({ items: [] })).toBeNull()
  })
  it('preserves native download links and visibly separates the destructive action', () => {
    const tree = open()
    expect(item(tree, 'Download').type).toBe('a')
    expect(item(tree, 'Download').props).toMatchObject({ href: '/project/doc/original', download: '仕様書.md' })
    expect(item(tree, 'Recycle').props.className).toContain('actionMenuDanger')
    expect(find(tree, (node) => node.props.role === 'separator')).toBeTruthy()
  })
  it('closes and restores focus before opening an action dialog, only once per selection', () => {
    called.mockImplementation(() => expect(browserDocument.activeElement).toBe(button))
    const original = item(open(), 'Rename')
    invoke(original, 'onClick')
    invoke(original, 'onClick')
    expect(called).toHaveBeenCalledTimes(1)
    expect(() => find(render(), (node) => node.props.role === 'menu')).toThrow()
  })
  it('rejects disabled actions including an old callback after permissions change', () => {
    const tree = open()
    invoke(item(tree, 'Unavailable'), 'onClick')
    render({ items: items.map((entry) => ({ ...entry, disabled: true })) })
    invoke(item(tree, 'Rename'), 'onClick')
    expect(called).not.toHaveBeenCalled()
  })
  it.each(['owner', 'disabled', 'unmount'])('closes old actions on %s without dispatch', (change) => {
    const original = item(open(), 'Rename')
    if (change === 'owner') render({ ownerKey: 'doc-2' }, false)
    else if (change === 'disabled') render({ disabled: true }, false)
    else unmountHooks()
    invoke(original, 'onClick')
    expect(called).not.toHaveBeenCalled()
  })
})

describe('ActionMenu keyboard and dismissal ownership', () => {
  it('focuses the first action and supports directional, Home and End navigation', () => {
    const tree = open()
    const panel = find(tree, (node) => node.props.role === 'menu')
    expect(browserDocument.activeElement).toBe(menu.children[0])
    invoke(panel, 'onKeyDown', event('ArrowUp'))
    expect(browserDocument.activeElement).toBe(menu.children[2])
    invoke(panel, 'onKeyDown', event('Home'))
    expect(browserDocument.activeElement).toBe(menu.children[0])
    invoke(panel, 'onKeyDown', event('ArrowDown'))
    expect(browserDocument.activeElement).toBe(menu.children[1])
    invoke(panel, 'onKeyDown', event('End'))
    expect(browserDocument.activeElement).toBe(menu.children[2])
  })
  it.each(['Escape', 'Tab'])('returns to the trigger on %s, retaining native Tab traversal', (key) => {
    const keyboard = event(key)
    invoke(find(open(), (node) => node.props.role === 'menu'), 'onKeyDown', keyboard)
    expect(browserDocument.activeElement).toBe(button)
    expect(keyboard.preventDefault).toHaveBeenCalledTimes(key === 'Escape' ? 1 : 0)
    expect(() => find(render(), (node) => node.props.role === 'menu')).toThrow()
  })
  it('closes on outside pointer events without taking their focus and cleans listeners', () => {
    open()
    const other = new FakeNode()
    browserDocument.activeElement = other
    documentListeners.get('pointerdown')?.({ target: other } as unknown as Event)
    render()
    expect(browserDocument.activeElement).toBe(other)
    expect(documentListeners.size).toBe(0)
    expect(windowListeners.size).toBe(0)
  })
  it('keeps a long menu inside a narrow short viewport', () => {
    browserWindow.innerWidth = 390
    browserWindow.innerHeight = 320
    button.bounds = { top: 274, bottom: 310, right: 382 }
    menu.scrollHeight = 700
    open()
    expect(menu.style).toMatchObject({ width: '272px', maxHeight: '304px', left: '110px', top: '8px' })
  })
})
