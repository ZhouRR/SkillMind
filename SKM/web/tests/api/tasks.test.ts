import { afterEach, describe, expect, it, vi } from 'vitest'

import { API_BASE, createTaskRun, loadProjectTasks } from '../../src/api/index'

const CSRF = 's'.repeat(32)
const PROJECT_ID = '00000000-0000-4000-8000-000000000020'
const SKILL_VERSION_ID = '00000000-0000-4000-8000-000000000203'

/** 汎用 repository-review task を発見契約テストの素材にする。 */
const TASK = {
  skill_id: '00000000-0000-4000-8000-000000000202',
  skill_version_id: SKILL_VERSION_ID,
  skill_key: 'repository-review',
  skill_name: 'Repository Review',
  version: '1.0.0',
  task_key: 'review',
  task_id: '00000000-0000-4000-8000-0000000000aa',
  last_run: null,
  capability: 'generic.repository.review/v1',
  title: 'Generic repository review',
  task_type: 'immediate',
  input_schema: {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    type: 'object',
    properties: { subject: { type: 'string', description: 'Review subject' } },
    required: ['subject'],
    additionalProperties: false,
  },
  output_schema: {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    type: 'object',
    properties: { summary: { type: 'string' } },
    required: ['summary'],
    additionalProperties: false,
  },
  input_schema_checksum: `sha256:${'1'.repeat(64)}`,
  output_schema_checksum: `sha256:${'2'.repeat(64)}`,
  task_output_schema: {
    type: 'object',
    properties: { summary: { type: 'string' } },
    required: ['summary'],
    additionalProperties: false,
  },
  task_output_schema_checksum: `sha256:${'3'.repeat(64)}`,
  workflow: 'review-v1',
  view: 'generic-structured',
  default_view: 'generic-structured',
  compatibility_level: 'adapted',
  tool_requirements: [{ capability: 'issue.read/v1', required: true }],
  published_at: '2026-07-09T10:00:00Z',
  readiness: {
    level: 'CONFIGURATION_REQUIRED',
    requirements: [
      {
        key: 'primary-issues',
        kind: 'issue',
        required: true,
        access: 'read',
        status: 'UNAVAILABLE',
        reason: 'The project has no resource bound for this requirement yet',
        capabilities: ['issue.read/v1'],
        selection_guidance: null,
        candidates: [],
      },
    ],
  },
} as const

/** create_task_run が返す Run 作成 response。 */
const RUN_RESPONSE = {
  run_id: '00000000-0000-4000-8000-000000000900',
  project_id: PROJECT_ID,
  task_id: '00000000-0000-4000-8000-000000000901',
  status: 'QUEUED',
  row_version: 1,
  created_at: '2026-07-09T10:05:00Z',
  idempotent_replay: false,
} as const

/** 成功 JSON body を持つ fetch mock を作る。 */
function jsonFetch(body: unknown, status: number) {
  return vi.fn<(input: string, init?: RequestInit) => Promise<Response>>(() =>
    Promise.resolve(new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })),
  )
}

afterEach(() => vi.unstubAllGlobals())

/** Task 発見と task-run 作成 client が API 契約と解析境界を守ることを検証する。 */
describe('Task discovery and task-run API contract', () => {
  it('loads a project task catalog and parses generic task descriptors', async () => {
    const fetchMock = jsonFetch({ tasks: [TASK] }, 200)
    vi.stubGlobal('fetch', fetchMock)

    const catalog = await loadProjectTasks(PROJECT_ID)

    expect(catalog.tasks).toHaveLength(1)
    const task = catalog.tasks[0]
    if (!task) throw new Error('Expected one task')
    expect(task.capability).toBe('generic.repository.review/v1')
    expect(task.tool_requirements[0]?.required).toBe(true)
    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(`${API_BASE}/projects/${PROJECT_ID}/tasks`)
    expect(call[1]?.method ?? 'GET').toBe('GET')
  })

  it('rejects a task catalog whose descriptor drops a required field', async () => {
    const { capability: _dropped, ...withoutCapability } = TASK
    vi.stubGlobal('fetch', jsonFetch({ tasks: [withoutCapability] }, 200))

    await expect(loadProjectTasks(PROJECT_ID)).rejects.toThrow('Task catalog response did not match')
  })

  it('creates a generic task-run with CSRF, idempotency key and the exact body', async () => {
    const fetchMock = jsonFetch(RUN_RESPONSE, 201)
    vi.stubGlobal('fetch', fetchMock)

    const run = await createTaskRun(
      PROJECT_ID,
      {
        skill_version_id: SKILL_VERSION_ID,
        task_key: 'review',
        input: { subject: 'Review PR 42' },
        sources: { 'primary-issues': 'csv' },
      },
      'idem-key-1',
      CSRF,
    )

    expect(run.run_id).toBe(RUN_RESPONSE.run_id)
    expect(run.status).toBe('QUEUED')
    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(`${API_BASE}/projects/${PROJECT_ID}/task-runs`)
    expect(call[1]?.method).toBe('POST')
    expect(call[1]?.headers).toMatchObject({ 'X-CSRF-Token': CSRF, 'Idempotency-Key': 'idem-key-1' })
    expect(JSON.parse(String(call[1]?.body))).toEqual({
      skill_version_id: SKILL_VERSION_ID,
      task_key: 'review',
      input: { subject: 'Review PR 42' },
      sources: { 'primary-issues': 'csv' },
    })
  })

  it('parses per-requirement readiness so the UI can explain what to configure', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(
      JSON.stringify({ tasks: [TASK] }), { status: 200 },
    ))))

    const catalog = await loadProjectTasks(PROJECT_ID)
    const readiness = catalog.tasks[0]?.readiness

    expect(readiness?.level).toBe('CONFIGURATION_REQUIRED')
    expect(readiness?.requirements[0]?.status).toBe('UNAVAILABLE')
    expect(readiness?.requirements[0]?.reason).toContain('no resource bound')
  })

  it('accepts a task whose readiness is undetermined without assuming it cannot run', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(
      JSON.stringify({ tasks: [{ ...TASK, readiness: null }] }), { status: 200 },
    ))))

    const catalog = await loadProjectTasks(PROJECT_ID)

    expect(catalog.tasks[0]?.readiness).toBeNull()
  })

  it('rejects a readiness payload that omits its per-requirement evidence', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(
      JSON.stringify({ tasks: [{ ...TASK, readiness: { level: 'RUNNABLE' } }] }), { status: 200 },
    ))))

    await expect(loadProjectTasks(PROJECT_ID))
      .rejects.toThrow('Task catalog response did not match its contract')
  })
})
