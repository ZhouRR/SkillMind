import type { EvaluationSubmissionInput, EvaluationSubmissionReceipt, RunResultDetail } from '../../src/api'
import type { EvaluationScope } from '../../src/lib/evaluationSubmission'

export const EVALUATION_SCOPE: EvaluationScope = {
  actorId: '00000000-0000-4000-8000-000000000090', projectId: '00000000-0000-4000-8000-000000000101',
  runId: '00000000-0000-4000-8000-000000000010', resultId: '00000000-0000-4000-8000-000000000080',
}
export const SUBMISSION_KEY = '00000000-0000-4000-8000-000000000911'
export const EVALUATION_DATA = { summary: 'Original', nullable: null, array: [1, false], 'a/b': { '~value': { a: 1, b: null } } }

/** 結果固有の業務値を権限にせず、原値照合だけに使う fixture。 */
export function evaluationResult(): RunResultDetail {
  return { result_id: EVALUATION_SCOPE.resultId, result_kind: 'STRUCTURED_OUTPUT', output_schema: 'fixture/v1',
    data: structuredClone(EVALUATION_DATA), evidence_refs: [], artifact_refs: [], change_proposal_refs: [],
    optional_schema_identity: {}, summary: 'Original', confidence: null, needs_review: true,
    usage: {}, cost: {}, validation: {}, created_at: '2026-09-10T00:00:00Z' }
}

/** UUID と原 JSON を呼出ごとに独立させ、await 中の alias を作らない。 */
export function evaluationSubmissionInput(): EvaluationSubmissionInput {
  return { submission_key: SUBMISSION_KEY, result_id: EVALUATION_SCOPE.resultId, rating: 3, verdict: 'uncertain', comment: '原评价',
    revisions: [{ pointer: '/nullable', suggested_value: null, reason: 'Keep null' }] }
}

/** Server が同じ Result から原値を付与した四 field 受付記録。 */
export function evaluationReceipt(input = evaluationSubmissionInput()): EvaluationSubmissionReceipt {
  return { project_id: EVALUATION_SCOPE.projectId, run_id: EVALUATION_SCOPE.runId, submission_key: input.submission_key,
    evaluation: { evaluation_id: '00000000-0000-4000-8000-000000000081', result_id: input.result_id,
      run_id: EVALUATION_SCOPE.runId, user_id: EVALUATION_SCOPE.actorId, rating: input.rating, verdict: input.verdict,
      comment: input.comment, revisions: input.revisions.map((revision) => ({ ...structuredClone(revision), original_value: null })),
      created_at: '2026-09-10T01:00:00.000001Z' } }
}
