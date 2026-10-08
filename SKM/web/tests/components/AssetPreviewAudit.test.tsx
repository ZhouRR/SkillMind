// @vitest-environment jsdom
import { act } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { DocumentUploadStatus } from '../../src/components/DocumentUploadStatus'
import type { DocumentUploadItem } from '../../src/lib/documentUpload'
import assetsCss from '../../src/styles/assets-audit.css?raw'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SourcePreview } from '../../src/components/SourcePreview'
import { LanguageProvider } from '../../src/i18n'
import { documentPreviewHtml, documentMarkdownHtml } from '../../src/lib/documentPreview'
let container: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals() })

/** 読書位置と live 状態は DOM で確認し、実 screen reader の発話までは模倣しない。 */
describe('asset preview navigation', () => {
  it('resets raw reading scroll for page changes without taking focus from the navigation', async () => {
    await act(async () => root.render(<LanguageProvider language="en"><SourcePreview source={'a'.repeat(32_000) + 'tail'} /></LanguageProvider>))
    const pre = container.querySelector('pre')!
    expect(pre.tabIndex).toBe(0)
    expect(pre.getAttribute('role')).toBe('region')
    pre.scrollTop = 700; pre.scrollLeft = 10
    const next = [...container.querySelectorAll('button')].find((button) => button.textContent === 'Next')!
    await act(async () => { next.focus(); next.click() })
    expect(pre.scrollTop).toBe(0); expect(pre.scrollLeft).toBe(0)
    expect(document.activeElement).toBe(next)
    expect(container.querySelector('[role="status"]')!.textContent).toContain('2')
    expect(pre.textContent).toContain('tail')
  })

  it('preserves explicit source language and keeps unknown document language undetermined', () => {
    const japanese = documentPreviewHtml('<!doctype html><html lang="ja"><head></head><body><script>bad()</script><p>日本語</p></body></html>')
    expect(japanese).toContain('<html lang="ja">')
    expect(japanese).not.toContain('<script>')
    expect(japanese).toContain("default-src 'none'")
    expect(documentPreviewHtml('<p>Unknown document</p>')).toContain('<html lang="und">')
    expect(documentMarkdownHtml('# Mixed source')).toContain('<html lang="und">')
    expect(documentPreviewHtml('<html lang="bad&value"><body>Text</body></html>')).toContain('<html lang="und">')
  })
})

/** 完了の大量明細だけを畳み、未知・拒否と元 key の照会経路は失わない。 */
describe('upload progress hierarchy', () => {
  it('keeps unconfirmed and refused uploads visible while folding bounded completed detail', () => {
    const item = (index: number, phase: DocumentUploadItem['phase']): DocumentUploadItem => ({
      original: { actorId: 'actor', projectId: 'project', uploadKey: `key-${index}`, label: `file-${index}.txt`, body: null }, phase, document: null, failure: phase === 'refused' ? { key: 'uploadTooLarge' } : null,
    })
    const upload: Parameters<typeof DocumentUploadStatus>[0]['upload'] = {
      batch: { items: [...Array.from({ length: 100 }, (_, index) => item(index, 'published')), item(100, 'unknown'), item(101, 'refused')], paused: true },
      recovery: null, notice: null, checkFailure: null, checking: false, closing: false, readDenied: false, pendingReceipt: false,
      cancelUpload: vi.fn(), checkOriginal: vi.fn(), cancelCheck: vi.fn(), canContinue: () => false, continueBatch: vi.fn(), closeRecovery: vi.fn(),
    }
    container.innerHTML = renderToStaticMarkup(<LanguageProvider language="en"><DocumentUploadStatus upload={upload} canRead /></LanguageProvider>)
    const completed = container.querySelector('details.completedDocumentUploads')!
    expect(completed.hasAttribute('open')).toBe(false)
    expect(completed.querySelectorAll('li')).toHaveLength(100)
    const visible = container.querySelector('.documentUploadStatus > ul')!
    expect(visible.textContent).toContain('file-100.txt')
    expect(visible.textContent).toContain('file-101.txt')
    expect(visible.querySelectorAll('li')).toHaveLength(2)
    expect(container.querySelector('progress')!.getAttribute('value')).toBe('101')
    expect(container.querySelector('progress')!.getAttribute('max')).toBe('102')
    expect(container.querySelector('input[value="key-100"]')).not.toBeNull()
    expect(assetsCss).toMatch(/\.documentUploadCompletedItems[^}]*max-height:[^}]*overflow: auto/s)
  })
})
