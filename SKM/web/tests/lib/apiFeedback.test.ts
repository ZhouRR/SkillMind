import { describe, expect, it } from 'vitest'

import { ApiProblemError } from '../../src/api/http'
import { apiErrorMessage } from '../../src/lib/apiFeedback'
import { MESSAGES, UI_LANGUAGES } from '../../src/lib/i18n/messages'

describe('administrator permission feedback', () => {
  it.each(UI_LANGUAGES)('uses the %s catalog for the stable permission code', (language) => {
    const error = new ApiProblemError('Administrator access is required.', 403, 'administrator_required')
    expect(apiErrorMessage(error, 'fallback', MESSAGES[language])).toBe(MESSAGES[language].account.failures.adminRequired)
    expect(error.status).toBe(403)
    expect(error.code).toBe('administrator_required')
  })

  it('does not infer administrator denial from message text or another status', () => {
    for (const error of [
      new Error('Administrator access is required.'),
      new ApiProblemError('Other failure', 403, 'csrf_failed'),
      new ApiProblemError('Result unknown', 503, 'administrator_required'),
    ]) expect(apiErrorMessage(error, 'fallback', MESSAGES.ja)).toBe(error.message)
    expect(apiErrorMessage(null, 'fallback', MESSAGES.ja)).toBe('fallback')
  })
})
