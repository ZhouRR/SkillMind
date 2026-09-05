import { describe, expect, it } from 'vitest'

import type { RunEventRecord } from '../../src/api'
import {
  normalizeSelectedSources,
  projectAgentStream,
  structuredResultDigest,
} from '../../src/lib/agentStream'

/** Agent stream test 用の最小 RunEvent を生成する。 */
function event(sequence: number, eventType: string, text: string): RunEventRecord {
  return {
    run_id: '00000000-0000-4000-8000-000000000001',
    run_attempt_id: '00000000-0000-4000-8000-000000000002',
    agent_session_id: '00000000-0000-4000-8000-000000000003',
    sequence,
    event_type: eventType,
    occurred_at: '2026-07-02T13:00:00Z',
    payload: { text },
    trace_id: null,
  }
}

describe('agent stream projection', () => {
  it('concatenates realtime deltas in sequence order', () => {
    /** Redis の到着順が前後しても Agent 出力順を復元できることを守る。 */
    expect(projectAgentStream([
      event(3, 'TEXT_DELTA', 'world'),
      event(2, 'TEXT_DELTA', 'hello '),
    ])).toEqual({ completed: [], partial: 'hello world', partialKind: 'text' })
  })

  it('replaces partial deltas with the persisted completed message', () => {
    /** Refresh 後も完成 text を一度だけ表示し、delta と二重表示しないことを守る。 */
    const view = projectAgentStream([
      event(2, 'TEXT_DELTA', '{"summary"'),
      event(3, 'TEXT_DELTA', ':"ok"}'),
      event(4, 'TEXT_COMPLETED', '{"summary":"ok"}'),
    ])
    expect(view.partial).toBe('')
    expect(view.completed).toHaveLength(1)
    expect(view.completed[0]?.sequence).toBe(4)
  })

  it('keeps natural language output as plain text messages', () => {
    /** JSON でない Agent text は会話へそのまま表示することを守る。 */
    const view = projectAgentStream([event(2, 'TEXT_COMPLETED', '分析を開始します。')])
    expect(view.completed).toEqual([
      { sequence: 2, kind: 'text', text: '分析を開始します。' },
    ])
  })

  it('projects a JSON result body into a structured digest instead of raw JSON', () => {
    /** 構造化結果の全文 JSON を会話へ流し込まないことを守る。 */
    const body = JSON.stringify({
      issue: { id: 'fixture-001', subject: 'ログイン障害', source_ref: 'ev_1' },
      field_assessments: [{}, {}],
      consistency_checks: [{}],
      validity_checks: [{}, {}, {}],
      warnings: ['warning-a'],
      needs_confirmation: true,
    })
    const view = projectAgentStream([event(5, 'TEXT_COMPLETED', body)])
    expect(view.completed).toEqual([
      {
        sequence: 5,
        kind: 'structured',
        digest: {
          topLevelFields: 6,
          objectFields: 1,
          arrayFields: 4,
          arrayItems: 7,
          scalarFields: 1,
        },
      },
    ])
  })

  it('marks a streaming JSON body as structured to suppress raw display', () => {
    /** Streaming 中の JSON 断片を画面へ流し込まないことを守る。 */
    const view = projectAgentStream([event(2, 'TEXT_DELTA', '  {"issue":')])
    expect(view.partialKind).toBe('structured')
    expect(projectAgentStream([event(2, 'TEXT_DELTA', '通常の文章')]).partialKind).toBe('text')
  })

  it('digests a JSON body wrapped in a markdown code fence', () => {
    /** Model が prompt 契約に反して ```json fence を付けても raw dump しないことを守る。 */
    const body = ['```json', '{"issue":{"id":"x","subject":"件名"},"warnings":[]}', '```'].join('\n')
    const view = projectAgentStream([event(4, 'TEXT_COMPLETED', body)])
    expect(view.completed[0]?.kind).toBe('structured')
    expect(projectAgentStream([event(2, 'TEXT_DELTA', '```json\n{"iss')]).partialKind)
      .toBe('structured')
  })

  it('keeps fenced non-JSON content as plain text', () => {
    /** JSON でない fenced block を誤って要約 card に変換しないことを守る。 */
    const body = ['```python', 'print("hello")', '```'].join('\n')
    expect(projectAgentStream([event(4, 'TEXT_COMPLETED', body)]).completed[0]?.kind).toBe('text')
  })
})

describe('structured result digest', () => {
  it('returns null for non-JSON and non-object payloads', () => {
    /** 通常文章や JSON array を誤って digest 化しないことを守る。 */
    expect(structuredResultDigest('plain text')).toBeNull()
    expect(structuredResultDigest('[1,2]')).toBeNull()
    expect(structuredResultDigest('"quoted"')).toBeNull()
  })

  it('degrades gracefully for unknown JSON object shapes', () => {
    /** Schema 外の JSON object でも raw dump せず要約表示へ倒すことを守る。 */
    expect(structuredResultDigest('{"other":1}')).toEqual({
      topLevelFields: 1,
      objectFields: 0,
      arrayFields: 0,
      arrayItems: 0,
      scalarFields: 1,
    })
  })
})

describe('selected sources normalization', () => {
  it('normalizes both the historical flat and current structured source shapes', () => {
    /** 歴史 snapshot と現行 {provider} object のどちらも key→provider に畳む。 */
    expect(normalizeSelectedSources({
      issue_source: 'csv',
      'primary-issues': { capability: 'issue.read/v1', provider: 'redmine' },
    })).toEqual({ issue_source: 'csv', 'primary-issues': 'redmine' })
  })

  it('drops entries without a usable provider string', () => {
    /** provider を持たない値や空文字は表示対象から除外することを守る。 */
    expect(normalizeSelectedSources({
      repository_source: 'none',
      broken: { capability: 'x' },
      empty: '',
    })).toEqual({ repository_source: 'none' })
  })
})
