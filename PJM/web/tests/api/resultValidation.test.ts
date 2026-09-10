import { afterEach, describe, expect, it, vi } from 'vitest'
import fixture from '../../../contracts/examples/run-detail.v1.json'
import checkedFixture from '../../../contracts/examples/run-detail-documents.v1.json'
import artifactFixture from '../../../contracts/examples/run-detail-artifacts.v1.json'
import { isResultReferenceChecks, loadRunDetail, type ResultReferenceChecks } from '../../src/api'

const CHECKS: ResultReferenceChecks = {
  version: 'projectmind.result-reference-checks/v1', evidence: 'RUN_OWNERSHIP',
  proposals: 'RUN_OWNERSHIP_AND_STATE', effects: 'PLATFORM_RECORD_MATCH', artifacts: 'NOT_VERIFIED',
}
const ABSENT = Symbol('historical scope absent')

/** 公開 example の原 data を保持し、今回変更する validation だけを差し替える。 */
function response(kind: 'OUTCOME_ENVELOPE' | 'STRUCTURED_OUTPUT', checks: unknown = ABSENT) {
  return { ...structuredClone(fixture), result: { ...structuredClone(fixture.result), result_kind: kind,
    validation: { schema_valid: true, ...(checks === ABSENT ? {} : {
      evidence_refs_valid: true, change_proposal_refs_valid: true,
      ...(kind === 'OUTCOME_ENVELOPE' ? { outcome_envelope_valid: true } : {}), reference_checks: checks,
    }) } } }
}

afterEach(() => vi.unstubAllGlobals())

describe('saved Result reference-check protocol', () => {
  it('consumes the shared v2 example while preserving its original empty artifact set and data', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(artifactFixture))))
    const detail = await loadRunDetail(artifactFixture.project_id, artifactFixture.run_id)
    expect(detail.result?.validation).toEqual(artifactFixture.result.validation)
    expect(detail.result?.data).toEqual(artifactFixture.result.data)
    expect(detail.result?.validation.reference_checks?.version).toBe('projectmind.result-reference-checks/v2')
    expect(detail.result?.artifact_refs).toEqual([])
  })

  it('rejects v1 contradicted by an artifact pass flag but keeps false and historical absence', async () => {
    const payload = response('OUTCOME_ENVELOPE', CHECKS)
    Object.assign(payload.result.validation, { artifact_refs_valid: true })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))))
    await expect(loadRunDetail(payload.project_id, payload.run_id)).rejects.toThrow('contract')
    Object.assign(payload.result.validation, { artifact_refs_valid: false })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))))
    expect((await loadRunDetail(payload.project_id, payload.run_id)).result?.validation).toEqual(payload.result.validation)
    const old = response('OUTCOME_ENVELOPE')
    Object.assign(old.result.validation, { artifact_refs_valid: true })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(old))))
    expect((await loadRunDetail(old.project_id, old.run_id)).result?.validation).not.toHaveProperty('reference_checks')
  })

  it.each(['OUTCOME_ENVELOPE', 'STRUCTURED_OUTPUT'] as const)('accepts exact v2 and requires saved artifact validity for %s', async (kind) => {
    const checks = { ...CHECKS, version: 'projectmind.result-reference-checks/v2', artifacts: 'RUN_OWNERSHIP_AND_CONTENT',
      effects: kind === 'OUTCOME_ENVELOPE' ? 'PLATFORM_RECORD_MATCH' : 'NOT_APPLICABLE' }
    const payload = response(kind, checks)
    Object.assign(payload.result.validation, { artifact_refs_valid: true })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))))
    expect((await loadRunDetail(payload.project_id, payload.run_id)).result?.validation).toEqual(payload.result.validation)
    for (const flag of [undefined, false, null, 'true', 1]) {
      Object.assign(payload.result.validation, { artifact_refs_valid: flag })
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))))
      await expect(loadRunDetail(payload.project_id, payload.run_id)).rejects.toThrow('contract')
    }
  })

  it('accepts the same new checked example as the backend without rewriting its data or flags', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(checkedFixture))))
    const detail = await loadRunDetail(checkedFixture.project_id, checkedFixture.run_id)
    expect(detail.result?.validation.reference_checks).toEqual(CHECKS)
    expect(detail.result?.validation).toEqual(checkedFixture.result.validation)
    expect(detail.result?.data).toEqual(checkedFixture.result.data)
  })

  it.each(['OUTCOME_ENVELOPE', 'STRUCTURED_OUTPUT'] as const)('accepts exact saved scope for %s', async (kind) => {
    const checks = { ...CHECKS, effects: kind === 'OUTCOME_ENVELOPE' ? 'PLATFORM_RECORD_MATCH' : 'NOT_APPLICABLE' }
    const payload = response(kind, checks)
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    const detail = await loadRunDetail(payload.project_id, payload.run_id)
    expect(detail.result?.validation.reference_checks).toEqual(checks)
    expect(detail.result?.data).toEqual(payload.result.data)
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0]?.[0]).toContain(`/projects/${payload.project_id}/runs/${payload.run_id}/detail`)
  })

  it.each(['OUTCOME_ENVELOPE', 'STRUCTURED_OUTPUT'] as const)('keeps missing historical scope missing for %s', async (kind) => {
    const payload = response(kind)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))))
    const detail = await loadRunDetail(payload.project_id, payload.run_id)
    expect(detail.result?.validation).toEqual({ schema_valid: true })
    expect(detail.result?.validation).not.toHaveProperty('reference_checks')
    expect(detail.result?.data).toEqual(payload.result.data)
  })

  it.each([
    null, [], true, 'verified', {},
    { ...CHECKS, version: 'projectmind.result-reference-checks/v2' },
    { ...CHECKS, evidence: 'PROJECT_OWNERSHIP' },
    { ...CHECKS, proposals: 'RUN_OWNERSHIP' },
    { ...CHECKS, effects: 'REMOTE_VERIFIED' },
    { ...CHECKS, artifacts: 'VERIFIED' },
    { ...CHECKS, extra: 'not-public' },
    ...Object.keys(CHECKS).map((key) => Object.fromEntries(Object.entries(CHECKS).filter(([field]) => field !== key))),
  ])('rejects malformed new scope instead of interpreting it as a legacy record: %j', async (checks) => {
    const payload = response('OUTCOME_ENVELOPE', checks)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))))
    expect(isResultReferenceChecks(checks, 'OUTCOME_ENVELOPE')).toBe(false)
    await expect(loadRunDetail(payload.project_id, payload.run_id)).rejects.toThrow('contract')
  })

  it.each(['OUTCOME_ENVELOPE', 'STRUCTURED_OUTPUT'] as const)('rejects effect-check claims from the other Result kind: %s', async (kind) => {
    const checks = { ...CHECKS, effects: kind === 'OUTCOME_ENVELOPE' ? 'NOT_APPLICABLE' : 'PLATFORM_RECORD_MATCH' }
    const payload = response(kind, checks)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))))
    await expect(loadRunDetail(payload.project_id, payload.run_id)).rejects.toThrow('contract')
  })

  it.each(['schema_valid', 'evidence_refs_valid', 'change_proposal_refs_valid', 'outcome_envelope_valid'])(
    'does not accept new recorded checks with missing or non-true %s', async (flag) => {
      for (const value of [undefined, false, null, 'true', 1]) {
        const payload = response('OUTCOME_ENVELOPE', CHECKS)
        Object.assign(payload.result.validation, { [flag]: value })
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))))
        await expect(loadRunDetail(payload.project_id, payload.run_id)).rejects.toThrow('contract')
      }
    },
  )

  it('does not strengthen or fill historical validation flags without saved reference checks', async () => {
    const payload = response('OUTCOME_ENVELOPE')
    Object.assign(payload.result.validation, { schema_valid: false, evidence_refs_valid: null })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))))
    const detail = await loadRunDetail(payload.project_id, payload.run_id)
    expect(detail.result?.validation).toEqual(payload.result.validation)
  })
})
