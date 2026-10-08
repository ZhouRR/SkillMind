// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../../src/api'
import { DocumentManagerPanel } from '../../src/components/DocumentManagerPanel'
import { RunDeletionDialog } from '../../src/components/RunDeletionDialog'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES, type UiLanguage } from '../../src/lib/i18n/messages'
import { interactionRun } from '../fixtures/interaction'
import baseStyles from '../../src/styles/base.css?raw'
import workspaceStyles from '../../src/styles/workspace-audit.css?raw'
import confirmationStyles from '../../src/styles/delete-confirmation.css?raw'

vi.mock('../../src/api', async (load) => ({ ...await load<typeof import('../../src/api')>(),
  loadProjectDocuments: vi.fn(), loadDocumentFolders: vi.fn(), manageDocuments: vi.fn(), purgeProjectDocument: vi.fn(),
  previewRunDeletion: vi.fn(), changeRunDeletion: vi.fn() }))

let container: HTMLDivElement
let root: Root
let stylesheet: HTMLStyleElement
const messages = MESSAGES.en
const m = messages.fileManagement

/** 遅延・結果未知の応答を制御する。外部 API は一切呼ばない。 */
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

/** 実資源と無関係な長い名称を持つ、原実行の fixture。 */
function run(): api.RunHistoryItemRecord {
  return { ...interactionRun(), status: 'SUCCEEDED', task_title: 'Original frozen task / 原任务 / 元タスク'.repeat(12),
    input: {}, selected_sources: {}, started_at: null, finished_at: null, result_summary: null, result_confidence: null, result_needs_review: null }
}

/** 全公開成果を保持し、同名の別目录と参照保護を区別する。 */
function preview(count = 12): api.RunDeletionPreview {
  return { run_id: run().run_id, output_count: count, protected_output_count: count ? 1 : 0, cleanup_pending: 0, deleted: false,
    outputs: Array.from({ length: count }, (_, index) => ({ document_id: `00000000-0000-4000-8000-${String(index + 100).padStart(12, '0')}`,
      folder: `reports/${index}`, name: 'unbroken-long-file-name-'.repeat(12) + '.md', protected: index === 0 })) }
}

/** 原 path と ID の確認を検証するための架空文書。 */
function documents(): api.ProjectDocumentRecord[] {
  return ['first', 'second'].map((folder, index) => ({ document_id: `00000000-0000-4000-8000-${String(index + 200).padStart(12, '0')}`,
    project_id: run().project_id, folder, name: 'same-long-filename-'.repeat(12) + '.md', size: 32, mime: 'text/markdown',
    checksum: `sha256:${'a'.repeat(64)}`, uploaded_by: '00000000-0000-4000-8000-000000000001', created_at: '2026-01-01T00:00:00Z' }))
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Unexpected network request') }))
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
  Element.prototype.scrollIntoView = vi.fn()
  vi.clearAllMocks()
  vi.mocked(api.loadProjectDocuments).mockResolvedValue(documents())
  vi.mocked(api.loadDocumentFolders).mockResolvedValue(['first', 'second'])
  vi.mocked(api.previewRunDeletion).mockResolvedValue(preview())
  stylesheet = document.createElement('style')
  stylesheet.textContent = [confirmationStyles, baseStyles, workspaceStyles].join('\n')
  document.head.append(stylesheet)
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(async () => {
  await act(async () => root.unmount())
  container.remove(); stylesheet.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals()
})

/** Localized label と範囲から、操作対象の実 button を取得する。 */
function button(label: string, scope: ParentNode = container): HTMLButtonElement {
  const found = [...scope.querySelectorAll<HTMLButtonElement>('button')].find((node) => node.textContent === label)
  if (!found) throw new Error(`Missing button: ${label}`)
  return found
}

/** Dialog の原 owner を固定して描画する。 */
async function renderRun(action: 'TRASH' | 'RESTORE' | 'PURGE', language: UiLanguage = 'en', onClose = vi.fn(), onChanged = vi.fn(), key = 'original') {
  await act(async () => root.render(<LanguageProvider language={language}><RunDeletionDialog key={key} projectId={run().project_id}
    runId={run().run_id} run={run()} csrfToken="synthetic" action={action} onClose={onClose} onChanged={onChanged} /></LanguageProvider>))
}

/** 実 Panel の選択と確認だけを描画し、mutation を mock に閉じ込める。 */
async function renderDocuments(readOnly = false) {
  await act(async () => root.render(<LanguageProvider language="en"><DocumentManagerPanel projectId={run().project_id}
    actorId="fixture-actor" csrfToken="synthetic" readOnly={readOnly} onSessionEnded={vi.fn()} /></LanguageProvider>))
}

describe('deletion confirmation layout and context', () => {
  it.each(['zh', 'ja', 'en'] as const)('keeps the original run, full consequences and all output paths in %s', async (language) => {
    await renderRun('PURGE', language)
    const local = MESSAGES[language].fileManagement
    expect(container.querySelector('.modalCompact')).not.toBeNull()
    expect(container.querySelector('.runDeletionTarget strong')!.textContent).toBe(run().task_title)
    expect(container.querySelector('.runDeletionTarget code')!.textContent).toBe(run().run_id)
    expect(container.querySelector('.confirmMessage')!.textContent).toBe(local.purgeConfirm)
    expect(container.querySelector('.runDeletionOutputs')!.hasAttribute('open')).toBe(false)
    expect(container.querySelectorAll('.runDeletionOutputs li')).toHaveLength(12)
    expect(container.querySelector('.runDeletionOutputs ul')!.getAttribute('tabindex')).toBe('0')
    expect(container.querySelector('.runDeletionChoice')!.textContent).toBe(local.purgeOutputs)
    expect([...container.querySelectorAll('.confirmActions button')].map((node) => node.textContent)).toEqual([local.cancel, local.purge])
    expect(api.changeRunDeletion).not.toHaveBeenCalled()
  })

  it('uses scoped compact dimensions and wrapping without narrowing ordinary modals', async () => {
    await renderRun('PURGE')
    const dialog = container.querySelector('.modalDialog')!
    const body = container.querySelector('.modalBody')!
    expect(getComputedStyle(dialog).width).toBe('min(460px, 100%)')
    expect(getComputedStyle(body).paddingLeft).toBe(getComputedStyle(container.querySelector('.modalHeader')!).paddingLeft)
    expect(getComputedStyle(body).scrollbarGutter).toBe('auto')
    expect(getComputedStyle(container.querySelector('.deleteConfirmationContent')!).overflowWrap).toBe('anywhere')
    expect(getComputedStyle(container.querySelector('.confirmActions')!).flexWrap).toBe('wrap')
    for (const action of container.querySelectorAll('.confirmActions button')) {
      expect(getComputedStyle(action).width).toBe('auto')
      expect(getComputedStyle(action).maxWidth).toBe('100%')
      expect(getComputedStyle(action).whiteSpace).toBe('normal')
    }
    const plain = document.createElement('div'); plain.className = 'modalDialog'; container.append(plain)
    expect(getComputedStyle(plain).width).toBe('min(560px, 100%)')
    expect(confirmationStyles).toMatch(/@media\s*\(pointer:\s*coarse\)\s*\{\s*\.deleteConfirmationContent \.confirmActions > button\s*\{[^}]*min-height:\s*44px/s)
  })

  it('shows full document paths and original IDs separately from consequences, and cancels cleanly before reopening', async () => {
    await renderDocuments()
    await act(async () => container.querySelector<HTMLInputElement>('.documentSelectionToolbar input')!.click())
    const trigger = button(m.trashAction, container.querySelector('.documentSelectionToolbar')!)
    await act(async () => { trigger.focus(); trigger.click() })
    const dialog = container.querySelector('[role="dialog"]')!
    expect(dialog.querySelector('.confirmMessage')!.textContent).toBe(m.trashDocuments)
    expect([...dialog.querySelectorAll('.confirmTargetList strong')].map((node) => node.textContent)).toEqual(documents().map((d) => `${d.folder}/${d.name}`))
    expect([...dialog.querySelectorAll('.confirmTargetList code')].map((node) => node.textContent)).toEqual(documents().map((d) => d.document_id))
    expect(dialog.querySelector('.confirmTargetList')!.getAttribute('aria-label')).toBe(messages.documentsPanel.selectedCount(2))
    expect(getComputedStyle(dialog.querySelector('.confirmTargetList')!).overflow).toBe('auto')
    await act(async () => button(m.cancel, dialog).click())
    expect(container.querySelector('.modalOverlay')!.hasAttribute('hidden')).toBe(true)
    expect(document.activeElement).toBe(trigger)
    expect(api.manageDocuments).not.toHaveBeenCalled()
    await act(async () => trigger.click())
    expect(container.querySelector('.modalOverlay')!.hasAttribute('hidden')).toBe(false)
    expect(dialog.querySelectorAll('.confirmTargetList li')).toHaveLength(2)
    expect(api.manageDocuments).not.toHaveBeenCalled()
  })

  it('keeps document purge irreversible and a rejected first item stops a batch without replay', async () => {
    await renderDocuments()
    await act(async () => button(m.trash).click())
    await act(async () => container.querySelector<HTMLInputElement>('.documentSelectionToolbar input')!.click())
    await act(async () => button(m.purge, container.querySelector('.documentSelectionToolbar')!).click())
    const dialog = container.querySelector('[role="dialog"]')!
    expect(dialog.querySelector('.confirmMessage')!.textContent).toBe(m.purgeDocuments)
    expect(button(m.purge, dialog).className).toBe('destructiveButton')
    vi.mocked(api.purgeProjectDocument).mockRejectedValueOnce(new Error('Unconfirmed transport'))
    await act(async () => { button(m.purge, dialog).click(); button(m.purge, dialog).click() })
    expect(api.purgeProjectDocument).toHaveBeenCalledTimes(1)
    expect(api.purgeProjectDocument).toHaveBeenCalledWith(run().project_id, documents()[0]!.document_id, 'synthetic', expect.any(AbortSignal))
    expect(container.querySelector('[role="alert"]')!.textContent).toBe(m.unknown)
    expect(api.manageDocuments).not.toHaveBeenCalled()
  })

  it('retains read-only document controls and never sends a write', async () => {
    await renderDocuments(true)
    expect(container.querySelector<HTMLInputElement>('.documentSelectionToolbar input')!.disabled).toBe(true)
    expect(container.textContent).toContain(messages.documentsPanel.failures.archived)
    expect(api.manageDocuments).not.toHaveBeenCalled()
    expect(api.purgeProjectDocument).not.toHaveBeenCalled()
  })
})

describe('run deletion request boundaries', () => {
  it('keeps trash reversible and states the matching-output restore boundary', async () => {
    await renderRun('TRASH')
    expect(container.querySelector('.confirmMessage')!.textContent).toBe(`${m.runConfirm} ${m.recycleHint}`)
    await renderRun('RESTORE', 'en', vi.fn(), vi.fn(), 'restore')
    expect(container.querySelector('.confirmMessage')!.textContent).toBe(m.restoreRun)
    expect(container.querySelector('.runDeletionChoice')).toBeNull()
    expect(button(m.restore).className).toBe('primaryButton')
    expect(api.changeRunDeletion).not.toHaveBeenCalled()
  })

  it('disables confirmation before preview and after a failed preview', async () => {
    const read = deferred<api.RunDeletionPreview>()
    vi.mocked(api.previewRunDeletion).mockReturnValueOnce(read.promise)
    await renderRun('TRASH')
    expect(button(m.trashAction).disabled).toBe(true)
    expect(container.querySelector('[role="status"]')!.textContent).toBe(messages.uiAuditWorkspace.deletionLoading)
    await act(async () => read.reject(new Error('Read unavailable')))
    expect(button(m.trashAction).disabled).toBe(true)
    expect(container.querySelector('[role="alert"]')!.textContent).toBe(m.failure)
    expect(api.changeRunDeletion).not.toHaveBeenCalled()
  })

  it('sends the original choice once, keeps cancellation blocked in flight, and retains an unknown result', async () => {
    const write = deferred<api.RunDeletionPreview>()
    vi.mocked(api.changeRunDeletion).mockReturnValueOnce(write.promise)
    const onClose = vi.fn(), onChanged = vi.fn()
    await renderRun('PURGE', 'en', onClose, onChanged)
    await act(async () => container.querySelector<HTMLInputElement>('.runDeletionChoice input')!.click())
    await act(async () => { button(m.purge).click(); button(m.purge).click() })
    expect(api.changeRunDeletion).toHaveBeenCalledExactlyOnceWith(run().project_id, run().run_id, 'PURGE', false, 'synthetic', expect.any(AbortSignal))
    expect(button(m.cancel).disabled).toBe(true)
    await act(async () => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    expect(onClose).not.toHaveBeenCalled()
    await act(async () => write.reject(new Error('Response lost')))
    expect(container.querySelector('[role="alert"]')!.textContent).toBe(`${m.unknown} ${m.runBlocked}`)
    expect(container.querySelector('.runDeletionTarget code')!.textContent).toBe(run().run_id)
    expect(button(m.purge).disabled).toBe(true)
    expect(button(m.cancel).disabled).toBe(false)
    await act(async () => button(m.purge).click())
    expect(api.changeRunDeletion).toHaveBeenCalledTimes(1)
    expect(onChanged).not.toHaveBeenCalled()
  })

  it('ignores a dismissed preview and reopens with a fresh original-target read', async () => {
    const oldRead = deferred<api.RunDeletionPreview>()
    vi.mocked(api.previewRunDeletion).mockReturnValueOnce(oldRead.promise)
    await renderRun('TRASH')
    await act(async () => root.render(null))
    expect(vi.mocked(api.previewRunDeletion).mock.calls[0]![2].aborted).toBe(true)
    await renderRun('TRASH', 'en', vi.fn(), vi.fn(), 'reopened')
    await act(async () => oldRead.resolve(preview(1)))
    expect(container.querySelectorAll('.runDeletionOutputs li')).toHaveLength(12)
    expect(api.previewRunDeletion).toHaveBeenCalledTimes(2)
    expect(api.changeRunDeletion).not.toHaveBeenCalled()
  })

  it('retains cleanup warnings instead of reporting complete success', async () => {
    const onChanged = vi.fn()
    vi.mocked(api.changeRunDeletion).mockResolvedValueOnce({ ...preview(), deleted: true, cleanup_pending: 2 })
    await renderRun('PURGE', 'en', vi.fn(), onChanged)
    await act(async () => button(m.purge).click())
    expect(container.querySelector('[role="status"]')!.textContent).toBe(m.cleanupPending)
    expect(button(m.purge).disabled).toBe(true)
    expect(onChanged).not.toHaveBeenCalled()
  })
})
