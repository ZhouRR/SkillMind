import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  API_BASE,
  ApiProblemError,
  adjustInterpretation,
  confirmInterpretationRequest,
  createSkillVersionDraft,
  deleteSkillVersion,
  deprecateSkillVersion,
  disableProjectSkillVersion,
  enableProjectSkillVersion,
  interpretSkillSource,
  listProjectSkillVersions,
  listSkillVersions,
  loadInterpretationExecution,
  publishSkillVersion,
  uploadSkillFiles,
} from '../../src/api/index'

const CSRF = 's'.repeat(32)
const ORGANIZATION_ID = '00000000-0000-4000-8000-000000000002'
const PROJECT_ID = '00000000-0000-4000-8000-000000000020'
const SOURCE_ID = '00000000-0000-4000-8000-000000000040'
const INTERPRETATION_ID = '00000000-0000-4000-8000-000000000050'
const CHILD_INTERPRETATION_ID = '00000000-0000-4000-8000-000000000051'
const REQUEST_ID = '00000000-0000-4000-8000-000000000061'
const REQUEST_RESPONSE = {
  request_id: REQUEST_ID, skill_source_id: SOURCE_ID, status: 'QUEUED' as const,
  execution_key: `sha256:${'a'.repeat(64)}`, interpretation_id: null, error_code: null,
}

// 汎用 repository-review Skill を interpret 契約テストの素材にする。
const PREVIEW = {
  normalized_package: {
    package_format: 'skillmind.normalized/v1',
    source: {
      type: 'directory',
      content_hash: 'sha256:source',
      detected_adapter: 'directory-skill/v1',
      files: [],
    },
    metadata: {
      name: 'Repository Review',
      description: 'Review a repository change with deterministic evidence.',
      argument_hint: null,
    },
    resources: { scripts: [], references: ['references/checklist.md'], assets: [] },
    declared_tools: ['Read', 'Grep'],
    diagnostics: [],
  },
  runtime_manifest_draft: {
    identity: {
      skill_key: 'repository-review',
      source_hash: 'sha256:source',
      interpreter_version: 'skillmind-skill-interpreter/1.0.0',
    },
    compatibility: { level: 'adapted', confidence: 0.72, diagnostics: [] },
    tools: [{ capability: 'repository.read/v1' }],
    extensions: {},
    tasks: [{ task_key: 'repository.review' }],
  },
  capability_blueprint: {
    blueprint_version: 'skillmind.capability-blueprint/v1',
    capabilities: [{ key: 'repository.review', title: 'Repository Review' }],
    tasks: [
      {
        key: 'review-file',
        capability: 'repository.review',
        objective: 'Complete the Repository Review task declared by the Skill.',
      },
    ],
    resource_requirements: [
      {
        key: 'repository-source',
        kind: 'repository',
        required: true,
        access: 'read',
        capability_hints: ['repository.read/v1'],
      },
    ],
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

const REPORT = {
  report_version: 'skillmind.skill-interpretation-report/v1',
  summary: 'Adapted repository review skill with read-only evidence.',
  compatibility_level: 'adapted',
  confidence: {
    capabilities: 0.8,
    tasks: 0.75,
    data_sources: 0.7,
    tools: 0.72,
    workflows: 0.6,
    schemas: 0.65,
  },
  assumptions: [{ key: 'read_only', text: 'Skill only reads repository content.' }],
  questions: [
    { key: 'target_branch', text: 'Which branch is reviewed by default?', required: true },
    { key: 'depth', text: 'Should the review include history?', required: false },
  ],
  diagnostics: [
    {
      severity: 'warning',
      code: 'unmapped_reference',
      message: 'A reference could not be resolved to a capability.',
      path: 'references/checklist.md',
      line: 12,
    },
    {
      severity: 'info',
      code: 'declared_tool_noted',
      message: 'Declared tool recorded without authorization.',
      path: null,
      line: null,
    },
  ],
  source_traces: [
    { target: '/capabilities/0', path: 'SKILL.md', line: 3, reason: 'Repository read capability inferred from checklist.' },
    { target: '/tasks/0', path: 'SKILL.md', line: null, reason: 'Single review task derived from the steps section.' },
  ],
  unmapped_references: [{ path: 'references/legacy.md', reason: 'File is not present in the source index.' }],
} as const

/** PREVIEW_READY な最初の interpretation 実行 response。 */
const INTERPRET_RESPONSE = {
  interpretation_id: INTERPRETATION_ID,
  skill_source_id: SOURCE_ID,
  organization_id: ORGANIZATION_ID,
  status: 'PREVIEW_READY',
  origin: 'model',
  model: 'claude-sonnet-5',
  interpreter_version: 'skillmind-skill-interpreter/1.0.0',
  execution_key: 'sha256:execkey',
  error_code: null,
  compatibility_level: 'adapted',
  confidence: 0.72,
  summary: REPORT.summary,
  created_at: '2026-07-08T10:00:00Z',
  preview: PREVIEW,
  report: REPORT,
  reused: false,
  parent_interpretation_id: null,
  adjustment: null,
  diff: { has_changes: false },
} as const

/** 親を持つ reinterpretation（追加調整）の実行 response。 */
const ADJUST_RESPONSE = {
  ...INTERPRET_RESPONSE,
  interpretation_id: CHILD_INTERPRETATION_ID,
  execution_key: 'sha256:execkey-child',
  parent_interpretation_id: INTERPRETATION_ID,
  adjustment: { instruction: 'Only review a single file and stress unresolved risk.' },
  diff: {
    has_changes: true,
    tasks: { added: [], removed: [], changed: ['repository.review'] },
    compatibility_level: { from: 'adapted', to: 'adapted' },
    confidence: { changed: { tasks: { from: 0.75, to: 0.8 } } },
  },
} as const

/** Upload import が返す保存済み preview response。 */
const STORED = {
  skill_source_id: SOURCE_ID,
  interpretation_id: INTERPRETATION_ID,
  organization_id: ORGANIZATION_ID,
  name: 'Repository Review',
  source_hash: 'sha256:source',
  source_type: 'directory',
  interpretation_status: 'PREVIEW_READY',
  compatibility_level: 'assisted',
  confidence: 0.25,
  interpreter_version: 'deterministic-parser/1.0.0',
  created_at: '2026-07-10T00:00:00Z',
  preview: PREVIEW,
} as const

/** Organization library の PUBLISHED frozen version response。 */
const VERSION = {
  skill_id: '00000000-0000-4000-8000-000000000060',
  skill_version_id: '00000000-0000-4000-8000-000000000061',
  skill_source_id: SOURCE_ID,
  interpretation_id: INTERPRETATION_ID,
  organization_id: ORGANIZATION_ID,
  skill_key: 'repository-review',
  name: 'Repository Review',
  description: 'リポジトリ変更を読み取り専用で分析する。',
  version: '1.0.0',
  status: 'PUBLISHED',
  manifest_checksum: `sha256:${'a'.repeat(64)}`,
  manifest: {},
  gate_passed: true,
  gate_findings: [],
  interpretation_diff: {},
  created_at: '2026-07-10T00:00:00Z',
  published_by: '00000000-0000-4000-8000-000000000070',
  published_at: '2026-07-10T01:00:00Z',
} as const

/** Project が VERSION を active にした enablement response。 */
const ENABLEMENT = {
  project_id: PROJECT_ID,
  organization_id: ORGANIZATION_ID,
  skill_version: VERSION,
  enabled_by: '00000000-0000-4000-8000-000000000070',
  enabled_at: '2026-07-10T02:00:00Z',
  disabled_at: null,
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

/** Interpret/adjust/execution client が API 契約と解析境界を守ることを検証する。 */
describe('Skill interpretation API contract', () => {
  it('queues a worker interpret job and returns the launch envelope', async () => {
    const fetchMock = jsonFetch(
      REQUEST_RESPONSE,
      200,
    )
    vi.stubGlobal('fetch', fetchMock)

    const launch = await interpretSkillSource(SOURCE_ID, REQUEST_ID, CSRF)

    // API は原要求の状態だけを返し、本文は結果の read API が返す。
    expect(launch.status).toBe('QUEUED')
    expect(launch.interpretation_id).toBeNull()
    expect(launch.request_id).toBe(REQUEST_ID)
    expect(launch.execution_key).toMatch(/^sha256:/)
    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(`${API_BASE}/skill-sources/${SOURCE_ID}/interpretation-requests`)
    expect(call[1]?.method).toBe('POST')
    expect(call[1]?.headers).toMatchObject({ 'X-CSRF-Token': CSRF })
  })

  it('accepts a terminal request with a result pointer', async () => {
    vi.stubGlobal('fetch', jsonFetch(
      { ...REQUEST_RESPONSE, status: 'SUCCEEDED', interpretation_id: CHILD_INTERPRETATION_ID },
      200,
    ))

    const launch = await interpretSkillSource(SOURCE_ID, REQUEST_ID, CSRF)

    expect(launch.status).toBe('SUCCEEDED')
    expect(launch.interpretation_id).toBe(CHILD_INTERPRETATION_ID)
  })

  it('binds explicit regeneration to the original request in the body', async () => {
    const fetchMock = jsonFetch(
      { ...REQUEST_RESPONSE, execution_key: `sha256:${'b'.repeat(64)}` },
      200,
    )
    vi.stubGlobal('fetch', fetchMock)

    await interpretSkillSource(SOURCE_ID, REQUEST_ID, CSRF, undefined, true)

    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(
      `${API_BASE}/skill-sources/${SOURCE_ID}/interpretation-requests`,
    )
    expect(JSON.parse(String(call[1]?.body))).toEqual({ request_id: REQUEST_ID, force_regenerate: true })
  })

  it('uploads directory files as multipart with relative-path filenames and CSRF', async () => {
    const fetchMock = jsonFetch(STORED, 201)
    vi.stubGlobal('fetch', fetchMock)

    const files = [
      new File([new Uint8Array([0x89, 0x50, 0x00])], 'assets/logo.png', { type: 'image/png' }),
      new File(['# API Skill\n'], 'SKILL.md', { type: 'text/markdown' }),
    ]
    const stored = await uploadSkillFiles(files, CSRF)

    expect(stored.organization_id).toBe(ORGANIZATION_ID)
    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(`${API_BASE}/skill-imports/upload`)
    expect(call[1]?.method).toBe('POST')
    expect(call[1]?.headers).toMatchObject({ 'X-CSRF-Token': CSRF })
    // Content-Type は設定しない。undici/browser が multipart boundary を付与する。
    expect(call[1]?.headers).not.toHaveProperty('Content-Type')
    const body = call[1]?.body as FormData
    const entries = body.getAll('files') as File[]
    expect(entries.map((file) => file.name)).toEqual(['assets/logo.png', 'SKILL.md'])
  })

  it('removes the selected directory name from multipart filenames', async () => {
    const fetchMock = jsonFetch(STORED, 201)
    vi.stubGlobal('fetch', fetchMock)
    const skill = new File(['# Skill\n'], 'SKILL.md', { type: 'text/markdown' })
    const rules = new File(['# Rules\n'], 'rules.md', { type: 'text/markdown' })
    Object.defineProperty(skill, 'webkitRelativePath', { value: 'repository-review/SKILL.md' })
    Object.defineProperty(rules, 'webkitRelativePath', { value: 'repository-review/references/rules.md' })

    await uploadSkillFiles([skill, rules], CSRF)

    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    const entries = (call[1]?.body as FormData).getAll('files') as File[]
    expect(entries.map((file) => file.name)).toEqual(['SKILL.md', 'references/rules.md'])
  })

  it('queues an append-only adjustment job with the instruction and CSRF', async () => {
    const fetchMock = jsonFetch(
      { ...REQUEST_RESPONSE, execution_key: `sha256:${'b'.repeat(64)}` },
      200,
    )
    vi.stubGlobal('fetch', fetchMock)

    const launch = await adjustInterpretation(
      INTERPRETATION_ID,
      'Only review a single file and stress unresolved risk.',
      REQUEST_ID, CSRF,
    )

    expect(launch.status).toBe('QUEUED')
    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(
      `${API_BASE}/skill-interpretations/${INTERPRETATION_ID}/adjustment-requests`,
    )
    expect(call[1]?.method).toBe('POST')
    expect(JSON.parse(String(call[1]?.body))).toEqual({
      request_id: REQUEST_ID, instruction: 'Only review a single file and stress unresolved risk.',
    })
  })

  it('loads an interpretation execution with its parent diff', async () => {
    const fetchMock = jsonFetch(ADJUST_RESPONSE, 200)
    vi.stubGlobal('fetch', fetchMock)

    const execution = await loadInterpretationExecution(CHILD_INTERPRETATION_ID)

    expect(execution.parent_interpretation_id).toBe(INTERPRETATION_ID)
    expect(execution.diff.has_changes).toBe(true)
    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(
      `${API_BASE}/skill-interpretations/${CHILD_INTERPRETATION_ID}/execution`,
    )
    expect(call[1]?.method ?? 'GET').toBe('GET')
  })

  it('fills optional interpretation report fields with UI defaults', async () => {
    const minimalReport = {
      report_version: REPORT.report_version,
      summary: REPORT.summary,
      compatibility_level: REPORT.compatibility_level,
      diagnostics: [],
      source_traces: REPORT.source_traces,
    }
    vi.stubGlobal('fetch', jsonFetch({ ...INTERPRET_RESPONSE, report: minimalReport }, 200))

    const execution = await loadInterpretationExecution(INTERPRETATION_ID)

    expect(execution.report?.confidence).toEqual({})
    expect(execution.report?.assumptions).toEqual([])
    expect(execution.report?.questions).toEqual([])
    expect(execution.report?.unmapped_references).toEqual([])
  })

  it('parses a failed interpretation with a null report without fabricating a manifest', async () => {
    // 失敗した確定 execution は execution endpoint(GET)で取得される。
    const failed = {
      ...INTERPRET_RESPONSE,
      status: 'FAILED',
      error_code: 'provider_timeout',
      confidence: 0,
      report: null,
    }
    vi.stubGlobal('fetch', jsonFetch(failed, 200))

    const execution = await loadInterpretationExecution(CHILD_INTERPRETATION_ID)

    expect(execution.status).toBe('FAILED')
    expect(execution.error_code).toBe('provider_timeout')
    expect(execution.report).toBeNull()
    expect(execution).not.toHaveProperty('validation_attempts')
  })

  it.each([
    { label: 'an empty list', attempts: [] },
    { label: 'two ordered attempts', attempts: ['path=/report validator=required', 'path=/tasks validator=type'] },
    { label: '4096 Unicode characters', attempts: ['🧪'.repeat(4096)] },
  ])('preserves validation attempts containing $label', async ({ attempts }) => {
    vi.stubGlobal('fetch', jsonFetch({ ...INTERPRET_RESPONSE, validation_attempts: attempts }, 200))

    const execution = await loadInterpretationExecution(INTERPRETATION_ID)

    expect(execution.validation_attempts).toEqual(attempts)
  })

  it.each([
    { label: 'null', attempts: null },
    { label: 'an object', attempts: {} },
    { label: 'a string', attempts: 'path=/report validator=required' },
    { label: 'mixed item types', attempts: ['path=/report validator=required', 1] },
    { label: 'a null item', attempts: [null] },
    { label: 'an empty item', attempts: [''] },
    { label: 'a whitespace-only item', attempts: ['　 '] },
    { label: 'three attempts', attempts: ['first', 'second', 'third'] },
    { label: '4097 ASCII characters', attempts: ['a'.repeat(4097)] },
    { label: '4097 Unicode characters', attempts: ['🧪'.repeat(4097)] },
    ...['\u0000', '\t', '\n', '\r', '\u001f', '\u007f'].map((control) => ({
      label: `control character ${control.codePointAt(0)}`,
      attempts: [`path=/report${control}validator=required`],
    })),
  ])('rejects validation attempts containing $label', async ({ attempts }) => {
    vi.stubGlobal('fetch', jsonFetch({ ...INTERPRET_RESPONSE, validation_attempts: attempts }, 200))

    await expect(loadInterpretationExecution(INTERPRETATION_ID))
      .rejects.toThrow('Interpretation execution response did not match its contract')
  })

  it('rejects an execution response that drops the diff contract', async () => {
    const { diff: _diff, ...withoutDiff } = INTERPRET_RESPONSE
    vi.stubGlobal('fetch', jsonFetch(withoutDiff, 200))

    await expect(loadInterpretationExecution(CHILD_INTERPRETATION_ID))
      .rejects.toThrow('Interpretation execution response did not match its contract')
  })

  it('rejects an execution whose report violates its contract', async () => {
    const badReport = { ...INTERPRET_RESPONSE, report: { ...REPORT, confidence: [] } }
    vi.stubGlobal('fetch', jsonFetch(badReport, 200))

    await expect(loadInterpretationExecution(CHILD_INTERPRETATION_ID))
      .rejects.toThrow('Interpretation report did not match its contract')
  })

  it('rejects a launch envelope with an unknown status', async () => {
    vi.stubGlobal('fetch', jsonFetch(
      { ...REQUEST_RESPONSE, status: 'QUEUING' },
      200,
    ))

    await expect(interpretSkillSource(SOURCE_ID, REQUEST_ID, CSRF))
      .rejects.toThrow('Interpretation request response did not match its contract')
  })

  it('checks an unknown original request with GET and rejects a different identity', async () => {
    const fetchMock = jsonFetch({ ...REQUEST_RESPONSE, status: 'UNKNOWN' }, 200)
    vi.stubGlobal('fetch', fetchMock)
    expect((await confirmInterpretationRequest(REQUEST_ID)).status).toBe('UNKNOWN')
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${API_BASE}/skill-interpretation-requests/${REQUEST_ID}`)
    expect(fetchMock.mock.calls[0]?.[1]?.method).not.toBe('POST')
    vi.stubGlobal('fetch', jsonFetch({ ...REQUEST_RESPONSE, request_id: SOURCE_ID }, 200))
    await expect(confirmInterpretationRequest(REQUEST_ID)).rejects.toThrow('Interpretation request did not match')
  })

  it('surfaces an unavailable interpreter as a typed 503 problem', async () => {
    vi.stubGlobal('fetch', jsonFetch(
      { detail: 'Skill interpreter is not configured', code: 'skill_interpreter_unavailable' },
      503,
    ))

    const error = await interpretSkillSource(SOURCE_ID, REQUEST_ID, CSRF).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiProblemError)
    expect((error as ApiProblemError).status).toBe(503)
    expect((error as ApiProblemError).code).toBe('skill_interpreter_unavailable')
  })
})

/** Organization library と Project 明示有効化 API の client 契約を検証する。 */
describe('Skill library scope API contract', () => {
  // 全版管理入口が同じ HTTP/Problem 境界を守り、資格拒否から暗黙再送しないことを調べる。
  const mutations = {
    draft: () => createSkillVersionDraft(INTERPRETATION_ID, CSRF),
    publish: () => publishSkillVersion(VERSION.skill_version_id, [], CSRF),
    deprecate: () => deprecateSkillVersion(VERSION.skill_version_id, CSRF),
    delete: () => deleteSkillVersion(VERSION.skill_version_id, CSRF),
    enable: () => enableProjectSkillVersion(PROJECT_ID, VERSION.skill_version_id, CSRF),
    disable: () => disableProjectSkillVersion(PROJECT_ID, VERSION.skill_version_id, CSRF),
  }

  it.each(['draft', 'publish'] as const)(
    '%s sends the original CSRF with a same-origin cookie and no caller-supplied actor',
    async (operation) => {
      const fetchMock = jsonFetch(VERSION, operation === 'draft' ? 201 : 200)
      vi.stubGlobal('fetch', fetchMock)
      const controller = new AbortController()

      const stored = operation === 'draft'
        ? await createSkillVersionDraft(INTERPRETATION_ID, CSRF, controller.signal)
        : await publishSkillVersion(VERSION.skill_version_id, ['review_required'], CSRF, controller.signal)

      expect(stored.skill_version_id).toBe(VERSION.skill_version_id)
      expect(fetchMock).toHaveBeenCalledTimes(1)
      const call = fetchMock.mock.calls[0]
      if (!call) throw new Error('Expected one fetch call')
      expect(call[0]).toBe(operation === 'draft'
        ? `${API_BASE}/skill-interpretations/${INTERPRETATION_ID}/draft`
        : `${API_BASE}/skill-versions/${VERSION.skill_version_id}/publish`)
      expect(call[1]).toMatchObject({ method: 'POST', credentials: 'same-origin', signal: controller.signal })
      expect(call[1]?.headers).toMatchObject({ 'X-CSRF-Token': CSRF })
      expect(call[1]?.body).toBe(operation === 'draft'
        ? undefined : JSON.stringify({ accepted_warnings: ['review_required'] }))
    },
  )

  describe.each(['draft', 'publish', 'deprecate', 'delete', 'enable', 'disable'] as const)(
    '%s business authorization', (operation) => {
      it.each([
        [401, 'authentication_required'],
        [403, 'csrf_rejected'],
        [403, 'administrator_required'],
      ] as const)('preserves %s %s without retrying the mutation', async (status, code) => {
        const detail = 'Synthetic business authorization rejection'
        const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response(
          JSON.stringify({ status, code, detail }),
          { status, headers: { 'Content-Type': 'application/problem+json' } },
        ))
        vi.stubGlobal('fetch', fetchMock)

        const request = mutations[operation]()
        await expect(request).rejects.toMatchObject({ name: 'ApiProblemError', status, code, message: detail })
        // 新しい資格を取得して原操作を暗黙再送しない。再開は画面の明示判断に委ねる。
        expect(fetchMock).toHaveBeenCalledTimes(1)
      })
    },
  )

  it.each([
    ['enable', 404, 'project_not_found'],
    ['disable', 404, 'project_not_found'],
    ['enable', 409, 'project_archived'],
    ['disable', 409, 'project_archived'],
    ['enable', 409, 'project_skill_version_rejected'],
    ['disable', 409, 'project_skill_version_rejected'],
    ['delete', 409, 'skill_version_delete_blocked'],
    ['delete', 404, 'skill_version_not_found'],
  ] as const)('%s retains %s %s without switching the target or retrying', async (operation, status, code) => {
    const fetchMock = jsonFetch({ status, code, detail: 'Synthetic lifecycle rejection' }, status)
    vi.stubGlobal('fetch', fetchMock)

    await expect(mutations[operation]()).rejects.toMatchObject({ name: 'ApiProblemError', status, code })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('deletes an exact organization version with an empty receipt and the original credentials', async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    await expect(deleteSkillVersion(VERSION.skill_version_id, CSRF, controller.signal)).resolves.toBeUndefined()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${API_BASE}/skill-versions/${VERSION.skill_version_id}`)
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      method: 'DELETE', credentials: 'same-origin', signal: controller.signal,
      headers: { 'X-CSRF-Token': CSRF },
    })
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBeUndefined()
  })

  it('loads organization versions without requiring a project', async () => {
    const fetchMock = jsonFetch({ skill_versions: [VERSION] }, 200)
    vi.stubGlobal('fetch', fetchMock)

    const versions = await listSkillVersions()

    expect(versions[0]?.organization_id).toBe(ORGANIZATION_ID)
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${API_BASE}/skill-versions`)
  })

  it('loads project enablement history only from the project-scoped endpoint', async () => {
    const fetchMock = jsonFetch({ skill_versions: [ENABLEMENT] }, 200)
    vi.stubGlobal('fetch', fetchMock)

    const enablements = await listProjectSkillVersions(PROJECT_ID, true)

    expect(enablements[0]?.skill_version.skill_version_id).toBe(VERSION.skill_version_id)
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `${API_BASE}/projects/${PROJECT_ID}/skill-versions?include_disabled=true`,
    )
  })

  it.each([
    ['PUT', enableProjectSkillVersion],
    ['DELETE', disableProjectSkillVersion],
  ] as const)('%s mutates the exact project/version relationship with CSRF', async (method, mutate) => {
    const fetchMock = jsonFetch(ENABLEMENT, 200)
    vi.stubGlobal('fetch', fetchMock)

    const controller = new AbortController()
    const stored = await mutate(PROJECT_ID, VERSION.skill_version_id, CSRF, controller.signal)

    expect(stored.project_id).toBe(PROJECT_ID)
    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(`${API_BASE}/projects/${PROJECT_ID}/skill-versions/${VERSION.skill_version_id}`)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(call[1]).toMatchObject({ method, credentials: 'same-origin', signal: controller.signal })
    expect(call[1]?.headers).toMatchObject({ 'X-CSRF-Token': CSRF })
    expect(call[1]?.body).toBeUndefined()
  })

  it('deprecates a version at organization scope', async () => {
    const deprecated = { ...VERSION, status: 'DEPRECATED' }
    const fetchMock = jsonFetch(deprecated, 200)
    vi.stubGlobal('fetch', fetchMock)

    const controller = new AbortController()
    const stored = await deprecateSkillVersion(VERSION.skill_version_id, CSRF, controller.signal)

    expect(stored.status).toBe('DEPRECATED')
    const call = fetchMock.mock.calls[0]
    if (!call) throw new Error('Expected one fetch call')
    expect(call[0]).toBe(`${API_BASE}/skill-versions/${VERSION.skill_version_id}/deprecate`)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(call[1]).toMatchObject({ method: 'POST', credentials: 'same-origin', signal: controller.signal })
    expect(call[1]?.headers).toMatchObject({ 'X-CSRF-Token': CSRF })
    expect(call[1]?.body).toBeUndefined()
  })
})

/** 新しい前置条件を暗黙に省略せず、公開 preview の境界で型を検証する。 */
describe('document effect prerequisites', () => {
  it.each([null, [], ['register-run', 'register-run'], [12], 'register-run'])(
    'rejects malformed declared prerequisites %j', async (value) => {
      const response = structuredClone(INTERPRET_RESPONSE)
      Object.assign(response.preview.capability_blueprint.tasks[0]!, { document_prerequisites: value })
      vi.stubGlobal('fetch', jsonFetch(response, 200))
      await expect(loadInterpretationExecution(INTERPRETATION_ID)).rejects.toThrow()
    },
  )

  it('keeps a valid original prerequisite in the reviewable preview', async () => {
    const response = structuredClone(INTERPRET_RESPONSE)
    Object.assign(response.preview.capability_blueprint.tasks[0]!, {
      document_prerequisites: ['register-run'],
    })
    vi.stubGlobal('fetch', jsonFetch(response, 200))
    const execution = await loadInterpretationExecution(INTERPRETATION_ID)
    expect(execution.preview.capability_blueprint?.tasks[0]?.document_prerequisites).toEqual(['register-run'])
  })
})
