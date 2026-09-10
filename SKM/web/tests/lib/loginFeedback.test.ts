import { describe, expect, it } from 'vitest'

import { ApiProblemError } from '../../src/api'
import { MESSAGES, UI_LANGUAGES } from '../../src/lib/i18n/messages'
import { loginFeedback } from '../../src/lib/loginFeedback'

describe.each(UI_LANGUAGES)('login feedback in %s', (language) => {
  const messages = MESSAGES[language].login

  it.each([1, 60, 120, 240, 300])('uses the server wait of %d seconds', (seconds) => {
    expect(loginFeedback(new ApiProblemError('hidden', 429, 'login_rate_limited', seconds), messages))
      .toBe(messages.retryAfter(seconds))
    expect(messages.retryAfter(seconds)).toContain(String(seconds))
  })

  it.each([undefined, 0, -1, 1.5, 301, Infinity, NaN])('does not invent a wait from %s', (seconds) => {
    expect(loginFeedback(new ApiProblemError('hidden', 429, undefined, seconds), messages))
      .toBe(messages.rateLimited)
  })

  it('separates unavailable protection, credentials and CSRF without exposing details', () => {
    expect(loginFeedback(new ApiProblemError('hidden', 503, 'login_protection_unavailable'), messages))
      .toBe(messages.unavailable)
    expect(loginFeedback(new ApiProblemError('hidden', 503), messages)).toBe(messages.unavailable)
    expect(loginFeedback(new ApiProblemError('hidden', 401, 'invalid_credentials'), messages))
      .toBe(messages.invalidCredentials)
    expect(loginFeedback(new ApiProblemError('hidden', 403, 'csrf_rejected'), messages))
      .toBe(messages.csrfRejected)
    for (const error of [new Error('hidden'), 'hidden', new ApiProblemError('hidden', 500)]) {
      expect(loginFeedback(error, messages)).toBe(messages.failed)
    }
  })
})
