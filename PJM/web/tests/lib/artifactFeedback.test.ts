import { describe, expect, it } from 'vitest'
import { ApiProblemError, type RunResultDetail } from '../../src/api'
import { artifactFailure, resultArtifactRefs } from '../../src/lib/artifactFeedback'

/** 参照収集で必要な元公開 field だけを持つ合法 result を作る。 */
function result(): RunResultDetail {
  return { result_id: '00000000-0000-4000-8000-000000000040', output_schema: 'projectmind.outcome-envelope/v1',
    result_kind: 'OUTCOME_ENVELOPE', data: {}, evidence_refs: [], artifact_refs: ['art_original'],
    change_proposal_refs: [], optional_schema_identity: {}, summary: 'Original', confidence: null,
    needs_review: true, usage: {}, cost: {}, validation: {}, created_at: '2026-09-10T00:00:00Z' }
}

describe('Artifact display boundaries', () => {
  it('collects only explicit reference positions without treating URLs or business fields as authority', () => {
    const value = result()
    value.data = { deliverables: [{ artifact_ref: 'art_nested' }, { artifact_ref: 'art_original' }, { url: 'https://example.invalid' }],
      structured_data: { artifact_ref: 'art_business', artifact_refs: ['art_business_list'] },
      findings: [{ artifact_ref: 'art_finding' }] }
    const original = structuredClone(value)
    expect(resultArtifactRefs(value)).toEqual(['art_original', 'art_nested'])
    expect(value).toEqual(original)
    value.result_kind = 'STRUCTURED_OUTPUT'
    expect(resultArtifactRefs(value)).toEqual(['art_original'])
    expect(resultArtifactRefs(null)).toEqual([])
  })

  it.each([
    [401, 'anything', 'sessionExpired'], [403, 'anything', 'denied'], [404, 'run_not_found', 'denied'],
    [404, 'project_not_found', 'denied'], [404, 'artifact_not_found', 'notFound'],
    [409, 'artifact_content_invalid', 'contentInvalid'], [503, 'artifact_storage_unavailable', 'storageUnavailable'],
    [200, 'response_too_large', 'tooLarge'], [503, 'unknown', 'loadFailed'], [409, 'unknown', 'loadFailed'],
  ])('uses static failure vocabulary for %s/%s', (status, code, key) => {
    expect(artifactFailure(new ApiProblemError('Private internal detail', Number(status), String(code)))).toEqual({ key })
  })
})
