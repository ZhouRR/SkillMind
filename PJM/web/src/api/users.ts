import { isApiTimestamp, isUuid } from '../lib/validation'
import { API_BASE, exactFields, isRecord, requestApiJson } from './http'

/** Project の役割ではなく、組織 account の二つの権限。 */
export type UserRole = 'ADMIN' | 'USER'
/** 無効化しても ID と安全履歴を保持する account 状態。 */
export type UserStatus = 'ACTIVE' | 'DISABLED'
/** Password/hash/認証 token を一切含まない公開 account。 */
export interface UserAccountRecord {
  user_id: string
  email: string
  display_name: string
  system_role: UserRole
  status: UserStatus
  row_version: number
  created_at: string
  updated_at: string
}
/** 自由 metadata を受け取らず、公開許可された安全操作だけを表示する。 */
export interface UserSecurityEventRecord {
  event_id: string
  user_id: string
  actor_id: string
  action: 'CREATED' | 'UPDATED' | 'PASSWORD_CHANGED' | 'SESSIONS_REVOKED'
  row_version: number
  previous_role: UserRole | null
  previous_status: UserStatus | null
  system_role: UserRole
  status: UserStatus
  revoked_sessions: number
  request_id: string
  created_at: string
}
/** 同じ server query に属する件数/page。先頭 page の client filter は行わない。 */
export interface UserPageRecord<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}
/** 会話失効は変更対象の版や失効行数と独立した server の事実。 */
export interface UserMutationRecord {
  user: UserAccountRecord
  revoked_sessions: number
  session_revoked: boolean
}
/** ADMIN が明示的な役割と一時的な初期 password を指定する。 */
export interface CreateUserInput {
  email: string
  display_name: string
  system_role: UserRole
  password: string
}
/** 原版に対する完全な編集内容。email や password は変更できない。 */
export interface UpdateUserInput {
  display_name: string
  system_role: UserRole
  status: UserStatus
  expected_row_version: number
}
/** 自分の原会話を保ったまま現在 password を検証し、全旧会話を失効させる。 */
export interface ChangePasswordInput {
  current_password: string
  new_password: string
  expected_row_version: number
}

const ACCOUNT_FIELDS = ['user_id', 'email', 'display_name', 'system_role', 'status', 'row_version', 'created_at', 'updated_at']
const EVENT_FIELDS = ['event_id', 'user_id', 'actor_id', 'action', 'row_version', 'previous_role', 'previous_status', 'system_role', 'status', 'revoked_sessions', 'request_id', 'created_at']

/** 既知の system role 以外を TypeScript の union assertion で通さない。 */
function isRole(value: unknown): value is UserRole { return value === 'ADMIN' || value === 'USER' }
/** 既知の account 状態以外を ACTIVE に丸めない。 */
function isStatus(value: unknown): value is UserStatus { return value === 'ACTIVE' || value === 'DISABLED' }
/** JSON number の精度喪失や bool/string を版・件数として受理しない。 */
function isInteger(value: unknown, minimum = 0): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum
}
/** 追加禁止を含む account Schema の公開境界。 */
function isAccount(value: unknown): value is UserAccountRecord {
  return isRecord(value) && exactFields(value, ACCOUNT_FIELDS)
    && isUuid(value.user_id) && typeof value.email === 'string' && typeof value.display_name === 'string'
    && isRole(value.system_role) && isStatus(value.status) && isInteger(value.row_version, 1)
    && isApiTimestamp(value.created_at) && isApiTimestamp(value.updated_at)
}
/** Audit の全 required field と nullable な旧状態を別々に確認する。 */
function isSecurityEvent(value: unknown): value is UserSecurityEventRecord {
  return isRecord(value) && exactFields(value, EVENT_FIELDS)
    && isUuid(value.event_id) && isUuid(value.user_id) && isUuid(value.actor_id) && isUuid(value.request_id)
    && typeof value.action === 'string' && ['CREATED', 'UPDATED', 'PASSWORD_CHANGED', 'SESSIONS_REVOKED'].includes(value.action)
    && isInteger(value.row_version, 1) && isInteger(value.revoked_sessions)
    && (value.previous_role === null || isRole(value.previous_role))
    && (value.previous_status === null || isStatus(value.previous_status))
    && isRole(value.system_role) && isStatus(value.status) && isApiTimestamp(value.created_at)
}
/** No-op を許容し、失効行数を online 人数と解釈しない。 */
function isMutation(value: unknown): value is UserMutationRecord {
  return isRecord(value) && exactFields(value, ['user', 'revoked_sessions', 'session_revoked'])
    && isAccount(value.user) && isInteger(value.revoked_sessions) && typeof value.session_revoked === 'boolean'
}
/** Request と同じ page を検証し、異なる offset を現在一覧へ混ぜない。 */
function pageGuard<T>(guard: (value: unknown) => value is T, limit: number, offset: number) {
  return (value: unknown): value is UserPageRecord<T> => isRecord(value)
    && exactFields(value, ['items', 'total', 'limit', 'offset'])
    && Array.isArray(value.items) && value.items.every(guard)
    && value.items.length <= limit && isInteger(value.total)
    && value.limit === limit && value.offset === offset
}
/** 予期しない値を error に埋めず、全 request の cache/abort 方針を共有する。 */
async function callUsers<T>(suffix: string, guard: (value: unknown) => value is T, init: RequestInit = {}): Promise<T> {
  init.signal?.throwIfAborted()
  const value = await requestApiJson(`${API_BASE}/users${suffix}`, { ...init, cache: 'no-store' })
  init.signal?.throwIfAborted()
  if (!guard(value)) throw new Error('User response did not match its contract')
  return value
}
/** UUID 以外を path へ繋がず、権限は必ず API 側で再検証する。 */
function userPath(userId: string): string {
  if (!isUuid(userId)) throw new Error('Invalid user identity')
  return `/${encodeURIComponent(userId)}`
}
/** 安全な有限 page だけを server へ送り、曖昧な string 変換をしない。 */
function pageQuery(limit: number, offset: number): URLSearchParams {
  if (!isInteger(limit, 1) || limit > 100 || !isInteger(offset)) throw new Error('Invalid user page')
  return new URLSearchParams({ limit: String(limit), offset: String(offset) })
}
/** 指定 ID の応答だけを現在の編集対象として消費する。 */
function forUser<T>(guard: (value: unknown) => value is T, id: string, identity: (value: T) => string) {
  return (value: unknown): value is T => guard(value) && identity(value).toLowerCase() === id.toLowerCase()
}
/** 現在表示中の版を必須とし、自動的な増分・補正はしない。 */
function versionBody(version: number): { expected_row_version: number } {
  if (!isInteger(version, 1)) throw new Error('Invalid user version')
  return { expected_row_version: version }
}
/** JSON body と CSRF を mutation にだけ付け、再送/幂等 header は捏造しない。 */
function mutationInit(body: object, csrfToken: string, signal?: AbortSignal, method = 'POST'): RequestInit {
  return { method, signal, headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken }, body: JSON.stringify(body) }
}

/** Project 非依存の本人 account を取得する。 */
export function loadMyAccount(signal?: AbortSignal): Promise<UserAccountRecord> {
  return callUsers('/me/account', isAccount, { signal })
}
/** 編集前/競合後の精確 ID 読取。一覧の検索や first page filter で代用しない。 */
export function loadUserAccount(userId: string, signal?: AbortSignal): Promise<UserAccountRecord> {
  return callUsers(userPath(userId), forUser(isAccount, userId, (user) => user.user_id), { signal })
}
/** ADMIN の組織検索を server に任せ、個人情報を hash URL/storage に残さない。 */
export function loadUsers(query = '', limit = 25, offset = 0, signal?: AbortSignal): Promise<UserPageRecord<UserAccountRecord>> {
  if ([...query].length > 200) throw new Error('Invalid user query')
  const search = pageQuery(limit, offset)
  if (query) search.set('q', query)
  return callUsers(`?${search}`, pageGuard(isAccount, limit, offset), { signal })
}
/** 本人の安全履歴を有限 page で取得する。 */
export function loadMySecurityEvents(limit = 25, offset = 0, signal?: AbortSignal): Promise<UserPageRecord<UserSecurityEventRecord>> {
  return callUsers(`/me/security-events?${pageQuery(limit, offset)}`, pageGuard(isSecurityEvent, limit, offset), { signal })
}
/** ADMIN が選んだ一人の履歴だけを読み、他人の記録の混入を拒否する。 */
export function loadUserSecurityEvents(userId: string, limit = 25, offset = 0, signal?: AbortSignal): Promise<UserPageRecord<UserSecurityEventRecord>> {
  return callUsers(`${userPath(userId)}/security-events?${pageQuery(limit, offset)}`,
    pageGuard(forUser(isSecurityEvent, userId, (event) => event.user_id), limit, offset), { signal })
}
/** 初期 password はこの一回の body にだけ含め、unknown 時にも自動再送しない。 */
export function createUser(input: CreateUserInput, csrfToken: string, signal?: AbortSignal): Promise<UserMutationRecord> {
  const { email, display_name, system_role, password } = input
  return callUsers('', isMutation, mutationInit({ email, display_name, system_role, password }, csrfToken, signal))
}
/** 原版を変えずに三項目を更新し、immutable email を body に混ぜない。 */
export function updateUser(userId: string, input: UpdateUserInput, csrfToken: string, signal?: AbortSignal): Promise<UserMutationRecord> {
  const { display_name, system_role, status } = input
  return callUsers(userPath(userId), forUser(isMutation, userId, (result) => result.user.user_id),
    mutationInit({ display_name, system_role, status, ...versionBody(input.expected_row_version) }, csrfToken, signal, 'PUT'))
}
/** 現 password の拒否は 401 へ変換せず、client に元の Problem を返す。 */
export function changeMyPassword(input: ChangePasswordInput, csrfToken: string, signal?: AbortSignal): Promise<UserMutationRecord> {
  const { current_password, new_password } = input
  return callUsers('/me/password', isMutation,
    mutationInit({ current_password, new_password, ...versionBody(input.expected_row_version) }, csrfToken, signal))
}
/** 現在会話も失効するが、client が別途 logout や再ログインを実行してはならない。 */
export function revokeMySessions(version: number, csrfToken: string, signal?: AbortSignal): Promise<UserMutationRecord> {
  return callUsers('/me/sessions/revoke', isMutation, mutationInit(versionBody(version), csrfToken, signal))
}
/** ADMIN が精確対象の未撤销会話を失効させる。 */
export function revokeUserSessions(userId: string, version: number, csrfToken: string, signal?: AbortSignal): Promise<UserMutationRecord> {
  return callUsers(`${userPath(userId)}/sessions/revoke`, forUser(isMutation, userId, (result) => result.user.user_id),
    mutationInit(versionBody(version), csrfToken, signal))
}
