import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import type { ProjectDocumentRecord } from '../../src/api'
import {
  DocumentPreviewDialog,
  DocumentTree,
  buildDocumentTree,
  countDocuments,
  documentPreviewKind,
} from '../../src/components/DocumentManagerPanel'

const PROJECT_ID = '00000000-0000-4000-8000-000000000020'

/** テスト用の文書 record を生成する。 */
function document(overrides: Partial<ProjectDocumentRecord> = {}): ProjectDocumentRecord {
  return {
    document_id: '00000000-0000-4000-8000-000000000090',
    project_id: PROJECT_ID,
    folder: '',
    name: 'note.txt',
    size: 512,
    mime: 'text/plain',
    checksum: `sha256:${'a'.repeat(64)}`,
    uploaded_by: '00000000-0000-4000-8000-000000000030',
    created_at: '2026-07-10T00:00:00Z',
    ...overrides,
  }
}

/** DocumentTree を静的 markup へ描画する。 */
function renderTree(documents: ProjectDocumentRecord[], busyId: string | null = null): string {
  return renderToStaticMarkup(
    <DocumentTree
      root={buildDocumentTree(documents)}
      projectId={PROJECT_ID}
      busyId={busyId}
      onDelete={vi.fn()}
      onPreview={vi.fn()}
    />,
  )
}

describe('buildDocumentTree', () => {
  it('nests folder paths into real hierarchy with stable ordering', () => {
    const root = buildDocumentTree([
      document({ document_id: 'a', folder: 'specs/api' }),
      document({ document_id: 'b', folder: '' , name: 'zzz.md' }),
      document({ document_id: 'c', folder: 'assets' }),
      document({ document_id: 'd', folder: 'specs' }),
    ])

    // Root 直下は folder 昇順、root file はその後に保持される。
    expect(root.folders.map((folder) => folder.name)).toEqual(['assets', 'specs'])
    expect(root.files.map((file) => file.document_id)).toEqual(['b'])
    const specs = root.folders[1]
    expect(specs?.path).toBe('specs')
    expect(specs?.folders.map((folder) => folder.path)).toEqual(['specs/api'])
    expect(specs?.files.map((file) => file.document_id)).toEqual(['d'])
    // 子孫込みの件数を数える。
    expect(countDocuments(root)).toBe(4)
    expect(specs && countDocuments(specs)).toBe(2)
  })
})

describe('documentPreviewKind', () => {
  it('maps registered extensions and rejects everything else', () => {
    expect(documentPreviewKind('readme.md')).toBe('markdown')
    expect(documentPreviewKind('notes.TXT')).toBe('text')
    expect(documentPreviewKind('report.html')).toBe('html')
    expect(documentPreviewKind('archive.zip')).toBeNull()
    expect(documentPreviewKind('image.png')).toBeNull()
  })
})

describe('DocumentTree display', () => {
  it('renders nested folders as collapsible nodes with file rows and actions', () => {
    const html = renderTree([
      document({ document_id: 'doc-root', name: 'readme.md', folder: '', size: 2048, mime: 'text/markdown' }),
      document({ document_id: 'doc-spec', name: 'overview.md', folder: 'specs/api', size: 5 * 1024 * 1024 }),
    ])

    expect(html).toContain('docFolder')
    expect(html).toContain('specs')
    expect(html).toContain('api')
    expect(html).toContain('readme.md')
    expect(html).toContain('overview.md')
    expect(html).toContain('2.0 KB')
    expect(html).toContain('5.0 MB')
    expect(html).toContain('/documents/doc-root/content')
    expect(html).toContain('download="readme.md"')
    expect(html).toContain('下载')
    expect(html).toContain('删除')
  })

  it('offers preview only for registered extensions and disables it beyond the size cap', () => {
    const html = renderTree([
      document({ document_id: 'doc-md', name: 'small.md', size: 100 }),
      document({ document_id: 'doc-zip', name: 'assets.zip', size: 100, mime: 'application/zip' }),
    ])
    // md には预览、zip には出ない(1 件だけ)。
    expect(html.split('预览').length - 1).toBe(1)

    const oversized = renderTree([
      document({ document_id: 'doc-big', name: 'big.md', size: 2_000_000 }),
    ])
    // 上限超過は disabled + 誘導 title で残す。
    expect(oversized).toContain('预览')
    expect(oversized).toContain('disabled')
    expect(oversized).toContain('文件超过 1MB')
  })

  it('marks the document under deletion as busy and disables its delete button', () => {
    const html = renderTree([document({ document_id: 'doc-busy', name: 'note.txt' })], 'doc-busy')

    expect(html).toContain('删除中…')
    expect(html).toContain('disabled')
  })
})

describe('DocumentPreviewDialog', () => {
  it('renders text content inside an accessible dialog with download and close', () => {
    const html = renderToStaticMarkup(
      <DocumentPreviewDialog
        preview={{
          status: 'ready',
          document: document({ document_id: 'doc-md', name: 'guide.md', mime: 'text/markdown' }),
          kind: 'text',
          content: '# 标题\n\n正文内容。',
        }}
        projectId={PROJECT_ID}
        onClose={vi.fn()}
      />,
    )

    expect(html).toContain('role="dialog"')
    expect(html).toContain('aria-modal="true"')
    expect(html).toContain('modalViewport')
    expect(html).toContain('guide.md')
    expect(html).toContain('正文内容。')
    expect(html).toContain('/documents/doc-md/content')
    expect(html).toContain('关闭')
  })

  it('renders html content in a fully sandboxed frame instead of inline markup', () => {
    const html = renderToStaticMarkup(
      <DocumentPreviewDialog
        preview={{
          status: 'ready',
          document: document({ document_id: 'doc-html', name: 'report.html', mime: 'text/html' }),
          kind: 'html',
          content: '<h1>Report</h1><script>alert(1)</script>',
        }}
        projectId={PROJECT_ID}
        onClose={vi.fn()}
      />,
    )

    // script を無効化する空 sandbox の iframe に閉じ込め、直接 DOM へは埋め込まない。
    expect(html).toContain('sandbox=""')
    expect(html).toContain('previewFrame')
    expect(html).not.toContain('<h1>Report</h1>')
  })
})
