import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RunArtifacts } from '../../src/components/RunArtifacts'
import type { RunArtifactRecord, RunResultDetail } from '../../src/api'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES, type UiLanguage } from '../../src/lib/i18n/messages'

const state = vi.hoisted(() => ({ data: [] as RunArtifactRecord[], pending: false, failure: null as { key: string } | null }))
vi.mock('../../src/hooks/useResourceRequest', async (original) => ({
  ...await original<Record<string, unknown>>(),
  useResourceQuery: () => ({ ...state, refresh: vi.fn(), completed: 0, revision: 0 }),
}))

const PROJECT = '00000000-0000-4000-8000-000000000020'
const RUN = '00000000-0000-4000-8000-000000000030'

/** 元の結果と公開索引の対応だけを検証する、過去の無検証範囲 record。 */
function result(): RunResultDetail {
  return { result_id: '00000000-0000-4000-8000-000000000040', output_schema: 'projectmind.outcome-envelope/v1',
    result_kind: 'OUTCOME_ENVELOPE', data: {}, evidence_refs: [], artifact_refs: ['art_original'],
    change_proposal_refs: [], optional_schema_identity: {}, summary: 'Original', confidence: null,
    needs_review: true, usage: {}, cost: {}, validation: {}, created_at: '2026-09-10T00:00:00Z' }
}

/** HTTP validator が確認済みの十 field metadata を表示用に渡す。 */
function artifact(reference: string): RunArtifactRecord {
  return { artifact_ref: reference, project_id: PROJECT, run_id: RUN, tool_call_id: PROJECT,
    evidence_ref: 'ev_fixture', path: 'output/原文.txt', size_bytes: 10, mime_type: 'text/plain',
    checksum: `sha256:${'a'.repeat(64)}`, created_at: '2026-09-10T00:00:00Z' }
}

/** Query の表示状態のみを制御し、実 request/timer/所有権は App browser で検証する。 */
function render(language: UiLanguage = 'zh', value: RunResultDetail | null = result()): string {
  return renderToStaticMarkup(<LanguageProvider language={language}>
    <RunArtifacts projectId={PROJECT} runId={RUN} result={value} onSessionExpired={vi.fn()} />
  </LanguageProvider>)
}

afterEach(() => { state.data = []; state.pending = false; state.failure = null })

describe('published Artifact view', () => {
  it.each(['zh', 'ja', 'en'] as const)('distinguishes published references from unadopted artifacts in %s', (language) => {
    state.data = [artifact('art_original'), artifact('art_other')]
    const labels = MESSAGES[language].runResult.artifacts
    const html = render(language)
    for (const label of [labels.title, labels.hint, labels.referenced, labels.unreferenced, labels.download]) expect(html).toContain(label)
    expect(html).not.toContain('href=')
    expect(html).not.toContain(MESSAGES[language].runResult.referenceChecks.artifactsVerified)
  })

  it('does not turn unknown historical references, model URLs, or paths into download links', () => {
    const value = result()
    value.artifact_refs = ['art_unknown', '<img src="https://artifact.invalid/private">']
    const html = render('zh', value)
    expect(html).toContain(MESSAGES.zh.runResult.artifacts.unavailableRefs)
    expect(html).toContain('art_unknown')
    expect(html).toContain('&lt;img')
    expect(html).not.toContain('<img')
    expect(html).not.toContain('href=')
    expect(html).not.toContain(MESSAGES.zh.runResult.artifacts.download)
  })

  it.each(['pending', 'failure'])('hides previous index records while %s rather than leaving active downloads', (phase) => {
    state.data = [artifact('art_original')]
    state.pending = phase === 'pending'
    state.failure = phase === 'failure' ? { key: 'denied' } : null
    const html = render()
    expect(html).not.toContain('output/原文.txt')
    expect(html).not.toContain(MESSAGES.zh.runResult.artifacts.download)
    expect(html).toContain(phase === 'pending' ? MESSAGES.zh.runResult.artifacts.loading : MESSAGES.zh.runResult.artifacts.failures.denied)
  })

  it('shows real publications without inventing a final Result that adopted them', () => {
    state.data = [artifact('art_original')]
    expect(render('zh', null)).toContain(MESSAGES.zh.runResult.artifacts.unreferenced)
  })
})
