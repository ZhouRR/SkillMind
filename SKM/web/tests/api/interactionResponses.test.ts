import { afterEach, describe, expect, it, vi } from 'vitest'

import accepted from '../../../contracts/examples/interaction-response.v1.json'
import detailFixture from '../../../contracts/examples/run-detail.v1.json'
import detailSchema from '../../../contracts/runs/detail/v1.schema.json'
import { API_BASE, loadRunDetail, respondToInteraction, type InteractionAnswerInput, type UserInteractionDetail } from '../../src/api'

const CSRF = 's'.repeat(32)
const KEY = 'original-interaction-answer'
const ACTOR = '00000000-0000-4000-8000-000000000091'
const ANSWER = { text: '  Original answer.  ', selected_option_keys: ['second', 'first'] }

/** HTTP fixture も公開された初回/重放の status と header を明示する。 */
function receipt(body: unknown = accepted, status = 201, header: string | null = 'false'): Response {
  const headers = new Headers({ 'Content-Type': 'application/json' })
  if (header !== null) headers.set('Idempotent-Replay', header)
  return new Response(JSON.stringify(body), { status, headers })
}

/** リトライでも入力の正規化・新規 key の発行をしない API 境界を呼ぶ。 */
function answer(signal?: AbortSignal, payload: InteractionAnswerInput = ANSWER, version = 1) {
  return respondToInteraction(accepted.project_id, accepted.run_id, accepted.interaction_id, version, payload, KEY, CSRF, signal)
}

/** 公開履歴の形だけを与え、過去の payload を新規 request として再解釈しない。 */
function interaction(): UserInteractionDetail {
  return {
    interaction_id: accepted.interaction_id,
    run_segment_id: detailFixture.segments[0]!.run_segment_id,
    agent_session_id: detailFixture.sessions[0]!.agent_session_id,
    interaction_type: 'CHOICE', prompt: { prompt: 'Choose an option.', allow_multiple: true },
    options: [{ key: 'first', label: 'First' }, { key: 'second', label: 'Second' }],
    required: false, expires_at: '2026-07-02T14:00:00Z', status: 'RESPONDED', version: 2,
    continuation_mode: 'REPLACE', checkpoint_checksum: `sha256:${'b'.repeat(64)}`,
    change_proposal_id: null, created_at: '2026-07-02T13:00:00Z',
    response: { response_id: accepted.response_id, actor_id: ACTOR, interaction_version: 1,
      response: ANSWER, created_at: '2026-07-02T13:00:30Z' },
  }
}

afterEach(() => vi.unstubAllGlobals())

describe('ordinary interaction response delivery contract', () => {
  it('sends exact original IDs, version, text, choice order, key and CSRF without caching', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(receipt())
    vi.stubGlobal('fetch', fetcher)
    await expect(answer()).resolves.toEqual(accepted)
    expect(fetcher).toHaveBeenCalledWith(
      `${API_BASE}/projects/${accepted.project_id}/runs/${accepted.run_id}/interactions/${accepted.interaction_id}/responses`,
      expect.objectContaining({ method: 'POST', cache: 'no-store', credentials: 'same-origin',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json',
          'Idempotency-Key': KEY, 'X-CSRF-Token': CSRF },
        body: JSON.stringify({ interaction_version: 1, response: ANSWER }),
      }),
    )
  })

  it.each(['QUEUED', 'RUNNING', 'WAITING_FOR_INPUT', 'SUCCEEDED', 'FAILED', 'CANCELLED'])(
    'accepts original segment replay with current Run state %s', async (status) => {
      const replay = { ...accepted, status, row_version: 19, idempotent_replay: true }
      vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(receipt(replay, 200, 'true')))
      await expect(answer()).resolves.toEqual(replay)
    },
  )

  it('accepts canonical UUID receipts for uppercase request IDs without changing the request body', async () => {
    const record = { ...accepted, project_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      run_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', interaction_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc' }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(receipt(record))
    vi.stubGlobal('fetch', fetcher)
    await expect(respondToInteraction(record.project_id.toUpperCase(), record.run_id.toUpperCase(),
      record.interaction_id.toUpperCase(), 1, ANSWER, KEY, CSRF)).resolves.toEqual(record)
    expect(fetcher.mock.calls[0]![1]!.body).toBe(JSON.stringify({ interaction_version: 1, response: ANSWER }))
  })

  it.each([
    [201, 'true', false], [201, null, false], [201, 'False', false],
    [200, 'false', true], [200, null, true], [200, 'TRUE', true],
    [201, 'false', true], [200, 'true', false], [202, 'false', false], [206, 'true', true],
  ])('rejects mismatched status/header/body (%s, %s, %s) without retry', async (status, header, replay) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(receipt({ ...accepted, idempotent_replay: replay }, status, header))
    vi.stubGlobal('fetch', fetcher)
    await expect(answer()).rejects.toThrow()
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it.each([
    ['run_id', ACTOR], ['project_id', ACTOR], ['interaction_id', ACTOR],
    ['response_id', 'invalid'], ['run_segment_id', 'invalid'], ['status', 'UNKNOWN'],
    ['status', 'RUNNING'], ['row_version', 0], ['row_version', 1.5],
    ['row_version', Number.MAX_SAFE_INTEGER + 1], ['segment_no', 1], ['segment_no', 2.5],
    ['continuation_mode', 'INITIAL'], ['continuation_mode', 'BRANCH'], ['idempotent_replay', 'false'],
  ])('rejects a malformed or cross-context receipt field %s: %s', async (field, value) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(receipt({ ...accepted, [field]: value })))
    await expect(answer()).rejects.toThrow()
  })

  it('requires the entire receipt allowlist, rejecting extra or missing fields', async () => {
    const missing: Record<string, unknown> = { ...accepted }
    delete missing.response_id
    vi.stubGlobal('fetch', vi.fn<typeof fetch>()
      .mockResolvedValueOnce(receipt(missing))
      .mockResolvedValueOnce(receipt({ ...accepted, internal_request: {} })))
    await expect(answer()).rejects.toThrow('contract')
    await expect(answer()).rejects.toThrow('contract')
  })

  it.each([401, 403, 404, 409, 410, 422, 500])('preserves Problem status %s and never retries itself', async (status) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(
      JSON.stringify({ code: 'fixture_problem', detail: 'Do not display raw server details.' }), { status },
    ))
    vi.stubGlobal('fetch', fetcher)
    await expect(answer()).rejects.toMatchObject({ status, code: 'fixture_problem' })
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('keeps missing/non-JSON acceptance unconfirmed', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response('<html>gateway</html>', { status: 201 })))
    await expect(answer()).rejects.toThrow()
    await expect(answer()).rejects.toThrow()
  })

  it.each([0, -1, 1.2, Number.MAX_SAFE_INTEGER + 1])('does not send an invalid original version %s', async (version) => {
    const fetcher = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetcher)
    await expect(answer(undefined, ANSWER, version)).rejects.toThrow('request')
    expect(fetcher).not.toHaveBeenCalled()
  })

  it.each([
    {}, { text: '' }, { text: null }, { selected_option_keys: [] },
    { selected_option_keys: ['same', 'same'] }, { selected_option_keys: ['Uppercase'] },
    { selected_option_keys: ['a'.repeat(129)] }, { selected_option_keys: Array.from({ length: 21 }, (_, i) => `option_${i}`) },
    { text: 'value', extra: true }, { text: 'a'.repeat(10001) },
  ])('does not send invalid answer shape %j', async (payload) => {
    const fetcher = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetcher)
    await expect(answer(undefined, payload as InteractionAnswerInput)).rejects.toThrow('request')
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('preserves Unicode text at the schema code-point limit', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(receipt()))
    await expect(answer(undefined, { text: '😀'.repeat(10000) })).resolves.toEqual(accepted)
  })

  it('rejects an already-aborted request and a transport that resolves after abort', async () => {
    const controller = new AbortController()
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => {
      controller.abort()
      return receipt()
    })
    vi.stubGlobal('fetch', fetcher)
    await expect(answer(controller.signal)).rejects.toThrow()
    await expect(answer(controller.signal)).rejects.toThrow()
    expect(fetcher).toHaveBeenCalledOnce()
  })
})

describe('exact original Run detail reads', () => {
  it.each(detailSchema.required)('rejects a missing required detail field %s', async (field) => {
    const body: Record<string, unknown> = { ...detailFixture }
    delete body[field]
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(receipt(body, 200)))
    await expect(loadRunDetail(detailFixture.project_id, detailFixture.run_id)).rejects.toThrow('contract')
  })

  it.each(['idempotent_replay', 'internal_request'])('rejects an extra top-level detail field %s', async (field) => {
    const body = { ...detailFixture, [field]: false }
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(receipt(body, 200)))
    await expect(loadRunDetail(detailFixture.project_id, detailFixture.run_id)).rejects.toThrow('contract')
  })

  it('reads current authorized facts without claiming they identify a lost POST', async () => {
    const body = { ...detailFixture, interactions: [interaction()] }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(receipt(body, 200, null))
    vi.stubGlobal('fetch', fetcher)
    const value = await loadRunDetail(body.project_id, body.run_id)
    expect(value.interactions[0]!.response).toEqual(interaction().response)
    expect(fetcher).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ cache: 'no-store' }))
  })

  it.each(['project_id', 'run_id'])('rejects a valid detail returned for another %s', async (field) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(receipt({ ...detailFixture, [field]: ACTOR }, 200)))
    await expect(loadRunDetail(detailFixture.project_id, detailFixture.run_id)).rejects.toThrow('scope')
  })

  it('accepts uppercase request scope and canonical detail UUIDs as the same identity', async () => {
    const body = { ...detailFixture, project_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      run_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' }
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(receipt(body, 200)))
    await expect(loadRunDetail(body.project_id.toUpperCase(), body.run_id.toUpperCase())).resolves.toEqual(body)
  })

  it.each([201, 202, 206])('rejects non-detail HTTP success status %s', async (status) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(receipt(detailFixture, status)))
    await expect(loadRunDetail(detailFixture.project_id, detailFixture.run_id)).rejects.toThrow()
  })

  it.each([
    ['interaction_id', 'invalid'], ['run_segment_id', 'invalid'], ['agent_session_id', 'invalid'],
    ['version', 0], ['version', 1.5], ['expires_at', '2026-02-30T00:00:00Z'],
    ['continuation_mode', 'BRANCH'], ['checkpoint_checksum', 'missing'], ['change_proposal_id', 'invalid'],
    ['internal', 'not-public'],
  ])('rejects malformed interaction identity and metadata %s', async (field, value) => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(receipt({
      ...detailFixture, interactions: [{ ...interaction(), [field]: value }],
    }, 200)))
    await expect(loadRunDetail(detailFixture.project_id, detailFixture.run_id)).rejects.toThrow('contract')
  })

  it.each([
    ['response_id', 'invalid'], ['actor_id', 'invalid'], ['interaction_version', 0],
    ['interaction_version', Number.MAX_SAFE_INTEGER + 1], ['created_at', 'yesterday'], ['extra', 'not-public'],
  ])('does not expose an unvalidated prior response field %s', async (field, value) => {
    const item = interaction()
    item.response = { ...item.response!, [field]: value }
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(receipt({ ...detailFixture, interactions: [item] }, 200)))
    await expect(loadRunDetail(detailFixture.project_id, detailFixture.run_id)).rejects.toThrow('contract')
  })

  it('preserves legacy dangling approvals and historical answer payloads as read-only data', async () => {
    const item = interaction()
    item.interaction_type = 'EFFECT_APPROVAL'
    item.response!.response = { historical_field: 'original value' }
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(receipt({ ...detailFixture, interactions: [item] }, 200)))
    await expect(loadRunDetail(detailFixture.project_id, detailFixture.run_id))
      .resolves.toMatchObject({ interactions: [item] })
  })

  it('rejects duplicated identities instead of mounting two owners of the same interaction', async () => {
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(receipt({
      ...detailFixture, interactions: [interaction(), interaction()],
    }, 200)))
    await expect(loadRunDetail(detailFixture.project_id, detailFixture.run_id)).rejects.toThrow('contract')
  })

  it('also rejects duplicated interaction UUIDs written in different case', async () => {
    const item = { ...interaction(), interaction_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' }
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(receipt({ ...detailFixture,
      interactions: [item, { ...item, interaction_id: item.interaction_id.toUpperCase() }],
    }, 200)))
    await expect(loadRunDetail(detailFixture.project_id, detailFixture.run_id)).rejects.toThrow('contract')
  })

  it('does not read after cancellation or deliver a late detail', async () => {
    const controller = new AbortController()
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => {
      controller.abort()
      return receipt(detailFixture, 200)
    })
    vi.stubGlobal('fetch', fetcher)
    await expect(loadRunDetail(detailFixture.project_id, detailFixture.run_id, controller.signal)).rejects.toThrow()
    await expect(loadRunDetail(detailFixture.project_id, detailFixture.run_id, controller.signal)).rejects.toThrow()
    expect(fetcher).toHaveBeenCalledOnce()
  })
})
