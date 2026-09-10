import type { RespondedInteractionRecord, RunDetailRecord, RunRecord, UserInteractionDetail } from '../../src/api'
import type { InteractionScope } from '../../src/lib/interactionResponse'

/** 実データ/認証情報を含まない普通答復の共通 fixture。 */
export const INTERACTION_SCOPE: InteractionScope = {
  actorId: '00000000-0000-4000-8000-000000000001',
  projectId: '00000000-0000-4000-8000-000000000020',
  runId: '00000000-0000-4000-8000-000000000030',
}

/** CHOICE の推薦を自動回答にしないため、既定選択を持たない質問。 */
export function interactionFixture(type: UserInteractionDetail['interaction_type'] = 'CLARIFICATION'): UserInteractionDetail {
  return {
    interaction_id: '00000000-0000-4000-8000-000000000040',
    run_segment_id: '00000000-0000-4000-8000-000000000050',
    agent_session_id: '00000000-0000-4000-8000-000000000060',
    interaction_type: type, prompt: { prompt: 'Which public option?', rationale: 'Clarify scope.', allow_multiple: false },
    options: [{ key: 'a', label: 'Option A', recommended: true }, { key: 'b', label: 'Option B' }],
    required: false, expires_at: '2099-01-01T00:00:00Z', status: 'OPEN', version: 3,
    continuation_mode: 'RESUME', checkpoint_checksum: `sha256:${'1'.repeat(64)}`,
    change_proposal_id: null, response: null, created_at: '2026-01-01T00:00:00Z',
  }
}

/** 原 Segment と現在 Run snapshot を返す正式な受理形状。 */
export function interactionReceipt(): RespondedInteractionRecord {
  return { run_id: INTERACTION_SCOPE.runId, project_id: INTERACTION_SCOPE.projectId,
    interaction_id: interactionFixture().interaction_id,
    response_id: '00000000-0000-4000-8000-000000000070',
    run_segment_id: '00000000-0000-4000-8000-000000000080',
    segment_no: 2, continuation_mode: 'RESUME', status: 'QUEUED', row_version: 6, idempotent_replay: false }
}

/** 返却回执より新しい snapshot を比較するための現在 Run。 */
export function interactionRun(): RunRecord {
  return { run_id: INTERACTION_SCOPE.runId, project_id: INTERACTION_SCOPE.projectId,
    task_id: '00000000-0000-4000-8000-000000000090', status: 'WAITING_FOR_INPUT', row_version: 5,
    created_at: '2026-01-01T00:00:00Z', idempotent_replay: false }
}

/** Result や Effect を混ぜない、普通答復専用の最小 authorized detail。 */
export function interactionDetail(): RunDetailRecord {
  const { idempotent_replay: _replay, ...run } = interactionRun()
  return { ...run, input: {}, selected_sources: {}, document_snapshots: [], output_schema: null,
    output_schema_checksum: null, result: null, segments: [], attempts: [], sessions: [],
    interactions: [interactionFixture()], change_proposals: [], approvals: [], effect_executions: [],
    tool_calls: [], evidence: [], skill_snapshots: [] }
}
