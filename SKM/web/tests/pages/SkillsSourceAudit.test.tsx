// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../../src/api'
import type { SkillParseResult, SkillVersionRecord, StoredSkillPreviewRecord } from '../../src/api'
import { SkillsPage } from '../../src/pages/SkillsPage'
import { LanguageProvider } from '../../src/i18n'
import { loadInterpretationReceipt } from '../../src/lib/interpretationReceipt'
import { deferred } from '../fixtures/hookHarness'

vi.mock('../../src/api', async (original) => ({ ...await original<typeof import('../../src/api')>(),
  parseSkillSource: vi.fn(), saveSkillImport: vi.fn(), listSkillVersions: vi.fn(), listProjectSkillVersions: vi.fn(), enableProjectSkillVersion: vi.fn(), uploadSkillFiles: vi.fn(), interpretSkillSource: vi.fn(),
}))
vi.mock('../../src/lib/skillUpload', async (original) => ({ ...await original<typeof import('../../src/lib/skillUpload')>(),
  readUploadedSourcePreview: vi.fn(async () => [{ path: 'SKILL.md', kind: 'text', content: '# Uploaded', size: 10 }]),
}))
const parsed: SkillParseResult = {
  normalized_package: { package_format: 'test', source: { type: 'directory', content_hash: 'source', detected_adapter: 'test', files: [] }, metadata: { name: 'Parsed original', description: '', argument_hint: null }, resources: { scripts: [], references: [], assets: [] }, declared_tools: [], diagnostics: [] },
  runtime_manifest_draft: { identity: { skill_key: 'original', source_hash: 'source', interpreter_version: 'test' }, compatibility: { level: 'native', confidence: 1, diagnostics: [] }, tools: [], extensions: {} }, capability_blueprint: null,
}
const stored: StoredSkillPreviewRecord = { skill_source_id: 'source-one', interpretation_id: 'interpretation-one', organization_id: 'organization', name: 'Saved original', source_hash: 'hash', source_type: 'directory', interpretation_status: 'PREVIEW_READY', compatibility_level: 'native', confidence: 1, interpreter_version: 'test', created_at: '', preview: parsed }
const published: SkillVersionRecord = { skill_id: 'skill', skill_version_id: 'version-one', skill_source_id: 'source-one', interpretation_id: 'interpretation-one', organization_id: 'organization', skill_key: 'original', name: 'Published skill', description: '', version: '1', status: 'PUBLISHED', manifest_checksum: 'hash', manifest: {}, gate_passed: true, gate_findings: [], interpretation_diff: {}, created_at: '', published_by: null, published_at: null }
let container: HTMLDivElement
let root: Root

/** DOM の入力と遅延 transport を分け、実際の form handler の snapshot 所有権を検証する。 */
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
  Element.prototype.scrollIntoView = vi.fn()
  sessionStorage.clear()
  vi.mocked(api.listSkillVersions).mockResolvedValue([])
  vi.mocked(api.listProjectSkillVersions).mockResolvedValue([])
  vi.mocked(api.parseSkillSource).mockResolvedValue(parsed)
  vi.mocked(api.saveSkillImport).mockResolvedValue(stored)
  vi.mocked(api.uploadSkillFiles).mockResolvedValue(stored)
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.resetAllMocks(); vi.unstubAllGlobals() })
/** Project の props だけを替え、remount がなくても旧 scope の応答を捨てる。 */
async function render(projectId = 'project-a') { await act(async () => root.render(<LanguageProvider language="en"><SkillsPage projectId={projectId} csrfToken="csrf" /></LanguageProvider>)) }
/** ボタンの完全表示名で操作し、複数候補や欠落を隠さない。 */
function button(text: string): HTMLButtonElement {
  const matches = [...container.querySelectorAll('button')].filter((item) => item.textContent === text)
  expect(matches).toHaveLength(1); return matches[0]!
}
async function click(text: string) { await act(async () => button(text).click()) }
/** native setter は React の value tracker を迂回して実編集の input event を再現する。 */
async function type(value: string) {
  await act(async () => {
    const input = container.querySelector('textarea')!
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value }))
  })
}
async function parse() { await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))) }

describe('Skill source snapshot ownership', () => {
  it('saves exactly the parsed snapshot, locks submission fields, then invalidates every old source result after editing', async () => {
    await render(); await type('# Original'); await parse()
    expect(container.textContent).toContain('Parsed original')
    const pending = deferred<StoredSkillPreviewRecord>()
    vi.mocked(api.saveSkillImport).mockReturnValueOnce(pending.promise)
    await click('Save parse result')
    expect(api.saveSkillImport).toHaveBeenCalledWith([{ path: 'SKILL.md', content: '# Original' }], 'csrf', expect.any(AbortSignal))
    expect(container.querySelector('textarea')!.disabled).toBe(true)
    expect(container.querySelector<HTMLInputElement>('input[type="file"]')!.disabled).toBe(true)
    await act(async () => pending.resolve(stored))
    await type('# Changed')
    expect(container.textContent).toContain('The source has changed')
    expect(container.textContent).not.toContain('Parsed original')
    expect(container.querySelector('.savedSkill')).toBeNull()
    expect([...container.querySelectorAll('button')].some((item) => item.textContent === 'Save parse result')).toBe(false)
    await parse(); await click('Save parse result')
    expect(api.saveSkillImport).toHaveBeenLastCalledWith([{ path: 'SKILL.md', content: '# Changed' }], 'csrf', expect.any(AbortSignal))
  })

  it('drops a late parse response after editing even when transport ignores abort', async () => {
    const pending = deferred<SkillParseResult>(); vi.mocked(api.parseSkillSource).mockReturnValueOnce(pending.promise)
    await render(); await type('# Original'); await parse(); await type('# Newer')
    expect(vi.mocked(api.parseSkillSource).mock.calls[0]![2]!.aborted).toBe(true)
    await act(async () => pending.resolve(parsed))
    expect(container.textContent).not.toContain('Parsed original')
    expect(container.textContent).toContain('The source has changed')
    expect(container.querySelector('textarea')!.value).toBe('# Newer')
  })

  it('clears saved upload actions when switching back to manual source', async () => {
    await render()
    await act(async () => {
      const input = container.querySelector<HTMLInputElement>('input[type="file"]')!
      Object.defineProperty(input, 'files', { configurable: true, value: [new File(['# Uploaded'], 'SKILL.md')] })
      input.dispatchEvent(new Event('change', { bubbles: true }))
    })
    expect(container.querySelector('.savedSkill')).not.toBeNull()
    const clear = container.querySelector<HTMLButtonElement>('.sourcePreviewHeader button')!
    await act(async () => clear.click())
    expect(container.querySelector('.savedSkill')).toBeNull()
    expect(container.querySelector('.sourcePreview')).toBeNull()
    expect(container.textContent).toContain('The source has changed')
  })

  it('keeps an unknown interpretation receipt and recovery controls when the source changes', async () => {
    vi.mocked(api.interpretSkillSource).mockRejectedValueOnce(new TypeError('Response unavailable'))
    await render(); await type('# Original'); await parse(); await click('Save parse result'); await click('Interpret with model')
    const request = loadInterpretationReceipt()
    expect(request).not.toBeNull()
    await type('# New source')
    expect(loadInterpretationReceipt()).toBe(request)
    expect(button('Check original request').disabled).toBe(false)
    expect(container.textContent).toContain('This interpretation belongs to an earlier source')
    expect(container.querySelector('.savedSkill')).toBeNull()
  })

  it('does not let an old project mutation cancel the new project enablement read', async () => {
    vi.mocked(api.listSkillVersions).mockResolvedValue([published])
    const mutation = deferred<api.ProjectSkillVersionRecord>()
    vi.mocked(api.enableProjectSkillVersion).mockReturnValue(mutation.promise)
    await render(); await click('Enable for project')
    const projectB = deferred<api.ProjectSkillVersionRecord[]>()
    vi.mocked(api.listProjectSkillVersions).mockReturnValueOnce(projectB.promise)
    await render('project-b')
    const readSignal = vi.mocked(api.listProjectSkillVersions).mock.calls[1]![2]!
    await act(async () => mutation.resolve({ project_id: 'project-a', organization_id: 'organization', skill_version: published, enabled_by: 'actor', enabled_at: '', disabled_at: null }))
    expect(readSignal.aborted).toBe(false)
    expect(api.listProjectSkillVersions).toHaveBeenCalledTimes(2)
    await act(async () => projectB.resolve([]))
    expect(button('Enable for project').disabled).toBe(false)
  })
})
