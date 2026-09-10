import type { RunEventRecord } from '../api'

/** Agent conversation に表示する user task の安全な要約。任意 task に依存しない汎用形状。 */
export interface AgentPromptSummary {
  taskTitle: string
  capability: string | null
  input: Record<string, unknown>
  sources: Record<string, string>
}

/** selected_sources の歴史 flat snapshot と現行 {provider} object を表示用 map へ正規化する。 */
export function normalizeSelectedSources(raw: Record<string, unknown>): Record<string, string> {
  const normalized: Record<string, string> = {}
  for (const [key, value] of Object.entries(raw)) {
    if (typeof value === 'string' && value) {
      normalized[key] = value
    } else if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      const provider = (value as Record<string, unknown>).provider
      if (typeof provider === 'string' && provider) normalized[key] = provider
    }
  }
  return normalized
}

/** JSON 結果本文の代わりに会話へ表示する人間可読な要約。 */
export interface StructuredResultDigest {
  topLevelFields: number
  objectFields: number
  arrayFields: number
  arrayItems: number
  scalarFields: number
}

/** 会話 view の一つの完成 message。structured は raw JSON を表示しない。 */
export type AgentCompletedMessage =
  | { sequence: number; kind: 'text'; text: string }
  | { sequence: number; kind: 'structured'; digest: StructuredResultDigest }

/** Persisted complete message と現在だけ存在する realtime delta の表示 projection。 */
export interface AgentStreamView {
  completed: AgentCompletedMessage[]
  partial: string
  /** Streaming 中の text が JSON 結果本文らしい場合は raw を表示しない。 */
  partialKind: 'text' | 'structured'
}

/** RunEvent を重複のない完成 message と現在の partial text へ畳み込む。 */
export function projectAgentStream(events: RunEventRecord[]): AgentStreamView {
  const completed: AgentStreamView['completed'] = []
  let partial = ''
  for (const event of [...events].sort((left, right) => left.sequence - right.sequence)) {
    const text = event.payload.text
    if (typeof text !== 'string' || !text) continue
    if (event.event_type === 'TEXT_DELTA') {
      partial += text
    } else if (event.event_type === 'TEXT_COMPLETED') {
      const digest = structuredResultDigest(text)
      completed.push(
        digest === null
          ? { sequence: event.sequence, kind: 'text', text }
          : { sequence: event.sequence, kind: 'structured', digest },
      )
      // SDK の完成 block が直前 delta 全体を含むため、二重表示せず置き換える。
      partial = ''
    }
  }
  return {
    completed,
    partial,
    // M0 の Agent 出力で fenced block は JSON 結果本文だけなので、fence 開始も streaming 抑止対象にする。
    partialKind: /^\s*(\{|```)/.test(partial) ? 'structured' : 'text',
  }
}

/** JSON object の text を会話用 digest へ変換する。JSON でなければ null を返す。 */
export function structuredResultDigest(text: string): StructuredResultDigest | null {
  let parsed: unknown
  try {
    // Prompt 契約に反して model が ```json fence を付ける場合があるため、表示判定では剥がして解析する。
    parsed = JSON.parse(unwrapCodeFence(text)) as unknown
  } catch {
    return null
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return null
  const values = Object.values(parsed as Record<string, unknown>)
  const arrays = values.filter(Array.isArray)
  return {
    topLevelFields: values.length,
    objectFields: values.filter(isNestedObject).length,
    arrayFields: arrays.length,
    arrayItems: arrays.reduce((total, value) => total + value.length, 0),
    scalarFields: values.filter((value) => !Array.isArray(value) && !isNestedObject(value)).length,
  }
}

/** 全体を一つの Markdown code fence が包む場合だけ中身を取り出す。 */
function unwrapCodeFence(text: string): string {
  const match = /^\s*```[A-Za-z0-9_-]*[ \t]*\r?\n([\s\S]*?)\r?\n?\s*```\s*$/.exec(text)
  return match?.[1] ?? text
}

/** Array/null 以外の object を構造統計上の nested object として数える。 */
function isNestedObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
