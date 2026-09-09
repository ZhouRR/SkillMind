import { ApiProblemError } from '../api'
import type { UiMessages } from './i18n/messages'

/** Login の拒否を三語の公開案内へ変換し、例外の内部 message をそのまま表示しない。 */
export function loginFeedback(reason: unknown, messages: UiMessages['login']): string {
  if (!(reason instanceof ApiProblemError)) return messages.failed
  if (reason.status === 429) {
    const seconds = reason.retryAfterSeconds
    // Login protocol の範囲外を、サーバーが約束した解除時刻として案内しない。
    return seconds !== undefined && Number.isInteger(seconds) && seconds >= 1 && seconds <= 300
      ? messages.retryAfter(seconds)
      : messages.rateLimited
  }
  if (reason.status === 503) return messages.unavailable
  if (reason.status === 401 && reason.code === 'invalid_credentials') return messages.invalidCredentials
  if (reason.status === 403 && reason.code === 'csrf_rejected') return messages.csrfRejected
  return messages.failed
}
