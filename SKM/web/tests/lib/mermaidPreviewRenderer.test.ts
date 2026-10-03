import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderMermaidPreview } from '../../src/lib/mermaidPreview'
import { LIGHT_PREVIEW_THEME } from '../../src/lib/previewTheme'
import { documentPreviewHtml } from '../../src/lib/documentPreview'
import { deferred } from '../fixtures/hookHarness'

const engine = vi.hoisted(() => ({ initialize: vi.fn(), render: vi.fn() }))
vi.mock('mermaid', () => ({ default: engine }))
vi.mock('../../src/lib/documentPreview', () => ({ documentPreviewHtml: vi.fn((source: string) => source) }))

/** Mermaid 自体は mock し、本物の lifecycle と sanitizer 呼出境界を独立検証する。 */
function documentFixture() {
  const hosts: Array<{ remove: ReturnType<typeof vi.fn> }> = []
  vi.stubGlobal('document', {
    createElement: () => { const host = { remove: vi.fn(), setAttribute: vi.fn(), style: { cssText: '' } }; hosts.push(host); return host },
    body: { append: vi.fn() },
    implementation: { createHTMLDocument: () => ({ documentElement: { innerHTML: '' }, querySelector: () => ({
      getAttribute: () => '0 0 200 100', setAttribute: vi.fn(), outerHTML: '<svg id="sanitized"/>',
    }) }) },
  })
  return hosts
}
afterEach(() => { vi.clearAllMocks(); vi.unstubAllGlobals() })

describe('Mermaid renderer isolation lifecycle', () => {
  it('validates source before touching the DOM or loading the rendering engine', async () => {
    const hosts = documentFixture()
    await expect(renderMermaidPreview('flowchart LR\nclick A "https://example.test"', new AbortController().signal))
      .resolves.toEqual({ status: 'failed' })
    expect(engine.initialize).not.toHaveBeenCalled()
    expect(hosts).toHaveLength(0)
  })
  it('uses strict settings and canonical DSL, removing active DOM immediately when aborted', async () => {
    const hosts = documentFixture()
    const pending = deferred<{ svg: string }>(); engine.render.mockReturnValueOnce(pending.promise)
    const abort = new AbortController()
    const result = renderMermaidPreview('flowchart LR\nA[开始]-->B', abort.signal)
    await vi.waitFor(() => expect(engine.render).toHaveBeenCalled())
    expect(engine.initialize).toHaveBeenCalledWith(expect.objectContaining({ securityLevel: 'strict', htmlLabels: false,
      startOnLoad: false, suppressErrorRendering: true, flowchart: { htmlLabels: false, useMaxWidth: false } }))
    expect(engine.render.mock.calls[0]![1]).toContain('n0["开始"] --> n1')
    expect(hosts).toHaveLength(1)
    abort.abort()
    expect(hosts[0]!.remove).toHaveBeenCalled()
    pending.resolve({ svg: '<svg/>' })
    await expect(result).resolves.toEqual({ status: 'failed' })
    expect(documentPreviewHtml).not.toHaveBeenCalled()
  })
  it('passes output through the existing static sanitizer and returns only its SVG', async () => {
    const hosts = documentFixture()
    engine.render.mockResolvedValueOnce({ svg: '<svg id="engine"/>' })
    const result = await renderMermaidPreview('flowchart LR\nA-->B', new AbortController().signal)
    expect(documentPreviewHtml).toHaveBeenCalledWith(expect.stringContaining('<svg id="engine"/>'))
    expect(documentPreviewHtml).toHaveBeenCalledWith(expect.stringContaining('color-scheme:light'))
    expect(documentPreviewHtml).toHaveBeenCalledWith(expect.stringContaining('max-width:100%;height:auto;background:transparent'))
    expect(result).toMatchObject({ status: 'ready', svg: '<svg id="sanitized"/>', height: 100 })
    expect(hosts[0]!.remove).toHaveBeenCalled()
  })
  it('uses dark colors for nodes, labels and edges without accepting source configuration', async () => {
    documentFixture()
    engine.render.mockResolvedValueOnce({ svg: '<svg/>' })
    const theme = { ...LIGHT_PREVIEW_THEME, mode: 'dark' as const, background: '#28251f', foreground: '#e4dfd3' }
    await renderMermaidPreview('flowchart LR\nA-->B', new AbortController().signal, theme)
    expect(engine.initialize).toHaveBeenCalledWith(expect.objectContaining({ theme: 'base',
      themeVariables: expect.objectContaining({ darkMode: true, primaryTextColor: theme.foreground, lineColor: theme.foreground }),
      secure: expect.arrayContaining(['theme', 'themeVariables', 'securityLevel']),
    }))
    expect(documentPreviewHtml).toHaveBeenCalledWith(expect.stringContaining('color-scheme:dark'))
    expect(documentPreviewHtml).toHaveBeenCalledWith(expect.stringContaining('background:#28251f'))
  })
  it('fails closed and removes temporary DOM if SVG sanitization throws', async () => {
    const hosts = documentFixture()
    engine.render.mockResolvedValueOnce({ svg: '<svg/>' })
    vi.mocked(documentPreviewHtml).mockImplementationOnce(() => { throw new Error('sanitizer failed') })
    await expect(renderMermaidPreview('flowchart LR\nA-->B', new AbortController().signal))
      .resolves.toEqual({ status: 'failed' })
    expect(hosts[0]!.remove).toHaveBeenCalled()
  })
})
