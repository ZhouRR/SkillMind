import { isApiTimestamp, isUuid } from '../lib/validation'
import { API_BASE, exactFields, isRecord, requestApiJson } from './http'

/** 完全な秘密を含まない API key 管理行。 */
export interface ApiKeyRecord {
  id: string
  name: string
  key_prefix: string
  created_by: string
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
}
/** 発行応答だけが一回表示用の秘密を持つ。永続 storage に保存しない。 */
export interface CreatedApiKeyRecord { api_key: ApiKeyRecord; token: string }
/** 追加フィールドや不正な ID・時刻を表示へ流さない。 */
function isKey(value: unknown): value is ApiKeyRecord {
  return isRecord(value) && exactFields(value, ['id', 'name', 'key_prefix', 'created_by', 'created_at', 'last_used_at', 'revoked_at'])
    && isUuid(value.id) && isUuid(value.created_by) && typeof value.name === 'string'
    && value.name.length > 0 && [...value.name].length <= 200
    && typeof value.key_prefix === 'string' && /^skm1\.[A-Za-z0-9_-]{8}$/.test(value.key_prefix)
    && isApiTimestamp(value.created_at) && (value.last_used_at === null || isApiTimestamp(value.last_used_at))
    && (value.revoked_at === null || isApiTimestamp(value.revoked_at))
}
/** Cache と abort を既存 HTTP 境界に集約し、応答本文を error に含めない。 */
async function call<T>(suffix: string, guard: (value: unknown) => value is T, init: RequestInit): Promise<T> {
  init.signal?.throwIfAborted()
  const value = await requestApiJson(`${API_BASE}/api-keys${suffix}`, { ...init, cache: 'no-store' })
  init.signal?.throwIfAborted()
  if (!guard(value)) throw new Error('API key response did not match its contract')
  return value
}
/** 全管理行を取得する。完全 token の再取得 API は存在しない。 */
export async function loadApiKeys(signal?: AbortSignal): Promise<ApiKeyRecord[]> {
  const guard = (value: unknown): value is { items: ApiKeyRecord[] } => isRecord(value)
    && exactFields(value, ['items']) && Array.isArray(value.items) && value.items.every(isKey)
  return (await call('', guard, { signal })).items
}
/** 作成の不明結果は呼出元で保持し、client が自動再送しない。 */
export function createApiKey(name: string, csrfToken: string, signal?: AbortSignal): Promise<CreatedApiKeyRecord> {
  const guard = (value: unknown): value is CreatedApiKeyRecord => isRecord(value)
    && exactFields(value, ['api_key', 'token']) && isKey(value.api_key) && value.api_key.name === name.trim()
    && value.api_key.revoked_at === null && typeof value.token === 'string'
    && /^skm1\.[A-Za-z0-9_-]{43}$/.test(value.token) && value.token.startsWith(value.api_key.key_prefix)
  return call('', guard, { method: 'POST', signal, headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify({ name }) })
}
/** 原 ID の撤銷事実だけを成功とし、別 key の応答は採用しない。 */
export function revokeApiKey(id: string, csrfToken: string, signal?: AbortSignal): Promise<ApiKeyRecord> {
  if (!isUuid(id)) throw new Error('Invalid API key identity')
  const guard = (value: unknown): value is ApiKeyRecord => isKey(value) && value.id.toLowerCase() === id.toLowerCase() && value.revoked_at !== null
  return call(`/${encodeURIComponent(id)}/revoke`, guard, { method: 'POST', signal, headers: { 'X-CSRF-Token': csrfToken } })
}
