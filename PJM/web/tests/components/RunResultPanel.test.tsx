import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { AgentSessionDetail, EvidenceDetail, RunDetailRecord } from '../../src/api'
import {
  RunResultPanel,
  collectSubagentDispatches,
  groupSessionsByLineage,
  hasIncompleteBranch,
  type RunDetailState,
} from '../../src/components/RunResultPanel'

/** Result view test 用の Project-scoped detail を生成する。 */
function detail(result: RunDetailRecord['result']): RunDetailRecord {
  return {
    run_id: '00000000-0000-4000-8000-000000000001',
    project_id: '00000000-0000-4000-8000-000000000002',
    task_id: '00000000-0000-4000-8000-000000000003',
    status: result === null ? 'FAILED' : 'SUCCEEDED',
    row_version: 4,
    created_at: '2026-07-02T13:00:00Z',
    input: { ticket_id: 'fixture-001' },
    selected_sources: { issue_source: 'csv' },
    output_schema: {
      type: 'object',
      properties: {
        summary: { type: 'string', description: 'Result summary' },
        field_assessments: { type: 'array', items: { type: 'object' } },
      },
    },
    output_schema_checksum: `sha256:${'2'.repeat(64)}`,
    result,
    segments: [{
      run_segment_id: '00000000-0000-4000-8000-000000000100',
      segment_no: 1,
      trigger_type: 'INITIAL',
      trigger_ref: null,
      status: result === null ? 'FAILED' : 'COMPLETED',
      objective: { text: 'Analyze the project input' },
      checkpoint: {},
      continuation_mode: 'INITIAL',
      parent_agent_session_id: null,
      task_brief_checksum: `sha256:${'5'.repeat(64)}`,
      started_at: '2026-07-02T13:00:00Z',
      finished_at: '2026-07-02T13:01:00Z',
      created_at: '2026-07-02T13:00:00Z',
    }],
    attempts: [],
    sessions: [],
    interactions: [],
    change_proposals: [],
    approvals: [],
    effect_executions: [],
    tool_calls: [],
    evidence: result === null ? [] : [{
      evidence_ref: 'ev_fixture_001',
      tool_call_id: '00000000-0000-4000-8000-000000000004',
      evidence_type: 'ticket-row',
      source_uri: 'fixture://jaf/tickets.csv',
      source_locator: { row: 1 },
      content_hash: `sha256:${'1'.repeat(64)}`,
      snapshot_uri: null,
      excerpt: 'sanitized excerpt',
      metadata: {},
      created_at: '2026-07-02T13:00:01Z',
    }],
    skill_snapshots: [{
      skill_version_id: '00000000-0000-4000-8000-000000000203',
      sort_order: 0,
      manifest_checksum: `sha256:${'3'.repeat(64)}`,
      config_snapshot: {},
    }],
  }
}

/** Ready state を静的 HTML へ描画して利用者向け文言を検証する。 */
function render(detailRecord: RunDetailRecord): string {
  const state: RunDetailState = { status: 'ready', detail: detailRecord }
  return renderToStaticMarkup(<RunResultPanel csrfToken={'s'.repeat(32)} state={state} />)
}

describe('Run Result view', () => {
  it('shows an explicit state when a failed Run has no validated Result', () => {
    /** FAILED を空白や成功表示にせず、Result 不在を明示することを守る。 */
    expect(render(detail(null))).toContain('尚未生成通过校验的结果')
  })

  it('renders an open choice interaction beside the segment timeline', () => {
    /** WAITING Run が Result 不在でも選択肢と継続操作を失わない。 */
    const waiting = detail(null)
    waiting.status = 'WAITING_FOR_INPUT'
    waiting.segments[0] = { ...waiting.segments[0]!, status: 'WAITING' }
    waiting.interactions = [{
      interaction_id: '00000000-0000-4000-8000-000000000110',
      run_segment_id: waiting.segments[0]!.run_segment_id!,
      agent_session_id: '00000000-0000-4000-8000-000000000111',
      interaction_type: 'CHOICE',
      prompt: {
        prompt: 'Which severity should be used?',
        rationale: 'Project context determines the final rating.',
        impact: 'The recommendation may change.',
        allow_multiple: false,
      },
      options: [
        { key: 'high', label: 'High', description: 'Treat as urgent.' },
        { key: 'medium', label: 'Medium', description: 'Schedule normal remediation.' },
      ],
      required: true,
      expires_at: '2026-07-03T13:00:00Z',
      status: 'OPEN',
      version: 1,
      continuation_mode: 'FORK',
      checkpoint_checksum: `sha256:${'6'.repeat(64)}`,
      change_proposal_id: null,
      response: null,
      created_at: '2026-07-02T13:00:01Z',
    }]

    const html = render(waiting)

    expect(html).toContain('执行过程与审计')
    expect(html).toContain('Which severity should be used?')
    expect(html).toContain('Treat as urgent.')
    expect(html).toContain('提交并继续执行')
  })

  it('renders exact proposal approval data and effect read-back evidence', () => {
    /** Approval UI が summary だけでなく exact version/checksum/precondition を表示する。 */
    const waiting = detail(null)
    waiting.status = 'WAITING_FOR_APPROVAL'
    waiting.segments[0] = { ...waiting.segments[0]!, status: 'WAITING' }
    waiting.change_proposals = [{
      proposal_id: '00000000-0000-4000-8000-000000000120',
      proposal_ref: 'cp_effect001',
      project_id: waiting.project_id,
      run_id: waiting.run_id,
      run_segment_id: waiting.segments[0]!.run_segment_id!,
      agent_session_id: '00000000-0000-4000-8000-000000000121',
      target_binding_id: '00000000-0000-4000-8000-000000000122',
      integration_id: '00000000-0000-4000-8000-000000000123',
      effect_intent_key: 'update-tracker',
      capability_version: 'issue.update/v1',
      operation: 'Record the reviewed status',
      target: { locator: '42', display: 'Issue 42' },
      summary: 'Set Issue 42 to resolved',
      changes: [{ path: '/fields/status_id', action: 'SET', value: 3 }],
      precondition: { revision: '2026-07-18T12:00:00Z' },
      evidence_refs: ['ev_fixture_001'],
      risk_level: 'MEDIUM',
      reversible: true,
      rollback: { description: 'Restore the prior status.' },
      verification: { method: 'READ_BACK', paths: ['/fields/status_id'] },
      status: 'PENDING_APPROVAL',
      version: 1,
      checksum: `sha256:${'a'.repeat(64)}`,
      expires_at: '2099-07-19T12:00:00Z',
      created_at: '2026-07-18T12:00:00Z',
      updated_at: '2026-07-18T12:00:00Z',
    }]
    waiting.approvals = [{
      approval_id: '00000000-0000-4000-8000-000000000124',
      proposal_id: waiting.change_proposals[0]!.proposal_id,
      run_id: waiting.run_id,
      source: 'PREAUTHORIZATION',
      decision: 'APPROVED',
      actor_id: null,
      preauthorization_id: '00000000-0000-4000-8000-000000000125',
      proposal_version: 1,
      proposal_checksum: waiting.change_proposals[0]!.checksum,
      reason: 'Matched exact LOW policy',
      created_at: '2026-07-18T12:01:00Z',
    }]
    waiting.effect_executions = [{
      effect_execution_id: '00000000-0000-4000-8000-000000000126',
      proposal_id: waiting.change_proposals[0]!.proposal_id,
      run_id: waiting.run_id,
      approval_id: waiting.approvals[0]!.approval_id,
      tool_call_id: '00000000-0000-4000-8000-000000000127',
      status: 'APPLIED',
      provider: 'redmine',
      provider_version: 'redmine-cas/v1',
      before_ref: 'ev_before001',
      after_ref: 'ev_after001',
      verification: { method: 'READ_BACK', replayed: false },
      error: null,
      attempt_no: 1,
      executed_at: '2026-07-18T12:02:00Z',
      created_at: '2026-07-18T12:01:00Z',
      updated_at: '2026-07-18T12:02:00Z',
    }]

    const html = render(waiting)

    expect(html).toContain('受控外部变更')
    expect(html).toContain('Issue 42')
    expect(html).toContain('/fields/status_id')
    expect(html).toContain(`sha256:${'a'.repeat(64)}`)
    expect(html).toContain('批准并执行')
    expect(html).toContain('PREAUTHORIZATION APPROVED')
    expect(html).toContain('ev_before001')
    expect(html).toContain('ev_after001')
  })

  it('renders nested generated-schema values and keeps excerpts collapsed', () => {
    /** Evidence 不足を confidence だけへ縮退させず、excerpt は既定で折り畳む。 */
    const html = render(detail({
      result_id: '00000000-0000-4000-8000-000000000005',
      output_schema: 'jaf/ticket-analyze/v1/output.schema.json',
      result_kind: 'STRUCTURED_OUTPUT',
      data: {
        field_assessments: [{
          field_id: 'cause',
          field_name: '原因',
          verdict: 'needs_confirmation',
          evidence_status: 'insufficient',
          evidence_refs: [],
          reasons: ['Missing source evidence'],
        }],
      },
      evidence_refs: [],
      artifact_refs: [],
      change_proposal_refs: [],
      optional_schema_identity: { schema_ref: 'jaf/ticket-analyze/v1/output.schema.json' },
      summary: 'Needs review',
      confidence: 0.4,
      needs_review: true,
      usage: {},
      cost: {},
      validation: { schema_valid: true },
      created_at: '2026-07-02T13:01:00Z',
    }))

    expect(html).toContain('needs_confirmation')
    expect(html).toContain('Missing source evidence')
    expect(html).toContain('<details class="evidenceCard">')
    expect(html).toContain('sanitized excerpt')
  })

  it('renders every result through the same generated-schema renderer', () => {
    /** Business path や task key を見ず、同じ object/array/scalar renderer を使用する。 */
    const html = render(detail({
      result_id: '00000000-0000-4000-8000-000000000006',
      output_schema: 'skills/generic-review/v1/output.schema.json',
      result_kind: 'STRUCTURED_OUTPUT',
      data: {
        summary: 'Repository review complete',
        // JAF と同名の field を持っていても、登録拡張 schema でなければ JAF section を描画しない。
        field_assessments: [{ field_id: 'x', evidence_refs: [] }],
      },
      evidence_refs: [],
      artifact_refs: [],
      change_proposal_refs: [],
      optional_schema_identity: { schema_ref: 'skills/generic-review/v1/output.schema.json' },
      summary: 'Repository review complete',
      confidence: 0.9,
      needs_review: false,
      usage: {},
      cost: {},
      validation: { schema_valid: true },
      created_at: '2026-07-02T13:02:00Z',
    }))

    expect(html).toContain('Repository review complete')
    expect(html).toContain('结构化结果')
    expect(html).toContain('field_assessments')
    expect(html).toContain('查看原始结果数据（JSON）')
  })

  it('renders OutcomeEnvelope without a task-specific business schema', () => {
    /** 開放式 report も専用 renderer や固定業務 field なしで交付物・Finding・制限を表示する。 */
    const html = render(detail({
      result_id: '00000000-0000-4000-8000-000000000007',
      output_schema: `sha256:${'4'.repeat(64)}`,
      result_kind: 'OUTCOME_ENVELOPE',
      data: {
        outcome_version: 'projectmind.outcome-envelope/v1',
        summary: 'Repository review complete',
        status: 'COMPLETED',
        deliverables: [{
          key: 'report',
          kind: 'report',
          title: 'Review report',
          content: 'One authorization issue was found.',
        }],
        findings: [{
          key: 'missing_auth',
          title: 'Authorization is missing',
          detail: 'The update path does not check project membership.',
          severity: 'high',
          evidence_refs: ['ev_fixture_001'],
        }],
        evidence_refs: ['ev_fixture_001'],
        artifact_refs: [],
        open_questions: [],
        limitations: ['The integration was not exercised.'],
        confidence: 0.8,
        needs_review: true,
        change_proposal_refs: [],
        effects: [],
      },
      evidence_refs: ['ev_fixture_001'],
      artifact_refs: [],
      change_proposal_refs: [],
      optional_schema_identity: {},
      summary: 'Repository review complete',
      confidence: 0.8,
      needs_review: true,
      usage: {},
      cost: {},
      validation: { schema_valid: true, outcome_envelope_valid: true },
      created_at: '2026-07-02T13:03:00Z',
    }))

    expect(html).toContain('结果概要')
    expect(html).toContain('Review report')
    expect(html).toContain('Authorization is missing')
    expect(html).toContain('The integration was not exercised.')
    expect(html).not.toContain('业务数据明细')
  })
})

describe('Run Result information order', () => {
  /** OPEN の質問を 1 件持つ WAITING Run を作る。 */
  function waitingWithOpenQuestion(): RunDetailRecord {
    const waiting = detail(null)
    waiting.status = 'WAITING_FOR_INPUT'
    waiting.interactions = [{
      interaction_id: '00000000-0000-4000-8000-000000000130',
      run_segment_id: waiting.segments[0]!.run_segment_id!,
      agent_session_id: '00000000-0000-4000-8000-000000000131',
      interaction_type: 'CLARIFICATION',
      prompt: { prompt: '需要确认的问题正文', rationale: '', impact: '' },
      options: [],
      required: true,
      expires_at: '2026-09-01T13:00:00Z',
      status: 'OPEN',
      version: 1,
      continuation_mode: 'RESUME',
      checkpoint_checksum: `sha256:${'7'.repeat(64)}`,
      change_proposal_id: null,
      response: null,
      created_at: '2026-07-02T13:00:01Z',
    }]
    return waiting
  }

  it('lifts an unanswered question above the audit trail so the next step is reachable', () => {
    // 回答待ちで停止した Run は Workspace が自動でこの画面へ切り替える。
    // 「何をすれば続くのか」が監査記録の下に埋もれていては用を成さない。
    const html = render(waitingWithOpenQuestion())

    expect(html).toContain('待你处理')
    expect(html).toContain('执行已暂停')
    expect(html.indexOf('需要确认的问题正文')).toBeLessThan(html.indexOf('执行过程与审计'))
  })

  it('keeps an answered question in the audit trail instead of the pending block', () => {
    const answered = waitingWithOpenQuestion()
    answered.interactions[0] = {
      ...answered.interactions[0]!,
      status: 'RESPONDED',
      response: {
        response_id: '00000000-0000-4000-8000-000000000141',
        actor_id: '00000000-0000-4000-8000-000000000140',
        interaction_version: 1,
        response: { text: '已确认' },
        created_at: '2026-07-02T13:05:00Z',
      },
    }

    const html = render(answered)

    expect(html).not.toContain('待你处理')
    expect(html).toContain('需要确认的问题正文')
  })

  it('collapses the reference material so the result is not buried under it', () => {
    // 工具・証拠・監査・評価は既定で畳む。畳んでも中身は DOM に残す(本 file の他断言の前提)。
    const html = render(detail(null))

    expect(html).toContain('resultCollapse')
    expect(html).toContain('resultCollapseBody')
    // 既定展開(open 属性)は付けない。
    expect(html).not.toContain('<details class="resultCollapse" open')
  })
})

describe('業務データ明細 (structured_data) rendering', () => {
  /** 入れ子配列を含む structured_data を持つ OutcomeEnvelope Result を生成する。 */
  function structuredDetail(): RunDetailRecord {
    const record = detail(null)
    record.status = 'SUCCEEDED'
    record.output_schema = {
      type: 'object',
      properties: {
        structured_data: {
          type: 'object',
          properties: {
            summary: { type: 'string', description: '分析の要約' },
            field_assessments: {
              type: 'array',
              items: {
                type: 'object',
                properties: {
                  field_key: { type: 'string', description: '評価対象のフィールド名' },
                  reason: { type: 'string', description: '判定の根拠' },
                  evidence_refs: { type: 'array', items: { type: 'string' } },
                },
              },
            },
          },
        },
      },
    }
    record.result = {
      result_id: '00000000-0000-4000-8000-000000000300',
      output_schema: 'projectmind.outcome-envelope/v1',
      result_kind: 'OUTCOME_ENVELOPE',
      data: {
        outcome_version: 'projectmind.outcome-envelope/v1',
        status: 'COMPLETED',
        deliverables: [],
        findings: [],
        evidence_refs: [],
        artifact_refs: [],
        open_questions: [],
        limitations: [],
        confidence: 0.8,
        needs_review: false,
        change_proposal_refs: [],
        effects: [],
        structured_data: {
          summary: 'ticket 1414 は status を示すが記載が不足している',
          field_assessments: [{
            field_key: 'title/subject',
            reason: 'title が null のままで、障害の内容を判別できない',
            evidence_refs: ['ev_7b7231c22daa4583b6f87a3b510949'],
          }],
        },
      },
      evidence_refs: [],
      artifact_refs: [],
      change_proposal_refs: [],
      optional_schema_identity: {},
      summary: 'ticket 1414 quality analysis',
      confidence: 0.8,
      needs_review: false,
      usage: {},
      cost: {},
      validation: { schema_valid: true, outcome_envelope_valid: true },
      created_at: '2026-07-02T13:03:00Z',
    }
    return record
  }

  it('labels fields with the schema description and hides contract keys by default', () => {
    /** 通常表示では利用者語を主にし、技術 key は「技術項目を表示」操作へ退避する。 */
    const html = render(structuredDetail())

    expect(html).toContain('評価対象のフィールド名')
    expect(html).not.toContain('<code class="mono">field_key</code>')
    expect(html).not.toContain('field_key · 評価対象のフィールド名')
    expect(html).toContain('查看技术字段')
  })

  it('does not truncate business values with the fixed Run-facts layout', () => {
    /** .runFacts は Run 事実行用の固定 3 列 + 省略表示で、任意構造の業務データを
        載せると値が切れる。専用 layout を使い続けることを回帰点として固定する。 */
    const html = render(structuredDetail())

    expect(html).toContain('class="schemaFacts"')
    expect(html).toContain('title が null のままで、障害の内容を判別できない')
    // 配列・object の field は全幅を取り、scalar だけを並列に置く。
    expect(html).toContain('class="schemaFactWide"')
  })

  it('separates the array index from the item body', () => {
    /** 以前は inline の strong で序号が値と地続きに描画され、"1ev_7b72..." に見えていた。 */
    const html = render(structuredDetail())

    expect(html).toContain('class="schemaItemIndex"')
    expect(html).not.toMatch(/<strong>1<\/strong><dl/)
  })

  it('renders scalar arrays as a chip row instead of numbered cards', () => {
    /** evidence_refs のような scalar 配列を番号付き card にすると余白ばかりになる。 */
    const html = render(structuredDetail())

    expect(html).toContain('class="schemaScalarList"')
    expect(html).toContain('ev_7b7231c22daa4583b6f87a3b510949')
  })

  it('folds the tail of long collections while keeping every item reachable', () => {
    /** 結果の縦の長さは件数で伸びる。畳んでも総数は見出しに残し、中身は DOM から消さない
        (閉じた `<details>` のまま検索・展開で必ず辿れる)。 */
    const record = structuredDetail()
    const data = record.result!.data as Record<string, unknown>
    data.findings = Array.from({ length: 23 }, (_, index) => ({
      key: `finding-${index + 1}`,
      title: `検出事項 ${index + 1}`,
      detail: `詳細 ${index + 1}`,
      evidence_refs: [],
    }))
    const structured = data.structured_data as Record<string, unknown>
    structured.field_assessments = Array.from({ length: 12 }, (_, index) => ({
      field_key: `field-${index + 1}`,
      reason: `理由 ${index + 1}`,
      evidence_refs: [],
    }))

    const html = render(record)

    // 発見 23 件は先頭 5 件だけ展開し、残り 18 件を畳む。
    expect(html).toContain('展开其余 18 项')
    // 構造化配列 12 件も同じ規則で畳む。
    expect(html).toContain('展开其余 7 项')
    // 総数は見出しの件数徽标から読める。
    expect(html).toContain('<span class="eventCount">23</span>')
    // 畳んだ側も DOM に残る:最後の 1 件が消えていないことを確認する。
    expect(html).toContain('検出事項 23')
    expect(html).toContain('理由 12')
  })
})

describe('repository change review', () => {
  /** 承認画面は direct 既定の下では唯一の闸門。全文が読めることを回帰で固定する。 */
  it('renders code changes per file instead of an escaped JSON blob', () => {
    const waiting = detail(null)
    waiting.status = 'WAITING_FOR_APPROVAL'
    waiting.segments[0] = { ...waiting.segments[0]!, status: 'WAITING' }
    waiting.change_proposals = [{
      proposal_id: '00000000-0000-4000-8000-000000000130',
      proposal_ref: 'cp_repo001',
      project_id: waiting.project_id,
      run_id: waiting.run_id,
      run_segment_id: waiting.segments[0]!.run_segment_id!,
      agent_session_id: '00000000-0000-4000-8000-000000000131',
      target_binding_id: '00000000-0000-4000-8000-000000000132',
      integration_id: '00000000-0000-4000-8000-000000000133',
      effect_intent_key: 'apply-fix',
      capability_version: 'repository.write/v1',
      operation: 'commit',
      target: { locator: 'main', display: 'Guard against a null payload' },
      summary: 'Guard against a null payload',
      changes: [
        {
          path: '/files/src/handler.py',
          action: 'SET',
          value: 'def handle(payload):\n    if payload is None:\n        return None\n',
        },
        { path: '/files/src/legacy.py', action: 'REMOVE', value: null },
      ],
      precondition: { revision: '9f2c1d4b8a6e5307c1b2d3e4f5a6b7c8d9e0f1a2' },
      evidence_refs: ['ev_source_1'],
      risk_level: 'MEDIUM',
      reversible: true,
      rollback: { description: 'Revert the commit.' },
      verification: {
        method: 'READ_BACK',
        paths: ['/files/src/handler.py', '/files/src/legacy.py'],
      },
      status: 'PENDING_APPROVAL',
      version: 1,
      checksum: `sha256:${'b'.repeat(64)}`,
      expires_at: '2099-07-19T12:00:00Z',
      created_at: '2026-07-25T12:00:00Z',
      updated_at: '2026-07-25T12:00:00Z',
    }]
    const state: RunDetailState = { status: 'ready', detail: waiting }

    const html = renderToStaticMarkup(
      <RunResultPanel csrfToken={'s'.repeat(32)} state={state} />,
    )

    // path と操作が個別に読め、本文がそのまま出る (JSON 転義ではない)。
    expect(html).toContain('src/handler.py')
    expect(html).toContain('src/legacy.py')
    expect(html).toContain('if payload is None:')
    expect(html).not.toContain('\\n    if payload is None:')
    expect(html).toContain('写入')
    expect(html).toContain('删除')
    // 基準 revision は審査の前提なので常に見せる。
    expect(html).toContain('9f2c1d4b8a6e5307c1b2d3e4f5a6b7c8d9e0f1a2')
  })
})
describe('subagent fan-out', () => {
  /** `subagent-dispatch` 証拠を組み立てる。 */
  function dispatchEvidence(
    branches: Array<{ key: string; outcome: string; session: string }>,
  ): EvidenceDetail {
    return {
      evidence_ref: 'ev_dispatch_1',
      tool_call_id: '00000000-0000-4000-8000-0000000000c1',
      evidence_type: 'subagent-dispatch',
      source_uri: 'projectmind://runs/x/subagents',
      source_locator: { branches: branches.map((item) => item.key) },
      content_hash: `sha256:${'a'.repeat(64)}`,
      snapshot_uri: null,
      excerpt: 'objective: check three aspects',
      metadata: {
        objective: '三个互不相关的改动面需要分别核对',
        branches,
        turns_per_branch: 4,
        output_bytes_per_branch: 262144,
      },
      created_at: '2026-07-26T09:00:00Z',
    }
  }

  it('reads the branches out of the evidence metadata', () => {
    const dispatches = collectSubagentDispatches([
      dispatchEvidence([
        { key: 'schema', outcome: 'COMPLETED', session: 's1' },
        { key: 'api', outcome: 'FAILED', session: 's2' },
      ]),
    ])

    expect(dispatches).toHaveLength(1)
    expect(dispatches[0]?.branches.map((item) => item.key)).toEqual(['schema', 'api'])
    expect(dispatches[0]?.turnsPerBranch).toBe(4)
  })

  it('ignores evidence of other types', () => {
    const dispatches = collectSubagentDispatches([
      { ...dispatchEvidence([]), evidence_type: 'workspace-file' },
    ])

    expect(dispatches).toEqual([])
  })

  it('flags a run whose fan-out did not cover every aspect', () => {
    // 扇出の固有の危うさは、一路が失敗していても要約が普通に返ること。未完了を検出できないと
    // 部分的な網羅を全面的な確認として読んでしまう。
    const complete = collectSubagentDispatches([
      dispatchEvidence([{ key: 'a', outcome: 'COMPLETED', session: 's1' }]),
    ])
    const partial = collectSubagentDispatches([
      dispatchEvidence([
        { key: 'a', outcome: 'COMPLETED', session: 's1' },
        { key: 'b', outcome: 'TIMED_OUT', session: 's2' },
      ]),
    ])

    expect(hasIncompleteBranch(complete)).toBe(false)
    expect(hasIncompleteBranch(partial)).toBe(true)
  })

  it('tolerates malformed branch entries instead of throwing', () => {
    // metadata は Provider が書くが、契約外の値が入っても画面は落とさない。
    const dispatches = collectSubagentDispatches([
      { ...dispatchEvidence([]), metadata: { branches: [null, 'x', { key: 'ok' }] } },
    ])

    expect(dispatches[0]?.branches.map((item) => item.key)).toEqual(['ok'])
  })
})

/** 時間線 test 用の AgentSession を生成する。 */
function session(
  id: string,
  kind: AgentSessionDetail['session_kind'],
  parent: string | null,
  status: string,
): AgentSessionDetail {
  return {
    agent_session_id: id,
    run_segment_id: '00000000-0000-4000-8000-000000000100',
    run_attempt_id: '00000000-0000-4000-8000-000000000200',
    sdk_session_id: kind === 'SUBAGENT' && status !== 'CLOSED' ? null : `sdk-${id}`,
    parent_session_id: parent,
    continuation_mode: kind === 'SUBAGENT' ? 'BRANCH' : 'INITIAL',
    session_kind: kind,
    checkpoint_checksum: null,
    engine_options_checksum: null,
    engine: 'claude-agent-sdk',
    sdk_version: '0.2.110',
    cli_version: '2.1.191',
    model: 'fixture-model',
    status,
    usage: kind === 'SUBAGENT' ? { branch_key: id } : {},
    cost: {},
    created_at: '2026-07-02T13:00:00Z',
    updated_at: '2026-07-02T13:01:00Z',
  }
}

describe('groupSessionsByLineage', () => {
  it('nests fan-out branches under their parent instead of listing them flat', () => {
    // 平坦に並べると「順に 3 回会話した」と読めるが、実際は主分析 1 本 + 併走した子分析 2 本。
    // 件数の意味が変わるため、主分析だけを数える。
    const lineages = groupSessionsByLineage([
      session('primary', 'PRIMARY', null, 'CLOSED'),
      session('a', 'SUBAGENT', 'primary', 'CLOSED'),
      session('b', 'SUBAGENT', 'primary', 'INTERRUPTED'),
    ])

    expect(lineages).toHaveLength(1)
    expect(lineages[0]?.primary.agent_session_id).toBe('primary')
    expect(lineages[0]?.branches.map((item) => item.agent_session_id)).toEqual(['a', 'b'])
  })

  it('keeps an orphan branch visible instead of dropping it', () => {
    // 親が同じ Segment に居ない子を捨てると、監査記録から黙って消える。表示が崩れる方がまだよい。
    const lineages = groupSessionsByLineage([
      session('primary', 'PRIMARY', null, 'CLOSED'),
      session('orphan', 'SUBAGENT', 'somewhere-else', 'FAILED'),
    ])

    expect(lineages.map((item) => item.primary.agent_session_id)).toEqual(['primary', 'orphan'])
  })

  it('preserves branch order within a lineage', () => {
    const lineages = groupSessionsByLineage([
      session('p1', 'PRIMARY', null, 'CLOSED'),
      session('p2', 'PRIMARY', null, 'CLOSED'),
      session('b2', 'SUBAGENT', 'p2', 'CLOSED'),
      session('b1', 'SUBAGENT', 'p1', 'CLOSED'),
    ])

    expect(lineages[0]?.branches.map((item) => item.agent_session_id)).toEqual(['b1'])
    expect(lineages[1]?.branches.map((item) => item.agent_session_id)).toEqual(['b2'])
  })
})
