import { marked, type Token } from 'marked'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MERMAID_PREVIEW_TIMEOUT_MS, useMermaidPreviews } from '../../src/hooks/useMermaidPreviews'
import { MERMAID_MAX_PAGE_DIAGRAMS, mermaidCodeTokens, renderMermaidPreview, type MermaidPreview } from '../../src/lib/mermaidPreview'
import { commitHooks, deferred, hookMicrotasks, hookPhases, unmountHooks } from '../fixtures/hookHarness'
import { LIGHT_PREVIEW_THEME, type PreviewTheme } from '../../src/lib/previewTheme'

vi.mock('react', async () => (await import('../fixtures/hookHarness')).hookReact)
vi.mock('../../src/lib/mermaidPreview', async (original) => ({
  ...await original<typeof import('../../src/lib/mermaidPreview')>(), renderMermaidPreview: vi.fn(),
}))
const renderer = vi.mocked(renderMermaidPreview)
/** 出力の内容より所有者の正しさを見る小さい fixture。 */
function tokens(source = 'flowchart LR\nA-->B'): Token[] { return marked.lexer(`\`\`\`mermaid\n${source}\n\`\`\``) }
function render(page?: Token[], enabled = true, theme: PreviewTheme = LIGHT_PREVIEW_THEME) { hookPhases.cursor = 0; return useMermaidPreviews(page, enabled, theme) }
const ready: MermaidPreview = { status: 'ready', svg: '<svg/>', document: '<svg/>', height: 120 }
afterEach(() => { unmountHooks(); vi.resetAllMocks(); vi.clearAllTimers(); vi.useRealTimers() })

describe('lazy Mermaid page lifecycle', () => {
  it('does not render for ordinary prose, hidden pages, or unsupported source', () => {
    render(marked.lexer('A→B→C')); commitHooks()
    render(tokens(), false); commitHooks()
    const unsafe = tokens('flowchart LR\nclick A "https://example.test"')
    expect([...render(unsafe).values()]).toEqual([{ status: 'failed' }]); commitHooks()
    expect(renderer).not.toHaveBeenCalled()
  })
  it('renders only the current page with separate entries for duplicate fences', async () => {
    renderer.mockResolvedValue(ready)
    const page = [...tokens(), ...tokens()]
    expect([...render(page).values()]).toEqual([{ status: 'loading' }, { status: 'loading' }]); commitHooks()
    await hookMicrotasks()
    expect(renderer).toHaveBeenCalledTimes(2)
    expect([...render(page).values()]).toEqual([ready, ready])
  })
  it('rejects old-source completion even before passive cleanup, then aborts old work', async () => {
    const pending = deferred<MermaidPreview>(); renderer.mockReturnValueOnce(pending.promise)
    const first = tokens(); const second = tokens('flowchart TD\nX-->Y')
    render(first); commitHooks()
    const signal = renderer.mock.calls[0]![1]
    expect([...render(second).values()]).toEqual([{ status: 'loading' }])
    pending.resolve(ready); await hookMicrotasks()
    expect([...render(second).values()]).toEqual([{ status: 'loading' }])
    renderer.mockResolvedValue(ready); commitHooks()
    expect(signal.aborted).toBe(true)
    await hookMicrotasks()
    expect(render(second).get(mermaidCodeTokens(second)[0]!)).toEqual(ready)
  })
  it('redraws unchanged tokens for a new theme and discards completion in the previous palette', async () => {
    const pending = deferred<MermaidPreview>(); renderer.mockReturnValueOnce(pending.promise)
    const page = tokens(); render(page); commitHooks()
    const original = renderer.mock.calls[0]![1]
    const dark = { ...LIGHT_PREVIEW_THEME, mode: 'dark' as const, background: '#28251f' }
    expect([...render(page, true, dark).values()]).toEqual([{ status: 'loading' }])
    pending.resolve(ready); await hookMicrotasks()
    expect([...render(page, true, dark).values()]).toEqual([{ status: 'loading' }])
    renderer.mockResolvedValue(ready); commitHooks(); await hookMicrotasks()
    expect(original.aborted).toBe(true)
    expect(renderer.mock.calls.at(-1)![2]).toBe(dark)
    expect([...render(page, true, dark).values()]).toEqual([ready])
  })
  it('aborts pending import/render on unmount and never starts the next diagram', async () => {
    const pending = deferred<MermaidPreview>(); renderer.mockReturnValue(pending.promise)
    render([...tokens(), ...tokens()]); commitHooks()
    const signal = renderer.mock.calls[0]![1]
    unmountHooks(); expect(signal.aborted).toBe(true)
    pending.resolve(ready); await hookMicrotasks()
    expect(renderer).toHaveBeenCalledTimes(1)
  })
  it('shows explicit source fallback at deadline and ignores late completion', async () => {
    vi.useFakeTimers()
    const pending = deferred<MermaidPreview>(); renderer.mockReturnValue(pending.promise)
    const page = tokens(); render(page); commitHooks()
    vi.advanceTimersByTime(MERMAID_PREVIEW_TIMEOUT_MS)
    expect(renderer.mock.calls[0]![1].aborted).toBe(true)
    expect([...render(page).values()]).toEqual([{ status: 'failed' }])
    pending.resolve(ready); await hookMicrotasks()
    expect([...render(page).values()]).toEqual([{ status: 'failed' }])
  })
  it('limits diagrams per page and leaves excess diagrams as explicit source fallback', async () => {
    renderer.mockResolvedValue(ready)
    const page = Array.from({ length: MERMAID_MAX_PAGE_DIAGRAMS + 1 }, () => tokens()).flat()
    render(page); commitHooks(); await hookMicrotasks()
    expect(renderer).toHaveBeenCalledTimes(MERMAID_MAX_PAGE_DIAGRAMS)
    expect([...render(page).values()].at(-1)).toEqual({ status: 'failed' })
  })
  it('rejects late layout success before the timeout callback has been delivered', async () => {
    const pending = deferred<MermaidPreview>(); renderer.mockReturnValue(pending.promise)
    const clock = vi.spyOn(performance, 'now').mockReturnValue(0)
    const page = tokens(); render(page); commitHooks()
    clock.mockReturnValue(MERMAID_PREVIEW_TIMEOUT_MS + 1)
    pending.resolve(ready); await hookMicrotasks()
    expect([...render(page).values()]).toEqual([{ status: 'failed' }])
    clock.mockRestore()
  })
})
