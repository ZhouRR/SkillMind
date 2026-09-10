import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { ResultReferenceChecks, RunResultDetail } from '../../src/api'
import { ResultValidationScope } from '../../src/components/ResultValidationScope'
import { RunResultPanel } from '../../src/components/RunResultPanel'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES, type UiLanguage } from '../../src/lib/i18n/messages'
import { loadRunDetail } from '../../src/api'
import fixture from '../../../contracts/examples/run-detail.v1.json'

const CHECKS: ResultReferenceChecks = {
  version: 'projectmind.result-reference-checks/v1', evidence: 'RUN_OWNERSHIP',
  proposals: 'RUN_OWNERSHIP_AND_STATE', effects: 'PLATFORM_RECORD_MATCH', artifacts: 'NOT_VERIFIED',
}

/** 表示範囲だけを検査する元 record。モデル本文や現在の Effect から検証済みを推論させない。 */
function result(checks?: ResultReferenceChecks, kind: RunResultDetail['result_kind'] = 'OUTCOME_ENVELOPE'): RunResultDetail {
  return { result_id: '00000000-0000-4000-8000-000000000040', output_schema: 'projectmind.outcome-envelope/v1',
    result_kind: kind, data: { summary: 'Original model content' }, evidence_refs: [], artifact_refs: [],
    change_proposal_refs: [], optional_schema_identity: {}, summary: 'Original model content', confidence: 0.8,
    needs_review: true, usage: {}, cost: {}, validation: { schema_valid: true, ...(checks ? {
      reference_checks: checks, evidence_refs_valid: true, change_proposal_refs_valid: true,
      ...(kind === 'OUTCOME_ENVELOPE' ? { outcome_envelope_valid: true } : {}),
    } : {}) },
    created_at: '2026-09-10T00:00:00Z' }
}

/** 三語 catalog と実 scope component を同じ Provider で描画する。 */
function scope(value: RunResultDetail, language: UiLanguage): string {
  return renderToStaticMarkup(<LanguageProvider language={language}><ResultValidationScope result={value} /></LanguageProvider>)
}

describe('Result validation scope display', () => {
  it.each(['zh', 'ja', 'en'] as const)('distinguishes v2 save-time artifact checks without changing v1 in %s', (language) => {
    const value = result({ ...CHECKS, version: 'projectmind.result-reference-checks/v2', artifacts: 'RUN_OWNERSHIP_AND_CONTENT' })
    value.validation.artifact_refs_valid = true
    const labels = MESSAGES[language].runResult.referenceChecks
    expect(scope(value, language)).toContain(labels.artifactsVerified)
    expect(scope(value, language)).not.toContain(labels.artifacts)
    expect(scope(result(CHECKS), language)).toContain(labels.artifacts)
    value.validation.artifact_refs_valid = false
    expect(scope(value, language)).toContain(labels.invalid)
  })

  it.each(['zh', 'ja', 'en'] as const)('shows saved checks and explicit limits in %s', (language) => {
    const labels = MESSAGES[language].runResult.referenceChecks
    const html = scope(result(CHECKS), language)
    for (const text of [labels.title, labels.recorded, labels.references, labels.effects, labels.artifacts, labels.limit]) {
      expect(html).toContain(text)
    }
    expect(html).not.toContain(labels.legacy)
    expect(html).not.toContain('href=')
  })

  it.each(['zh', 'ja', 'en'] as const)('does not manufacture checks for an old result in %s', (language) => {
    const value = result()
    const before = structuredClone(value)
    const labels = MESSAGES[language].runResult.referenceChecks
    const html = scope(value, language)
    expect(html).toContain(labels.legacy)
    expect(html).toContain(labels.limit)
    expect(html).not.toContain(labels.recorded)
    expect(html).not.toContain(labels.effects)
    expect(value).toEqual(before)
  })

  it('does not attribute model-effect checks to the structured format', () => {
    const html = scope(result({ ...CHECKS, effects: 'NOT_APPLICABLE' }, 'STRUCTURED_OUTPUT'), 'zh')
    expect(html).toContain(MESSAGES.zh.runResult.referenceChecks.effectsNotApplicable)
    expect(html).not.toContain(MESSAGES.zh.runResult.referenceChecks.effects)
  })

  it('keeps a malformed direct component input distinct from missing historical scope', () => {
    const value = result()
    Object.assign(value.validation, { reference_checks: { ...CHECKS, artifacts: 'VERIFIED' } })
    const html = scope(value, 'zh')
    expect(html).toContain(MESSAGES.zh.runResult.referenceChecks.invalid)
    expect(html).not.toContain(MESSAGES.zh.runResult.referenceChecks.recorded)
    expect(html).not.toContain(MESSAGES.zh.runResult.referenceChecks.legacy)
  })

  it('does not label contradictory saved flags as completed checks', () => {
    const value = result(CHECKS)
    value.validation.evidence_refs_valid = false
    const html = scope(value, 'zh')
    expect(html).toContain(MESSAGES.zh.runResult.referenceChecks.invalid)
    expect(html).not.toContain(MESSAGES.zh.runResult.referenceChecks.recorded)
  })

  it('keeps original model effect claims and saved platform records in different sections', async () => {
    const payload = { ...structuredClone(fixture), result: { ...result(), data: {
      outcome_version: 'projectmind.outcome-envelope/v1', status: 'PARTIAL',
      effects: [{ status: 'APPLIED', summary: 'Original model claim', proposal_ref: 'cp_original' }],
    } }, effect_executions: [{ effect_execution_id: 'effect-original', proposal_id: 'proposal-original',
      run_id: fixture.run_id, approval_id: 'approval-original', tool_call_id: null, status: 'APPLIED',
      provider: 'fixture-record', provider_version: 'v1', before_ref: null, after_ref: null,
      verification: {}, error: null, attempt_no: 1, executed_at: null,
      created_at: '2026-09-10T00:00:00Z', updated_at: '2026-09-10T00:00:00Z' }] }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))))
    try {
      const detail = await loadRunDetail(payload.project_id, payload.run_id)
      const html = renderToStaticMarkup(<RunResultPanel csrfToken={'s'.repeat(32)} state={{ status: 'ready', detail }} />)
      const messages = MESSAGES.zh.runResult
      for (const text of [messages.changesAndEffects, messages.modelEffectsHint, messages.platformEffectsHint,
        messages.referenceChecks.legacy, 'Original model claim', 'fixture-record']) expect(html).toContain(text)
      expect(html).not.toContain(messages.referenceChecks.recorded)
    } finally { vi.unstubAllGlobals() }
  })
})
