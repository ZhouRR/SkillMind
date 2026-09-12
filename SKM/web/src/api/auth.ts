import { API_BASE, ApiProblemError, hasStrings, isRecord, requestApiEmpty, requestApiJson } from './http'

/** Web shell が表示に利用できる credential 非含有 user。 */
export interface AuthenticatedUserRecord {
  user_id: string
  organization_id: string
  email: string
  display_name: string
  system_role: 'ADMIN' | 'USER'
}

/** Browser memory 上で保持する user identity と CSRF token。 */
export interface AuthSessionRecord {
  user: AuthenticatedUserRecord
  csrf_token: string
  absolute_expires_at: string
  /** 旧 API の省略は後置操作を公開する許可にならない。 */
  deferred_features_enabled?: boolean
  database_writes_enabled?: boolean
}

/** Password login form の入力。 */
export interface LoginInput {
  email: string
  password: string
}

/** Cookie に対応する current session を取得し、未認証は null として返す。 */
export async function loadAuthSession(signal?: AbortSignal): Promise<AuthSessionRecord | null> {
  try {
    return parseSession(await requestApiJson(`${API_BASE}/auth/session`, { signal, cache: 'no-store' }))
  } catch (error) {
    if (error instanceof ApiProblemError && error.status === 401) return null
    throw error
  }
}

/** Login CSRF challenge を取得して password login を実行する。 */
export async function login(input: LoginInput, signal?: AbortSignal): Promise<AuthSessionRecord> {
  signal?.throwIfAborted()
  const context = parseLoginContext(await requestApiJson(
    `${API_BASE}/auth/login-context`,
    { signal, cache: 'no-store' },
  ))
  // Transport が abort に遅れて応答しても、離頁後に password POST を開始しない。
  signal?.throwIfAborted()
  const response = await requestApiJson(`${API_BASE}/auth/login`, {
    method: 'POST',
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': context.csrf_token,
    },
    body: JSON.stringify(input),
    signal,
  })
  signal?.throwIfAborted()
  return parseSession(response)
}

/** Current session を server 側で失効させる。 */
export async function logout(csrfToken: string, signal?: AbortSignal): Promise<void> {
  await requestApiEmpty(`${API_BASE}/auth/logout`, {
    method: 'POST',
    cache: 'no-store',
    headers: { 'X-CSRF-Token': csrfToken },
    signal,
  })
}

/** Unknown JSON が login challenge contract を満たすことを検証する。 */
function parseLoginContext(value: unknown): { csrf_token: string; expires_in_seconds: number } {
  if (!isRecord(value)
    || typeof value.csrf_token !== 'string'
    || value.csrf_token.length < 32
    || typeof value.expires_in_seconds !== 'number') {
    throw new Error('Login context response did not match its contract')
  }
  return { csrf_token: value.csrf_token, expires_in_seconds: value.expires_in_seconds }
}

/** Unknown JSON を認証済み Session contract へ制限する。 */
function parseSession(value: unknown): AuthSessionRecord {
  if (!isRecord(value)
    || !isRecord(value.user)
    || !hasStrings(value, ['csrf_token', 'absolute_expires_at'])
    || ('deferred_features_enabled' in value && typeof value.deferred_features_enabled !== 'boolean')
    || ('database_writes_enabled' in value && typeof value.database_writes_enabled !== 'boolean')
    || !hasStrings(value.user, [
      'user_id', 'organization_id', 'email', 'display_name', 'system_role',
    ])
    || typeof value.user.system_role !== 'string'
    || !['ADMIN', 'USER'].includes(value.user.system_role)) {
    throw new Error('Auth session response did not match its contract')
  }
  return value as unknown as AuthSessionRecord
}
