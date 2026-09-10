import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  cancelRun,
  createEvaluation,
  createTaskRun,
  parseSkillSource,
  saveSkillImport,
} from '../../src/api/index'

const CSRF = 's'.repeat(32)
const RUN = {
  run_id: '00000000-0000-4000-8000-000000000001',
  project_id: '00000000-0000-4000-8000-000000000002',
  task_id: '00000000-0000-4000-8000-000000000003',
  status: 'QUEUED',
  row_version: 1,
  created_at: '2026-07-05T12:00:00Z',
  idempotent_replay: false,
} as const

const PREVIEW = {
  normalized_package: {
    package_format: 'skillmind.normalized/v1',
    source: {
      type: 'directory',
      content_hash: 'sha256:source',
      detected_adapter: 'directory-skill/v1',
      files: [],
    },
    metadata: { name: 'Skill', description: 'Skill preview', argument_hint: null },
    resources: { scripts: [], references: [], assets: [] },
    declared_tools: [],
    diagnostics: [],
  },
  runtime_manifest_draft: {
    identity: {
      skill_key: 'skill',
      source_hash: 'sha256:source',
      interpreter_version: 'deterministic-parser/1.0.0',
    },
    compatibility: { level: 'assisted', confidence: 0.25, diagnostics: [] },
    tools: [],
    extensions: {},
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

afterEach(() => vi.unstubAllGlobals())

describe('authenticated business mutations', () => {
  it('sends CSRF for Skill parse and import', async () => {
    const stored = {
      skill_source_id: '00000000-0000-4000-8000-000000000010',
      interpretation_id: '00000000-0000-4000-8000-000000000011',
      organization_id: '00000000-0000-4000-8000-000000000004',
      name: 'Skill',
      source_hash: 'sha256:source',
      source_type: 'directory',
      interpretation_status: 'PREVIEW_READY',
      compatibility_level: 'assisted',
      confidence: 0.25,
      interpreter_version: 'deterministic-parser/1.0.0',
      created_at: '2026-07-05T12:00:00Z',
      preview: PREVIEW,
    }
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(PREVIEW))
      .mockResolvedValueOnce(jsonResponse(stored, 201))
    vi.stubGlobal('fetch', fetchMock)

    await parseSkillSource([{ path: 'SKILL.md', content: '# Skill' }], CSRF)
    await saveSkillImport([{ path: 'SKILL.md', content: '# Skill' }], CSRF)

    expectCsrf(fetchMock)
  })

  it('sends CSRF and idempotency identity when creating and cancelling Run', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(RUN, 201))
      .mockResolvedValueOnce(jsonResponse({
        run_id: RUN.run_id,
        project_id: RUN.project_id,
        status: 'CANCELLED',
        row_version: 2,
        cancellation: 'CANCELLED',
      }))
    vi.stubGlobal('fetch', fetchMock)

    await createTaskRun(RUN.project_id, {
      skill_version_id: '00000000-0000-4000-8000-000000000203',
      task_key: 'review',
      input: { subject: 'Review source' },
      sources: { repository: 'git' },
    }, 'request-001', CSRF)
    await cancelRun(RUN.run_id, CSRF)

    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('Idempotency-Key'))
      .toBe('request-001')
    expectCsrf(fetchMock)
  })

  it('sends CSRF when appending Evaluation', async () => {
    const evaluation = {
      evaluation_id: '00000000-0000-4000-8000-000000000020',
      result_id: '00000000-0000-4000-8000-000000000021',
      run_id: RUN.run_id,
      user_id: '00000000-0000-4000-8000-000000000022',
      rating: 4,
      verdict: 'accurate',
      comment: 'confirmed',
      revisions: [],
      created_at: '2026-07-05T12:00:00Z',
    }
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(evaluation, 201))
    vi.stubGlobal('fetch', fetchMock)

    await createEvaluation(RUN.project_id, RUN.run_id, {
      rating: 4,
      verdict: 'accurate',
      comment: 'confirmed',
      revisions: [],
    }, CSRF)

    expectCsrf(fetchMock)
  })
})

/** 全 mutation request が同じ memory CSRF token を送ることを確認する。 */
function expectCsrf(fetchMock: ReturnType<typeof vi.fn<typeof fetch>>): void {
  for (const call of fetchMock.mock.calls) {
    expect(new Headers(call[1]?.headers).get('X-CSRF-Token')).toBe(CSRF)
    expect(call[1]?.credentials).toBe('same-origin')
  }
}

/** Fetch test 用 JSON response を生成する。 */
function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}
