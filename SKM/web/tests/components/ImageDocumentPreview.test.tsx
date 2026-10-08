import { isValidElement, type CSSProperties, type ReactElement, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

import { ImageDocumentPreview } from '../../src/components/ImageDocumentPreview'
import { LoadingSkeleton } from '../../src/components/PageElements'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { commitHooks, hookPhases, unmountHooks } from '../fixtures/hookHarness'

vi.mock('react', async (original) => ({ ...await original<typeof import('react')>(),
  ...(await import('../fixtures/hookHarness')).hookReact }))
vi.mock('../../src/i18n', () => ({ useMessages: () => MESSAGES.zh }))

/** 実 JSX の公開表示属性と event だけを扱い、browser の画像 decode は模倣しない。 */
interface PreviewProps {
  children?: ReactNode; className?: string; role?: string; label?: string
  src?: string; alt?: string; referrerPolicy?: string; style?: CSSProperties
  'aria-busy'?: boolean; onClick?: () => void; 'aria-pressed'?: boolean; onLoad?: () => void; onError?: () => void
}
type Element = ReactElement<PreviewProps>
const labels = MESSAGES.zh.documentsPanel
let first: Blob
let second: Blob
let createUrl: MockInstance<typeof URL.createObjectURL>
let revokeUrl: MockInstance<typeof URL.revokeObjectURL>

/** 描画と commit を分離し、旧 img が layout cleanup より先に発火する競合を再現する。 */
function render(blob = first, title = '预览图片.png'): Element {
  hookPhases.cursor = 0
  return ImageDocumentPreview({ blob, title })
}

/** URL の effect を commit 後、その state を反映する次の描画を返す。 */
function mount(blob = first): Element {
  render(blob)
  commitHooks()
  return render(blob)
}

/** Function component を実行せず、画像と loading/error の JSX 配線を探索する。 */
function elements(node: ReactNode, predicate: (item: Element) => boolean): Element[] {
  if (Array.isArray(node)) return node.flatMap((child) => elements(child, predicate))
  if (!isValidElement<PreviewProps>(node)) return []
  return [...(predicate(node) ? [node] : []), ...elements(node.props.children, predicate)]
}

/** 対象の欠落や重複を隠さず、一つの画像 event owner を選ぶ。 */
function image(node: ReactNode): Element {
  const found = elements(node, (item) => item.type === 'img')
  expect(found).toHaveLength(1)
  return found[0]!
}

/** Shared catalog の loading 文言を維持し、失敗表示と併存させない。 */
function expectLoading(node: Element): void {
  expect(node.props['aria-busy']).toBe(true)
  const skeletons = elements(node, (item) => item.type === LoadingSkeleton)
  expect(skeletons).toHaveLength(1)
  expect(skeletons[0]!.props.label).toBe(labels.loadingPreview)
  expect(elements(node, (item) => item.props.role === 'alert')).toHaveLength(0)
}

/** エラーは三語 catalog の既存文言を使い、broken image と loading を残さない。 */
function expectError(node: Element): void {
  expect(node.props['aria-busy']).toBe(false)
  const alerts = elements(node, (item) => item.props.role === 'alert')
  expect(alerts).toHaveLength(1)
  expect(alerts[0]!.props.children).toBe(labels.failures.loadFailed)
  expect(elements(node, (item) => item.type === LoadingSkeleton || item.type === 'img')).toHaveLength(0)
}

beforeEach(() => {
  first = new Blob(['first image'], { type: 'image/png' })
  second = new Blob(['second image'], { type: 'image/png' })
  let nextUrl = 0
  createUrl = vi.spyOn(URL, 'createObjectURL').mockImplementation(() => `blob:image-${++nextUrl}`)
  revokeUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
})
afterEach(() => { unmountHooks(); vi.restoreAllMocks() })

describe('ImageDocumentPreview loading and decode state', () => {
  it('shows loading before creating a URL, then keeps the image hidden until its load event', () => {
    const initial = render()
    expectLoading(initial)
    expect(elements(initial, (item) => item.type === 'img')).toHaveLength(0)
    expect(createUrl).not.toHaveBeenCalled()
    commitHooks()
    const pending = render()
    expectLoading(pending)
    expect(createUrl).toHaveBeenCalledExactlyOnceWith(first)
    expect(image(pending).props).toMatchObject({
      src: 'blob:image-1', alt: '预览图片.png', referrerPolicy: 'no-referrer', style: { visibility: 'hidden' },
    })
    image(pending).props.onLoad!()
    const ready = render()
    expect(ready.props['aria-busy']).toBe(false)
    expect(image(ready).props.style?.visibility).toBe('visible')
    expect(elements(ready, (item) => item.type === LoadingSkeleton || item.props.role === 'alert')).toHaveLength(0)
    expect(revokeUrl).not.toHaveBeenCalled()
  })

  it('replaces a broken image with the localized error and releases its URL on unmount', () => {
    image(mount()).props.onError!()
    expectError(render())
    unmountHooks()
    expect(revokeUrl).toHaveBeenCalledExactlyOnceWith('blob:image-1')
  })

  it('does not recreate a URL or reset a decoded image when its Blob or title rerenders', () => {
    image(mount()).props.onLoad!()
    const ready = render(first, '改名后的图片.png')
    commitHooks()
    expect(image(ready).props).toMatchObject({ alt: '改名后的图片.png', src: 'blob:image-1', style: { visibility: 'visible' } })
    render(first, '再改名.png')
    commitHooks()
    expect(createUrl).toHaveBeenCalledTimes(1)
    expect(revokeUrl).not.toHaveBeenCalled()
  })

  it('retains a same-Blob decode error but retries that Blob after switching away and back', () => {
    image(mount()).props.onError!()
    expectError(render(first, '改名后的图片.png'))
    commitHooks()
    expect(createUrl).toHaveBeenCalledTimes(1)
    render(second)
    commitHooks()
    expectLoading(render(second))
    render(first)
    commitHooks()
    const retry = render()
    expectLoading(retry)
    expect(image(retry).props.src).toBe('blob:image-3')
    image(retry).props.onLoad!()
    expect(image(render()).props.style?.visibility).toBe('visible')
    expect(createUrl.mock.calls.map(([blob]) => blob)).toEqual([first, second, first])
    expect(revokeUrl.mock.calls).toEqual([['blob:image-1'], ['blob:image-2']])
  })

  it('reports URL creation failure without rendering an empty image or revoking a nonexistent URL', () => {
    createUrl.mockImplementationOnce(() => { throw new Error('Object URL unavailable') })
    expectError(mount())
    render()
    commitHooks()
    expect(createUrl).toHaveBeenCalledTimes(1)
    unmountHooks()
    expect(revokeUrl).not.toHaveBeenCalled()
  })
})

describe('ImageDocumentPreview resource ownership', () => {
  it('survives StrictMode-style cleanup/setup replay without revoking or updating the new owner', () => {
    render()
    // StrictMode と同じ cleanup → setup を同じ mount と Blob 上で再実行する。
    const replay = hookPhases.layout[0]!
    commitHooks()
    const original = image(render())
    replay()
    const current = render()
    expectLoading(current)
    expect(image(current).props.src).toBe('blob:image-2')
    expect(createUrl.mock.calls.map(([blob]) => blob)).toEqual([first, first])
    expect(revokeUrl).toHaveBeenCalledExactlyOnceWith('blob:image-1')
    original.props.onLoad!()
    original.props.onError!()
    expectLoading(render())
    image(current).props.onLoad!()
    expect(image(render()).props.style?.visibility).toBe('visible')
    unmountHooks()
    expect(revokeUrl.mock.calls).toEqual([['blob:image-1'], ['blob:image-2']])
  })

  it('hides the previous image before replacement effects and revokes exactly the replaced URL', () => {
    const original = image(mount())
    original.props.onLoad!()
    expect(image(render()).props.style?.visibility).toBe('visible')
    const replacing = render(second)
    expectLoading(replacing)
    expect(elements(replacing, (item) => item.type === 'img')).toHaveLength(0)
    expect(createUrl).toHaveBeenCalledTimes(1)
    expect(revokeUrl).not.toHaveBeenCalled()
    commitHooks()
    const next = image(render(second))
    expect(next.props.src).toBe('blob:image-2')
    expect(next.key).not.toBe(original.key)
    expect(next.props.style?.visibility).toBe('hidden')
    expect(createUrl).toHaveBeenNthCalledWith(2, second)
    expect(revokeUrl).toHaveBeenCalledExactlyOnceWith('blob:image-1')
    next.props.onLoad!()
    expect(image(render(second)).props.style?.visibility).toBe('visible')
    unmountHooks()
    expect(revokeUrl.mock.calls).toEqual([['blob:image-1'], ['blob:image-2']])
  })

  it.each(['onLoad', 'onError'] as const)('ignores stale %s both before and after replacement cleanup', (event) => {
    const original = image(mount())
    render(second)
    const beforeCommit = hookPhases.slots.map((slot) => slot.value)
    original.props[event]!()
    // 非表示だから安全と誤認せず、commit 前の旧 state にも書込がないことを確認する。
    expect(hookPhases.slots.map((slot) => slot.value)).toEqual(beforeCommit)
    commitHooks()
    const pending = render(second)
    original.props[event]!()
    expectLoading(render(second))
    expect(image(render(second)).props.src).toBe('blob:image-2')
    image(pending).props.onLoad!()
    original.props[event]!()
    expect(image(render(second)).props.style?.visibility).toBe('visible')
    expect(revokeUrl).toHaveBeenCalledExactlyOnceWith('blob:image-1')
  })

  it.each(['onLoad', 'onError'] as const)('does not write through stale %s after unmount or affect a new mount', (event) => {
    const original = image(mount())
    const oldSlots = [...hookPhases.slots]
    unmountHooks()
    const afterUnmount = oldSlots.map((slot) => slot.value)
    original.props[event]!()
    expect(oldSlots.map((slot) => slot.value)).toEqual(afterUnmount)
    const next = mount()
    original.props[event]!()
    expectLoading(render())
    expect(image(next).props.src).toBe('blob:image-2')
    image(next).props.onLoad!()
    expect(image(render()).props.style?.visibility).toBe('visible')
    expect(revokeUrl).toHaveBeenCalledExactlyOnceWith('blob:image-1')
  })

  it('revokes the old URL when replacement creation fails, ignores old events, and recovers for a new Blob', () => {
    const original = image(mount())
    original.props.onLoad!()
    createUrl.mockImplementationOnce(() => { throw new Error('Object URL unavailable') })
    render(second)
    commitHooks()
    expectError(render(second))
    original.props.onLoad!()
    original.props.onError!()
    expectError(render(second))
    expect(revokeUrl).toHaveBeenCalledExactlyOnceWith('blob:image-1')
    render(first)
    commitHooks()
    const recovered = render()
    expectLoading(recovered)
    expect(image(recovered).props.src).toBe('blob:image-2')
    image(recovered).props.onLoad!()
    expect(image(render()).props.style?.visibility).toBe('visible')
    unmountHooks()
    expect(revokeUrl.mock.calls).toEqual([['blob:image-1'], ['blob:image-2']])
  })

  it('releases a still-loading URL when the preview is closed', () => {
    expectLoading(mount())
    unmountHooks()
    expect(revokeUrl).toHaveBeenCalledExactlyOnceWith('blob:image-1')
  })
})

/** 拡大は表示だけを変え、検証済み Blob と decode owner を再発行しない。 */
describe('image preview size controls', () => {
  it('toggles original dimensions without recreating the Blob URL', () => {
    image(mount()).props.onLoad!()
    const before = render()
    const toggle = elements(before, (item) => item.type === 'button')[0]!
    expect(toggle.props['aria-pressed']).toBe(false)
    toggle.props.onClick!()
    const original = render()
    expect(elements(original, (item) => item.props.className === 'imagePreviewViewport imagePreviewOriginal')).toHaveLength(1)
    expect(elements(original, (item) => item.type === 'button')[0]!.props['aria-pressed']).toBe(true)
    expect(createUrl).toHaveBeenCalledTimes(1)
  })
})
