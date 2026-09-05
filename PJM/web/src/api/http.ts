/** Build 時の context path 配下に固定する API base path。 */
export const API_BASE = `${import.meta.env.BASE_URL}api/v1` as const

/** Problem Details の status/code を画面 state へ伝える API error。 */
export class ApiProblemError extends Error {
  /** HTTP 境界で公開された安定情報だけを保持する。 */
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message)
    this.name = 'ApiProblemError'
  }
}

/** Cookie を含む JSON API request を実行し、Problem Details を型付き error に変換する。 */
export async function requestApiJson(url: string, init: RequestInit = {}): Promise<unknown> {
  const response = await fetch(url, {
    ...init,
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...init.headers },
  })
  if (!response.ok) return throwProblemFromBody(response)
  try {
    return (await response.json()) as unknown
  } catch {
    // Gateway error page など非 JSON body は HTTP status を安定 message として返す。
    throw new ApiProblemError('API returned a non-JSON response', response.status)
  }
}

/** Body を返さない API request を実行し、失敗時だけ Problem Details を解析する。 */
export async function requestApiEmpty(url: string, init: RequestInit): Promise<void> {
  const response = await fetch(url, {
    ...init,
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...init.headers },
  })
  if (response.ok) return
  return throwProblemFromBody(response)
}

/** Raw text content を返す API request を実行し、失敗時だけ Problem Details を解析する。 */
export async function requestApiText(url: string, init: RequestInit = {}): Promise<string> {
  const response = await fetch(url, { ...init, credentials: 'same-origin' })
  if (response.ok) return response.text()
  return throwProblemFromBody(response)
}

/** 失敗 response の body を一度だけ解析し、常に型付き ApiProblemError を送出する。 */
async function throwProblemFromBody(response: Response): Promise<never> {
  let value: unknown
  try {
    value = (await response.json()) as unknown
  } catch {
    throw new ApiProblemError(`API returned ${response.status}`, response.status)
  }
  throw problemFromResponse(response.status, value)
}

/** Unknown Problem body から存在情報を追加しない client error を生成する。 */
function problemFromResponse(status: number, value: unknown): ApiProblemError {
  const detail = isRecord(value) && typeof value.detail === 'string'
    ? value.detail
    : `API returned ${status}`
  const code = isRecord(value) && typeof value.code === 'string' ? value.code : undefined
  return new ApiProblemError(detail, status, code)
}

/** JSON object かどうかを prototype に依存せず判定する。 */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
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
