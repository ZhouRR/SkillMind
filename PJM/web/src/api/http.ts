/** Build 時の context path 配下に固定する API base path。 */
export const API_BASE = `${import.meta.env.BASE_URL}api/v1` as const

/** Problem Details の status/code を画面 state へ伝える API error。 */
export class ApiProblemError extends Error {
  /** HTTP 境界で公開された安定情報だけを保持する。 */
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
    readonly retryAfterSeconds?: number,
  ) {
    super(message)
    this.name = 'ApiProblemError'
  }
}

/** JSON body と合わせて確認する、消費済み response の公開 HTTP metadata。 */
export interface ApiResponseMetadata {
  status: number
  headers: Headers
}

/** 資源固有の status/header/body 整合性も、共通 HTTP 境界内で検証する。 */
export interface ApiJsonResponseContract {
  statuses: readonly number[]
  validate: (value: unknown, metadata: ApiResponseMetadata) => void
}

/** Cookie を含む JSON API request を実行し、Problem Details を型付き error に変換する。 */
export async function requestApiJson(
  url: string, init: RequestInit = {}, expectedStatus?: number | ApiJsonResponseContract,
): Promise<unknown> {
  const response = await fetch(url, {
    ...init,
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...init.headers },
  })
  if (!response.ok) return throwProblemFromBody(response)
  if (expectedStatus !== undefined && !(typeof expectedStatus === 'number'
    ? response.status === expectedStatus : expectedStatus.statuses.includes(response.status))) {
    throw new ApiProblemError('API returned an unexpected success status', response.status)
  }
  let value: unknown
  try {
    value = await response.json()
  } catch {
    // Gateway error page など非 JSON body は HTTP status を安定 message として返す。
    throw new ApiProblemError('API returned a non-JSON response', response.status)
  }
  // 契約違反を JSON parse 失敗へ変換せず、呼出元の未知結果分類へ引き渡す。
  if (typeof expectedStatus === 'object') expectedStatus.validate(value, response)
  return value
}

/** Body なし API を実行し、指定された場合は受理 202 と完了 204 も区別する。 */
export async function requestApiEmpty(url: string, init: RequestInit, expectedStatus?: number): Promise<void> {
  const response = await fetch(url, {
    ...init,
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...init.headers },
  })
  if (response.ok) {
    if (expectedStatus !== undefined && response.status !== expectedStatus) {
      throw new ApiProblemError('API returned an unexpected success status', response.status)
    }
    return
  }
  return throwProblemFromBody(response)
}

/** 文書 preview など、成功 status と実 byte 上限が固定された text 契約。 */
export interface ApiTextResponseContract {
  status: number
  maxBytes: number
}

/** Raw text も共通 HTTP 境界で扱い、有界 consumer は error body も全量読みしない。 */
export async function requestApiText(
  url: string, init: RequestInit = {}, contract?: ApiTextResponseContract,
): Promise<string> {
  if (contract && (!Number.isSafeInteger(contract.maxBytes) || contract.maxBytes < 0)) {
    throw new TypeError('Text response byte limit must be a nonnegative safe integer')
  }
  init.signal?.throwIfAborted()
  const response = await fetch(url, { ...init, credentials: 'same-origin' })
  if (contract && (response.status === 401 || response.status === 403)) {
    // 資格拒否は headers だけで確定する。遅い本文を待ち、期限で status を失ってはならない。
    void response.body?.cancel().catch(() => undefined)
    throw new ApiProblemError(`API returned ${response.status}`, response.status, undefined,
      parseRetryAfterSeconds(response.headers.get('Retry-After')))
  }
  if (!response.ok) return throwProblemFromBody(response, contract?.maxBytes, init.signal)
  if (contract && response.status !== contract.status) {
    void response.body?.cancel().catch(() => undefined)
    throw new ApiProblemError('API returned an unexpected success status', response.status)
  }
  return contract ? readBoundedText(response, contract.maxBytes, init.signal) : response.text()
}

/** Content-Length は早期拒否だけに使い、展開後の stream を数えて超過時に破棄する。 */
async function readBoundedText(response: Response, maxBytes: number, signal?: AbortSignal | null): Promise<string> {
  const reader = response.body?.getReader()
  const cancel = (): void => { void reader?.cancel().catch(() => undefined) }
  signal?.addEventListener('abort', cancel, { once: true })
  try {
    signal?.throwIfAborted()
    const declared = response.headers.get('Content-Length')
    if (declared !== null && /^\d+$/.test(declared) && Number(declared) > maxBytes) {
      throw new ApiProblemError('Text response exceeds its byte limit', response.status, 'response_too_large')
    }
    const decoder = new TextDecoder('utf-8', { fatal: true })
    const parts: string[] = []
    let size = 0
    while (reader) {
      const { done, value } = await reader.read()
      signal?.throwIfAborted()
      if (done) break
      size += value.byteLength
      if (size > maxBytes) {
        throw new ApiProblemError('Text response exceeds its byte limit', response.status, 'response_too_large')
      }
      parts.push(decoder.decode(value, { stream: true }))
    }
    parts.push(decoder.decode())
    return parts.join('')
  } finally {
    signal?.removeEventListener('abort', cancel)
    // cancel の完了待ちで UI の期限や元の失敗を上書きしない。
    cancel()
    reader?.releaseLock()
  }
}

/** 失敗 response の body を一度だけ解析し、常に型付き ApiProblemError を送出する。 */
async function throwProblemFromBody(response: Response, maxBytes?: number, signal?: AbortSignal | null): Promise<never> {
  const retryAfterSeconds = parseRetryAfterSeconds(response.headers.get('Retry-After'))
  let value: unknown
  try {
    value = maxBytes === undefined ? await response.json()
      : JSON.parse(await readBoundedText(response, maxBytes, signal)) as unknown
  } catch {
    signal?.throwIfAborted()
    throw new ApiProblemError(`API returned ${response.status}`, response.status, undefined, retryAfterSeconds)
  }
  throw problemFromResponse(response.status, value, retryAfterSeconds)
}

/** 秒形式だけを損失なく読み、日時・不正値を推測した待機時間へ置き換えない。 */
function parseRetryAfterSeconds(value: string | null): number | undefined {
  if (value === null || !/^\d+$/.test(value)) return undefined
  const seconds = Number(value)
  return Number.isSafeInteger(seconds) ? seconds : undefined
}

/** Unknown Problem body から存在情報を追加しない client error を生成する。 */
function problemFromResponse(status: number, value: unknown, retryAfterSeconds?: number): ApiProblemError {
  const detail = isRecord(value) && typeof value.detail === 'string'
    ? value.detail
    : `API returned ${status}`
  const code = isRecord(value) && typeof value.code === 'string' ? value.code : undefined
  return new ApiProblemError(detail, status, code, retryAfterSeconds)
}

/** JSON object かどうかを prototype に依存せず判定する。 */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** required と追加 field 拒否を一つの判定に揃える。 */
export function exactFields(value: Record<string, unknown>, fields: readonly string[]): boolean {
  return Object.keys(value).length === fields.length && fields.every((field) => Object.hasOwn(value, field))
}

/** Unknown JSON が string array であることを検証する。 */
export function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
}

/** List response の配列 field を型 guard で全件検証し、契約違反は資源別 message で拒否する。 */
export function parseItemList<T>(
  value: unknown,
  key: string,
  guard: (item: unknown) => item is T,
  message: string,
): T[] {
  const items: unknown = isRecord(value) ? value[key] : null
  if (!Array.isArray(items) || !items.every((item: unknown) => guard(item))) {
    throw new Error(message)
  }
  return items as T[]
}

/** Object の指定 key がすべて string であることを検証する。 */
export function hasStrings(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return keys.every((key) => typeof value[key] === 'string')
}
