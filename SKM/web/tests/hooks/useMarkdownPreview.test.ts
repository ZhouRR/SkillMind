import { marked } from 'marked'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MARKDOWN_PREVIEW_WORKER_TIMEOUT_MS, useMarkdownPreview } from '../../src/hooks/useMarkdownPreview'
import type { MarkdownPreviewPage } from '../../src/lib/markdownPreviewPages'
import type { MarkdownPreviewWorkerRequest, MarkdownPreviewWorkerResponse } from '../../src/lib/markdownPreviewWorker'
import { DOCUMENT_PREVIEW_MAX_BYTES } from '../../src/lib/previewLimits'
import { commitHooks, hookPhases, unmountHooks } from '../fixtures/hookHarness'

vi.mock('react', async () => (await import('../fixtures/hookHarness')).hookReact)

/** 本物の Worker と同様に、terminate 後の queued message も意図的に配送する。 */
class FakeWorker {
  static instances: FakeWorker[] = []
  onmessage: ((event: MessageEvent<MarkdownPreviewWorkerResponse>) => void) | null = null
  onerror: (() => void) | null = null
  onmessageerror: (() => void) | null = null
  postMessage = vi.fn<(message: MarkdownPreviewWorkerRequest) => void>()
  terminate = vi.fn()
  constructor(readonly url: URL, readonly options: WorkerOptions) { FakeWorker.instances.push(this) }
  emit(response: MarkdownPreviewWorkerResponse): void {
    this.onmessage?.({ data: response } as MessageEvent<MarkdownPreviewWorkerResponse>)
  }
}

const LARGE = 'A **paragraph**.\n\n'.repeat(4000)

/** 表示内容だけを区別する小さい page fixture。 */
function page(source: string): MarkdownPreviewPage { return { source, tokens: [], heading: '', sourceOnly: false } }

/** render と commit の間にも旧 owner の応答を注入できるようにする。 */
function render(source = LARGE, enabled = true, mode: 'document' | 'reading' = 'document') {
  hookPhases.cursor = 0
  return useMarkdownPreview(source, enabled, mode)
}

/** 初回 load に対する一頁のみの正常応答を返す。 */
function ready(worker: FakeWorker, count = 3): void {
  worker.emit({ type: 'page', requestId: 1, count, index: 0, page: page('first') })
}

beforeEach(() => {
  FakeWorker.instances = []
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'performance'] })
  vi.stubGlobal('Worker', FakeWorker)
})
afterEach(() => { unmountHooks(); vi.restoreAllMocks(); vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals() })

/** main thread での全文解析を禁じ、所有者・deadline・頁 request の取消を検証する。 */
describe('Markdown preview worker lifecycle', () => {
  it('renders small documents synchronously for SSR and keeps reading-mode breaks', () => {
    const result = render('first\nsecond', true, 'reading')
    expect(result.status).toBe('ready')
    expect(marked.parser(result.page!.tokens)).toContain('<br>')
    expect(FakeWorker.instances).toHaveLength(0)
  })

  it('moves larger documents off the main thread and transfers only the selected page back', () => {
    const lexer = vi.spyOn(marked, 'lexer')
    expect(render().status).toBe('loading'); commitHooks()
    const worker = FakeWorker.instances[0]!
    expect(worker.url.pathname).toContain('/workers/markdownPreview.worker.ts')
    expect(worker.options).toEqual({ type: 'module' })
    expect(worker.postMessage).toHaveBeenCalledExactlyOnceWith({ type: 'load', requestId: 1, source: LARGE, mode: 'document' })
    expect(lexer).not.toHaveBeenCalled()
    ready(worker)
    expect(render()).toMatchObject({ status: 'ready', count: 3, index: 0, page: { source: 'first' } })
    render().select(2)
    expect(worker.postMessage).toHaveBeenLastCalledWith({ type: 'page', requestId: 2, index: 2 })
    expect(render()).toMatchObject({ status: 'loading', page: null, count: 3, index: 2 })
    worker.emit({ type: 'page', requestId: 2, count: 3, index: 2, page: page('last') })
    expect(render()).toMatchObject({ status: 'ready', page: { source: 'last' }, index: 2 })
  })

  it('discards out-of-order page replies and rejects invalid page indexes', () => {
    render(); commitHooks(); const worker = FakeWorker.instances[0]!; ready(worker)
    const current = render()
    for (const index of [-1, 3, 0.5, Number.NaN]) current.select(index)
    expect(worker.postMessage).toHaveBeenCalledTimes(1)
    current.select(2); current.select(1)
    worker.emit({ type: 'page', requestId: 2, count: 3, index: 2, page: page('stale') })
    expect(render()).toMatchObject({ status: 'loading', index: 1, page: null })
    worker.emit({ type: 'page', requestId: 3, count: 3, index: 1, page: page('selected') })
    expect(render()).toMatchObject({ status: 'ready', index: 1, page: { source: 'selected' } })
  })

  it('ignores old-source replies before cleanup and terminates the old worker on commit', () => {
    render(); commitHooks(); const previous = FakeWorker.instances[0]!
    const changed = LARGE + 'New source'
    expect(render(changed).status).toBe('loading')
    ready(previous)
    expect(render(changed)).toMatchObject({ status: 'loading', page: null })
    commitHooks()
    expect(previous.terminate).toHaveBeenCalled()
    ready(previous)
    const next = FakeWorker.instances.at(-1)!
    expect(next).not.toBe(previous)
    expect(render(changed)).toMatchObject({ status: 'loading', page: null })
    ready(next)
    expect(render(changed).status).toBe('ready')
  })

  it('terminates parsing when hidden or unmounted and never applies a queued reply', () => {
    render(); commitHooks(); const worker = FakeWorker.instances[0]!
    render(LARGE, false); commitHooks()
    expect(worker.terminate).toHaveBeenCalledExactlyOnceWith()
    ready(worker)
    expect(render(LARGE, false)).toMatchObject({ status: 'idle', page: null })
    render(); commitHooks(); const next = FakeWorker.instances.at(-1)!
    unmountHooks(); expect(next.terminate).toHaveBeenCalledExactlyOnceWith()
    ready(next)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('terminates a timed-out lexer and exposes bounded, navigable source fallback', () => {
    render(); commitHooks(); const worker = FakeWorker.instances[0]!
    vi.advanceTimersByTime(MARKDOWN_PREVIEW_WORKER_TIMEOUT_MS)
    expect(worker.terminate).toHaveBeenCalledExactlyOnceWith()
    const failed = render()
    expect(failed).toMatchObject({ status: 'error', failure: 'timeout', page: { sourceOnly: true } })
    expect(failed.page!.source.length).toBeLessThanOrEqual(32_000)
    expect(failed.count).toBeGreaterThan(1)
    failed.select(failed.count - 1)
    const last = render()
    expect(last).toMatchObject({ status: 'error', failure: 'timeout', index: failed.count - 1 })
    expect(LARGE.endsWith(last.page!.source)).toBe(true)
    ready(worker)
    expect(render().failure).toBe('timeout')
  })

  it('rejects a response after the absolute deadline even if its timer has not run', () => {
    render(); commitHooks(); const worker = FakeWorker.instances[0]!
    vi.spyOn(performance, 'now').mockReturnValue(MARKDOWN_PREVIEW_WORKER_TIMEOUT_MS + 1)
    ready(worker)
    expect(render()).toMatchObject({ status: 'error', failure: 'timeout' })
    expect(worker.terminate).toHaveBeenCalledExactlyOnceWith()
  })

  it.each(['onerror', 'onmessageerror'] as const)('reports %s and ignores later queued success', (kind) => {
    render(); commitHooks(); const worker = FakeWorker.instances[0]!
    worker[kind]?.()
    expect(render()).toMatchObject({ status: 'error', failure: 'parseFailed', page: { sourceOnly: true } })
    ready(worker)
    expect(render().failure).toBe('parseFailed')
    expect(worker.terminate).toHaveBeenCalledExactlyOnceWith()
  })

  it('reports unavailable workers without running a large synchronous fallback parser', () => {
    vi.stubGlobal('Worker', undefined)
    const lexer = vi.spyOn(marked, 'lexer')
    render(); commitHooks()
    expect(render()).toMatchObject({ status: 'error', failure: 'workerUnavailable', page: { sourceOnly: true } })
    expect(lexer).not.toHaveBeenCalled()
  })

  it('accepts the exact shared byte ceiling and rejects overflow before creating a worker', () => {
    const boundary = 'x'.repeat(DOCUMENT_PREVIEW_MAX_BYTES)
    expect(render(boundary).status).toBe('loading'); commitHooks()
    expect(FakeWorker.instances).toHaveLength(1)
    const tooLarge = boundary + 'x'
    expect(render(tooLarge)).toMatchObject({ status: 'error', failure: 'tooLarge', page: null, count: 0 })
    commitHooks()
    expect(FakeWorker.instances).toHaveLength(1)
    expect(FakeWorker.instances[0]!.terminate).toHaveBeenCalledExactlyOnceWith()
  })
})
