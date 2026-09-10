import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  API_BASE,
  META_ENDPOINT,
  cancelRun,
  createEvaluation,
  createTaskRun,
  loadRunDetail,
  loadRunHistory,
  loadEvaluations,
  loadSkillInterpretation,
  parseSkillSource,
  respondToInteraction,
  saveSkillImport,
  subscribeRunEvents,
  type RunEventRecord,
} from '../../src/api/index'

const CSRF = 's'.repeat(32)

const RUN_RESPONSE = {
  run_id: '00000000-0000-4000-8000-000000000010',
  project_id: '00000000-0000-4000-8000-000000000020',
  task_id: '00000000-0000-4000-8000-000000000030',
  status: 'QUEUED',
  row_version: 1,
  created_at: '2026-07-01T00:00:00Z',
  idempotent_replay: false,
} as const

const SKILL_PARSE_RESPONSE = {
  normalized_package: {
    package_format: 'skillmind.normalized/v1',
    source: {
      type: 'directory',
      content_hash: 'sha256:source',
      detected_adapter: 'directory-skill/v1',
      files: [],
    },
    metadata: {
      name: 'Ticket Reviewer',
      description: 'Review ticket',
      argument_hint: null,
    },
    resources: {
      scripts: [],
      references: ['references/rules.md'],
      assets: [],
    },
    declared_tools: ['Read'],
    diagnostics: [{
      severity: 'warning',
      code: 'declared_tools_not_authorized',
      message: 'not authorized',
      path: 'SKILL.md',
    }],
  },
  runtime_manifest_draft: {
    identity: {
      skill_key: 'ticket-reviewer',
      source_hash: 'sha256:source',
      interpreter_version: 'deterministic-parser/1.0.0',
    },
    compatibility: {
      level: 'assisted',
      confidence: 0.25,
      diagnostics: [],
    },
    tools: [],
    extensions: {
      normalized_package_hash: 'sha256:normalized',
      declared_tools: ['Read'],
    },
  },
  capability_blueprint: {
    blueprint_version: 'skillmind.capability-blueprint/v1',
    capabilities: [],
    tasks: [],
    resource_requirements: [],
    guidance: {
      required_rules: [],
      recommended_steps: [],
      quality_criteria: [],
      prohibited_actions: [],
    },
    interaction_points: [],
    effect_intents: [],
  },
} as const

const STORED_SKILL_RESPONSE = {
  skill_source_id: '00000000-0000-4000-8000-000000000040',
  interpretation_id: '00000000-0000-4000-8000-000000000050',
  organization_id: '00000000-0000-4000-8000-000000000002',
  name: 'Ticket Reviewer',
  source_hash: 'sha256:source',
  source_type: 'directory',
  interpretation_status: 'PREVIEW_READY',
  compatibility_level: 'assisted',
  confidence: 0.25,
  interpreter_version: 'deterministic-parser/1.0.0',
  created_at: '2026-07-02T01:30:00Z',
  preview: SKILL_PARSE_RESPONSE,
} as const

const RUN_DETAIL_RESPONSE = {
  run_id: RUN_RESPONSE.run_id,
  project_id: RUN_RESPONSE.project_id,
  task_id: RUN_RESPONSE.task_id,
  created_at: RUN_RESPONSE.created_at,
  status: 'SUCCEEDED',
  row_version: 4,
  input: { ticket_id: 'fixture-001' },
  selected_sources: { issue_source: 'csv', repository_source: 'git' },
  document_snapshots: [],
  output_schema: { type: 'object', properties: { issue: { type: 'object' } } },
  output_schema_checksum: `sha256:${'4'.repeat(64)}`,
  result: {
    result_id: '00000000-0000-4000-8000-000000000040',
    output_schema: `sha256:${'c'.repeat(64)}`,
    result_kind: 'STRUCTURED_OUTPUT',
    data: { issue: { id: 'fixture-001' }, field_assessments: [] },
    evidence_refs: [],
    artifact_refs: [],
    change_proposal_refs: [],
    optional_schema_identity: { schema_ref: `sha256:${'c'.repeat(64)}` },
    summary: 'completed',
    confidence: 0.8,
    needs_review: false,
    usage: {},
    cost: {},
    validation: { schema_valid: true },
    created_at: '2026-07-02T13:00:00Z',
  },
  segments: [{
    run_segment_id: '00000000-0000-4000-8000-000000000101',
    segment_no: 1,
    trigger_type: 'INITIAL',
    trigger_ref: null,
    status: 'COMPLETED',
    objective: { text: 'Analyze the ticket' },
    checkpoint: {},
    continuation_mode: 'INITIAL',
    parent_agent_session_id: null,
    task_brief_checksum: `sha256:${'5'.repeat(64)}`,
    started_at: '2026-07-02T12:59:59Z',
    finished_at: '2026-07-02T13:00:01Z',
    created_at: '2026-07-02T12:59:58Z',
  }],
  attempts: [],
  sessions: [],
  interactions: [],
  change_proposals: [],
  approvals: [],
  effect_executions: [],
  tool_calls: [{
    tool_call_id: '00000000-0000-4000-8000-000000000050',
    run_attempt_id: '00000000-0000-4000-8000-000000000060',
    agent_session_id: '00000000-0000-4000-8000-000000000070',
    tool_name: 'issue_read_v1',
    capability: 'issue.read/v1',
    provider: 'csv-fixture',
    arguments_summary: { ticket_id: 'fixture-001' },
    status: 'SUCCEEDED',
    duration_ms: 12,
    created_at: '2026-07-02T13:00:00Z',
  }],
  evidence: [{
    evidence_ref: 'ev_fixture_001',
    tool_call_id: '00000000-0000-4000-8000-000000000050',
    evidence_type: 'ticket-row',
    source_uri: 'fixture://ticket-review/tickets.csv',
    source_locator: { row: 1 },
    content_hash: `sha256:${'1'.repeat(64)}`,
    snapshot_uri: null,
    excerpt: 'sanitized',
    metadata: {},
    created_at: '2026-07-02T13:00:00Z',
  }],
  skill_snapshots: [{
    skill_version_id: '00000000-0000-4000-8000-000000000203',
    sort_order: 0,
    manifest_checksum: `sha256:${'3'.repeat(64)}`,
    config_snapshot: {},
  }],
} as const

const RUN_HISTORY_RESPONSE = {
  items: [{
    run_id: RUN_RESPONSE.run_id,
    project_id: RUN_RESPONSE.project_id,
    task_id: RUN_RESPONSE.task_id,
    status: 'SUCCEEDED',
    row_version: 4,
    created_at: RUN_RESPONSE.created_at,
    started_at: '2026-07-01T00:00:01Z',
    finished_at: '2026-07-01T00:01:00Z',
    input: { ticket_id: 'fixture-001', report_language: 'ja-JP' },
    selected_sources: { issue_source: 'csv', repository_source: 'git' },
    result_summary: 'completed',
    result_confidence: 0.8,
    result_needs_review: false,
  }],
  limit: 10,
  offset: 0,
  has_more: false,
} as const

const EVALUATION_RESPONSE = {
  evaluation_id: '00000000-0000-4000-8000-000000000081',
  result_id: '00000000-0000-4000-8000-000000000080',
  run_id: RUN_RESPONSE.run_id,
  user_id: '00000000-0000-4000-8000-000000000090',
  rating: 4,
  verdict: 'partially_accurate',
  comment: 'Needs correction',
  revisions: [{
    pointer: '/field_assessments/0/proposed_value',
    original_value: 'before',
    suggested_value: 'after',
    reason: 'Evidence confirmed',
  }],
  created_at: '2026-07-03T12:00:00Z',
} as const

afterEach(() => vi.unstubAllGlobals())

/** API request が context path 配下の Traefik route から外れないことを検証する。 */
describe('API routing contract', () => {
  it('keeps API requests under the Vite base path', () => {
    expect(API_BASE).toBe(`${import.meta.env.BASE_URL}api/v1`)
    expect(META_ENDPOINT).toBe(`${import.meta.env.BASE_URL}api/v1/meta`)
    expect(META_ENDPOINT).not.toContain('//')
  })

  it('creates a published task run with idempotency and validates its response', async () => {
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() => Promise.resolve(new Response(JSON.stringify(RUN_RESPONSE), {
      status: 201,
      headers: { 'Content-Type': 'application/json' },
    })))
    vi.stubGlobal('fetch', fetchMock)

    const run = await createTaskRun(
      RUN_RESPONSE.project_id,
      {
        skill_version_id: '00000000-0000-4000-8000-000000000203',
        task_key: 'review',
        input: { subject: 'Review source' },
        sources: { repository: 'git' },
      },
      'request-001',
      CSRF,
    )

    expect(run.status).toBe('QUEUED')
    expect(fetchMock).toHaveBeenCalledOnce()
    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    const [url, init] = call
    expect(url).toBe(`${API_BASE}/projects/${RUN_RESPONSE.project_id}/task-runs`)
    expect(init?.headers).toMatchObject({ 'Idempotency-Key': 'request-001', 'X-CSRF-Token': CSRF })
  })

  it('requests cancellation and validates its response', async () => {
    const response = {
      run_id: RUN_RESPONSE.run_id,
      project_id: RUN_RESPONSE.project_id,
      status: 'RUNNING',
      row_version: 3,
      cancellation: 'REQUESTED',
    }
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() => Promise.resolve(new Response(JSON.stringify(response), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })))
    vi.stubGlobal('fetch', fetchMock)

    await expect(cancelRun(RUN_RESPONSE.run_id, CSRF)).resolves.toEqual(response)
    expect(fetchMock).toHaveBeenCalledWith(
      `${API_BASE}/runs/${RUN_RESPONSE.run_id}/cancel`,
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('creates and lists append-only evaluations', async () => {
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValueOnce(new Response(JSON.stringify(EVALUATION_RESPONSE), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [EVALUATION_RESPONSE] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
    vi.stubGlobal('fetch', fetchMock)

    const created = await createEvaluation(RUN_RESPONSE.project_id, RUN_RESPONSE.run_id, {
      rating: 4,
      verdict: 'partially_accurate',
      comment: 'Needs correction',
      revisions: [{
        pointer: '/field_assessments/0/proposed_value',
        suggested_value: 'after',
        reason: 'Evidence confirmed',
      }],
    }, CSRF)
    const history = await loadEvaluations(RUN_RESPONSE.project_id, RUN_RESPONSE.run_id)

    expect(created.revisions[0]?.original_value).toBe('before')
    expect(history).toEqual([EVALUATION_RESPONSE])
  })

  it('rejects a run response that bypasses the status contract', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({
      ...RUN_RESPONSE,
      status: 'UNKNOWN',
    }), { status: 200 }))))

    await expect(createTaskRun(
      RUN_RESPONSE.project_id,
      {
        skill_version_id: '00000000-0000-4000-8000-000000000203',
        task_key: 'review',
        input: {},
        sources: {},
      },
      'request-002',
      CSRF,
    )).rejects.toThrow('Run response did not match its contract')
  })

  it('loads a project-scoped run detail and validates nested evidence', async () => {
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() => Promise.resolve(new Response(
      JSON.stringify(RUN_DETAIL_RESPONSE),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )))
    vi.stubGlobal('fetch', fetchMock)

    const detail = await loadRunDetail(RUN_RESPONSE.project_id, RUN_RESPONSE.run_id)

    expect(detail.result?.summary).toBe('completed')
    expect(detail.tool_calls[0]?.capability).toBe('issue.read/v1')
    expect(detail.evidence[0]?.evidence_ref).toBe('ev_fixture_001')
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${API_BASE}/projects/${RUN_RESPONSE.project_id}/runs/${RUN_RESPONSE.run_id}/detail`,
    )
  })

  it('rejects a run detail containing an unvalidated evidence shape', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({
      ...RUN_DETAIL_RESPONSE,
      evidence: [{ evidence_ref: 'ev_missing-fields' }],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))))

    await expect(loadRunDetail(RUN_RESPONSE.project_id, RUN_RESPONSE.run_id))
      .rejects.toThrow('Run detail response did not match its contract')
  })

  it('answers a versioned interaction and validates the next segment snapshot', async () => {
    /** Interaction reply が Project route、CSRF、idempotency と version を同時に固定する。 */
    const interactionId = '00000000-0000-4000-8000-000000000120'
    const response = {
      run_id: RUN_RESPONSE.run_id,
      project_id: RUN_RESPONSE.project_id,
      status: 'QUEUED',
      row_version: 5,
      interaction_id: interactionId,
      response_id: '00000000-0000-4000-8000-000000000121',
      run_segment_id: '00000000-0000-4000-8000-000000000122',
      segment_no: 2,
      continuation_mode: 'FORK',
      idempotent_replay: false,
    }
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() => Promise.resolve(
      new Response(JSON.stringify(response), {
        status: 201,
        headers: { 'Content-Type': 'application/json', 'Idempotent-Replay': 'false' },
      }),
    ))
    vi.stubGlobal('fetch', fetchMock)

    await expect(respondToInteraction(
      RUN_RESPONSE.project_id,
      RUN_RESPONSE.run_id,
      interactionId,
      1,
      { selected_option_keys: ['accept'] },
      'interaction-response-001',
      CSRF,
    )).resolves.toEqual(response)

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${API_BASE}/projects/${RUN_RESPONSE.project_id}/runs/${RUN_RESPONSE.run_id}`
        + `/interactions/${interactionId}/responses`,
    )
    const init = fetchMock.mock.calls[0]?.[1]
    expect(init?.method).toBe('POST')
    expect(init?.body).toBe(JSON.stringify({
      interaction_version: 1,
      response: { selected_option_keys: ['accept'] },
    }))
  })

  it('loads paginated project run history', async () => {
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() => Promise.resolve(new Response(
      JSON.stringify(RUN_HISTORY_RESPONSE),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )))
    vi.stubGlobal('fetch', fetchMock)

    const page = await loadRunHistory(RUN_RESPONSE.project_id, 10, 0)

    expect(page.items[0]?.input.ticket_id).toBe('fixture-001')
    expect(page.has_more).toBe(false)
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${API_BASE}/projects/${RUN_RESPONSE.project_id}/runs?limit=10&offset=0`,
    )
  })

  it('parses inline skill files through the backend parser', async () => {
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() => Promise.resolve(new Response(JSON.stringify(SKILL_PARSE_RESPONSE), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })))
    vi.stubGlobal('fetch', fetchMock)

    const result = await parseSkillSource([
      { path: 'SKILL.md', content: '# Ticket Reviewer\n' },
      { path: 'references/rules.md', content: '# Rules\n' },
    ], CSRF)

    expect(result.normalized_package.source.detected_adapter).toBe('directory-skill/v1')
    expect(result.runtime_manifest_draft.tools).toEqual([])
    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(`${API_BASE}/skills/parse`)
    expect(JSON.parse(String(call[1]?.body))).toMatchObject({
      files: [{ path: 'SKILL.md' }, { path: 'references/rules.md' }],
    })
  })

  it('rejects a malformed skill parse response', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({
      ...SKILL_PARSE_RESPONSE,
      runtime_manifest_draft: { tools: [] },
    }), { status: 200 }))))

    await expect(parseSkillSource([
      { path: 'README.md', content: '# Guide\n' },
    ], CSRF)).rejects.toThrow('Skill parse response did not match its contract')
  })

  it('reads a skill parse response without a blueprint as not yet interpreted', async () => {
    // 蓝图は Interpreter の産物であり、導入直後の決定的 draft には存在しない。
    const withoutBlueprint = { ...SKILL_PARSE_RESPONSE, capability_blueprint: null }
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(
      JSON.stringify(withoutBlueprint),
      { status: 200 },
    ))))

    const result = await parseSkillSource([
      { path: 'README.md', content: '# Guide\n' },
    ], CSRF)

    expect(result.capability_blueprint).toBeNull()
  })

  it('rejects a skill parse response whose capability blueprint is malformed', async () => {
    // 「未解釈」と「壊れた蓝图」を同じ null へ畳むと、Interpreter の欠陥が UI 上で無害に見える。
    const brokenBlueprint = { ...SKILL_PARSE_RESPONSE, capability_blueprint: { capabilities: 'no' } }
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(
      JSON.stringify(brokenBlueprint),
      { status: 200 },
    ))))

    await expect(parseSkillSource([
      { path: 'README.md', content: '# Guide\n' },
    ], CSRF)).rejects.toThrow('CapabilityBlueprint did not match its contract')
  })

  it('saves an inline skill import in the organization library', async () => {
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() => Promise.resolve(new Response(JSON.stringify(STORED_SKILL_RESPONSE), {
      status: 201,
      headers: { 'Content-Type': 'application/json' },
    })))
    vi.stubGlobal('fetch', fetchMock)

    const stored = await saveSkillImport([
      { path: 'SKILL.md', content: '# Ticket Reviewer\n' },
    ], CSRF)

    expect(stored.interpretation_status).toBe('PREVIEW_READY')
    expect(stored.preview.runtime_manifest_draft.compatibility.level).toBe('assisted')
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${API_BASE}/skill-imports`,
    )
  })

  it('loads a stored skill interpretation by immutable id', async () => {
    const fetchMock = vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() => Promise.resolve(new Response(JSON.stringify(STORED_SKILL_RESPONSE), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })))
    vi.stubGlobal('fetch', fetchMock)

    const stored = await loadSkillInterpretation(STORED_SKILL_RESPONSE.interpretation_id)

    expect(stored.skill_source_id).toBe(STORED_SKILL_RESPONSE.skill_source_id)
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${API_BASE}/skill-interpretations/${STORED_SKILL_RESPONSE.interpretation_id}`,
    )
  })

  it('subscribes to named SSE events and closes the source', () => {
    class FakeMessageEvent {
      constructor(readonly data: string) {}
    }
    class FakeEventSource {
      static latest: FakeEventSource | null = null
      readonly listeners = new Map<string, (event: Event) => void>()
      closed = false
      onerror: (() => void) | null = null

      constructor(readonly url: string, readonly options: EventSourceInit) {
        FakeEventSource.latest = this
      }

      addEventListener(name: string, listener: EventListener): void {
        this.listeners.set(name, listener)
      }

      close(): void { this.closed = true }
    }
    vi.stubGlobal('MessageEvent', FakeMessageEvent)
    vi.stubGlobal('EventSource', FakeEventSource)
    const received: RunEventRecord[] = []
    const subscription = subscribeRunEvents(RUN_RESPONSE.run_id, 4, (event) => received.push(event), vi.fn())
    const source = FakeEventSource.latest
    expect(source?.url).toBe(`${API_BASE}/runs/${RUN_RESPONSE.run_id}/events?after=4`)
    expect(source?.options.withCredentials).toBe(true)

    source?.listeners.get('run.snapshot')?.(new FakeMessageEvent(JSON.stringify({
      run_id: RUN_RESPONSE.run_id,
      run_attempt_id: null,
      agent_session_id: null,
      sequence: 5,
      event_type: 'RUN_SNAPSHOT',
      occurred_at: '2026-07-01T00:00:01Z',
      payload: { status: 'RUNNING' },
      trace_id: null,
    })) as unknown as Event)

    expect(received).toHaveLength(1)
    expect(received[0]?.sequence).toBe(5)

    source?.listeners.get('text.delta')?.(new FakeMessageEvent(JSON.stringify({
      run_id: RUN_RESPONSE.run_id,
      run_attempt_id: '00000000-0000-4000-8000-000000000030',
      agent_session_id: '00000000-0000-4000-8000-000000000031',
      sequence: 6,
      event_type: 'TEXT_DELTA',
      occurred_at: '2026-07-01T00:00:02Z',
      payload: { text: 'partial' },
      trace_id: null,
    })) as unknown as Event)

    expect(received).toHaveLength(2)
    expect(received[1]?.payload.text).toBe('partial')
    subscription.close()
    expect(source?.closed).toBe(true)
  })
})
