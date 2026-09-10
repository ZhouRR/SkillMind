import { afterEach, describe, expect, it, vi } from 'vitest'

import { API_BASE, ApiProblemError, saveSkillImport, uploadSkillFiles } from '../../src/api/index'

const CSRF = 'synthetic-import-csrf'
const SOURCE_HASH = `sha256:${'a'.repeat(64)}`
const FILES = [{ path: 'SKILL.md', content: '# 汎用資料\n元の説明を保持する。' }]

/** 未解釈の合法 preview を使い、今回無関係な model pipeline を合成しない。 */
function storedPreview() {
  return {
    skill_source_id: '00000000-0000-4000-8000-000000000010',
    interpretation_id: '00000000-0000-4000-8000-000000000011',
    organization_id: '00000000-0000-4000-8000-000000000012',
    name: '汎用資料',
    source_hash: SOURCE_HASH,
    source_type: 'directory',
    interpretation_status: 'PREVIEW_READY',
    compatibility_level: 'assisted',
    confidence: 0.25,
    interpreter_version: 'deterministic-parser/1.0.0',
    created_at: '2026-09-10T12:00:00Z',
    preview: {
      normalized_package: {
        package_format: 'skillmind.normalized/v1',
        source: { type: 'directory', content_hash: SOURCE_HASH, detected_adapter: 'directory-skill/v1', files: [] },
        metadata: { name: '汎用資料', description: '説明の原文', argument_hint: null },
        resources: { scripts: [], references: [], assets: [] },
        declared_tools: [],
        diagnostics: [],
      },
      runtime_manifest_draft: {
        identity: { skill_key: 'generic-notes', source_hash: SOURCE_HASH, interpreter_version: 'deterministic-parser/1.0.0' },
        compatibility: { level: 'assisted', confidence: 0.25, diagnostics: [] },
        tools: [],
        extensions: {},
      },
      capability_blueprint: null,
    },
  }
}

/** Browser の実 File/FormData を使い、multipart を JSON mock に置き換えない。 */
function upload(signal?: AbortSignal) {
  return uploadSkillFiles([new File([FILES[0]!.content], 'SKILL.md', { type: 'text/markdown' })], CSRF, signal)
}

const IMPORTS = [
  { name: 'inline', path: '/skill-imports', submit: (signal?: AbortSignal) => saveSkillImport(FILES, CSRF, signal) },
  { name: 'multipart', path: '/skill-imports/upload', submit: upload },
] as const

/** 失敗も成功も同じ HTTP consumer を通し、再送がないことを観測する。 */
function jsonResponse(body: unknown, status = 201): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => vi.unstubAllGlobals())

describe.each(IMPORTS)('$name Skill import', ({ name, path, submit }) => {
  it('accepts only the saved 201 receipt and preserves cookie, CSRF, signal and original files', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(storedPreview()))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    const result = await submit(controller.signal)

    expect(result).toEqual(storedPreview())
    expect(result.preview.capability_blueprint).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const call = fetchMock.mock.calls[0]!
    expect(call[0]).toBe(`${API_BASE}${path}`)
    expect(call[1]).toMatchObject({ method: 'POST', credentials: 'same-origin', signal: controller.signal })
    const headers = new Headers(call[1]?.headers)
    expect(headers.get('X-CSRF-Token')).toBe(CSRF)
    expect(headers.get('Accept')).toBe('application/json')
    if (name === 'inline') {
      expect(headers.get('Content-Type')).toBe('application/json')
      expect(JSON.parse(String(call[1]?.body))).toEqual({ files: FILES })
    } else {
      expect(headers.has('Content-Type')).toBe(false)
      expect(call[1]?.body).toBeInstanceOf(FormData)
      const values = (call[1]?.body as FormData).getAll('files')
      expect(values).toHaveLength(1)
      expect(values[0]).toBeInstanceOf(File)
      const file = values[0] as File
      expect(file.name).toBe('SKILL.md')
      expect(await file.text()).toBe(FILES[0]!.content)
    }
  })

  it.each([200, 202, 204, 205, 206])('rejects unexpected success %i without claiming rollback or retrying', async (status) => {
    const body = status === 204 || status === 205 ? null : JSON.stringify(storedPreview())
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { status }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(submit()).rejects.toMatchObject({ name: 'ApiProblemError', status, code: undefined })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each([
    [401, 'authentication_required'],
    [403, 'csrf_rejected'],
    [403, 'administrator_required'],
    [400, 'invalid_file_path'],
    [413, 'skill_upload_too_large'],
    [422, 'invalid_skill_upload'],
    [503, 'skill_storage_unavailable'],
  ] as const)('preserves the stable %i/%s rejection without an automatic retry', async (status, code) => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ status, code, detail: 'Synthetic rejection' }, status))
    vi.stubGlobal('fetch', fetchMock)

    await expect(submit()).rejects.toMatchObject({ name: 'ApiProblemError', status, code })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each(['skill_source_id', 'interpretation_id', 'organization_id', 'source_hash', 'interpreter_version', 'created_at'] as const)(
    'rejects a missing or wrong-type %s in a successful response', async (field) => {
      for (const value of [undefined, null, 42, {}, []]) {
        const body = { ...storedPreview(), [field]: value }
        const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body))
        vi.stubGlobal('fetch', fetchMock)

        await expect(submit()).rejects.toThrow('Stored skill preview response did not match its contract')
        expect(fetchMock).toHaveBeenCalledTimes(1)
      }
    },
  )

  it.each([null, [], {}, { normalized_package: {}, runtime_manifest_draft: {} }])(
    'rejects malformed preview %j instead of constructing a saved receipt', async (preview) => {
      const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ ...storedPreview(), preview }))
      vi.stubGlobal('fetch', fetchMock)

      await expect(submit()).rejects.toThrow('Skill parse response did not match its contract')
      expect(fetchMock).toHaveBeenCalledTimes(1)
    },
  )

  it('keeps damaged declared Blueprint distinct from legitimate null', async () => {
    const body = storedPreview()
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      ...body, preview: { ...body.preview, capability_blueprint: { capabilities: 'invalid' } },
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(submit()).rejects.toThrow('CapabilityBlueprint did not match its contract')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each([null, [], {}, { confidence: '0.25' }])('rejects malformed receipt %j', async (body) => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body))
    vi.stubGlobal('fetch', fetchMock)

    await expect(submit()).rejects.toThrow('Stored skill preview response did not match its contract')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each([201, 503])('keeps non-JSON %i as an unclassified response without retry', async (status) => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response('<html>synthetic proxy response</html>', { status }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(submit()).rejects.toBeInstanceOf(ApiProblemError)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it.each(['abort', 'network'] as const)('propagates %s uncertainty without resending or producing a receipt', async (kind) => {
    const error = kind === 'abort' ? new DOMException('Synthetic cancellation', 'AbortError') : new TypeError('Synthetic disconnect')
    const fetchMock = vi.fn<typeof fetch>().mockRejectedValue(error)
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    await expect(submit(controller.signal)).rejects.toBe(error)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]?.[1]?.signal).toBe(controller.signal)
  })
})
