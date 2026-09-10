import { ApiProblemError } from '../api'
import type { UiMessages } from './i18n/messages'

/** Server の本文を表示せず、アカウント操作の既知の結果だけを分類する。 */
export interface UserFailure {
  key: keyof UiMessages['account']['failures']
  retryAfterSeconds?: number
}

/** 200 の解析失敗や transport 障害を「拒否・rollback」と誤表示しない。 */
export function userFailure(error: unknown, mutation: boolean): UserFailure {
  if (error instanceof ApiProblemError) {
    const { status, code } = error
    if (status === 401) return { key: 'sessionExpired' }
    if (status === 400 && code === 'current_password_rejected') return { key: 'passwordRejected' }
    if (status === 403 && code === 'csrf_rejected') return { key: 'csrfRejected' }
    if (status === 403 && code === 'administrator_required') return { key: 'adminRequired' }
    if (status === 404 && code === 'user_not_found') return { key: 'notFound' }
    if (status === 409 && code === 'user_version_conflict') return { key: 'versionConflict' }
    if (status === 409 && code === 'last_active_admin') return { key: 'lastAdmin' }
    if (status === 409 && code === 'user_email_conflict') return { key: 'emailConflict' }
    if (status === 422 && ['validation_error', 'invalid_user_request'].includes(code ?? '')) {
      return { key: 'invalidRequest' }
    }
    if (status === 429 && code === 'login_rate_limited') {
      const seconds = error.retryAfterSeconds
      // 本人改密も login protection の最大 300 秒を使い、未知の header で永久禁止にしない。
      return seconds !== undefined && Number.isInteger(seconds) && seconds >= 1 && seconds <= 300
        ? { key: 'rateLimited', retryAfterSeconds: seconds }
        : { key: 'rateLimited' }
    }
    if (status === 503 && code === 'login_protection_unavailable') return { key: 'unavailable' }
  }
  return { key: mutation ? 'unknown' : 'loadFailed' }
}

/** 文字符号数と UTF-8 byte 数を分け、Server の password 制限に合わせる。 */
export function passwordIssue(password: string, confirmation: string): 'passwordPolicy' | 'passwordMismatch' | null {
  if ([...password].length < 15 || new TextEncoder().encode(password).length > 1024) return 'passwordPolicy'
  return password === confirmation ? null : 'passwordMismatch'
}

/** 大小文字だけが異なる UUID 表記を同じ対象として照合する。 */
export function sameUser(left: string, right: string): boolean {
  return left.toLowerCase() === right.toLowerCase()
}

/** Email は 320 文字まで、検索は 200 文字まで。長い email も原値を残して候補を読む。 */
export function creationEmailQuery(email: string): string {
  return [...email.trim()].slice(0, 200).join('')
}
