import { describe, expect, it } from 'vitest'

import { ApiProblemError } from '../../src/api'
import { MESSAGES, UI_LANGUAGES } from '../../src/lib/i18n/messages'
import { creationEmailQuery, passwordIssue, sameUser, userFailure } from '../../src/lib/userFeedback'

describe('account failure boundaries', () => {
  it.each([
    [401, undefined, 'sessionExpired'],
    [401, 'invalid_credentials', 'sessionExpired'],
    [400, 'current_password_rejected', 'passwordRejected'],
    [403, 'csrf_rejected', 'csrfRejected'],
    [403, 'administrator_required', 'adminRequired'],
    [404, 'user_not_found', 'notFound'],
    [409, 'user_version_conflict', 'versionConflict'],
    [409, 'last_active_admin', 'lastAdmin'],
    [409, 'user_email_conflict', 'emailConflict'],
    [422, 'validation_error', 'invalidRequest'],
    [422, 'invalid_user_request', 'invalidRequest'],
    [503, 'login_protection_unavailable', 'unavailable'],
  ] as const)('keeps %s / %s distinct without displaying response details', (status, code, key) => {
    const error = new ApiProblemError('untrusted response body', status, code)
    expect(userFailure(error, true)).toEqual({ key })
    expect(userFailure(error, false)).toEqual({ key })
    for (const language of UI_LANGUAGES) {
      expect(MESSAGES[language].account.failures[key]).not.toContain(error.message)
    }
  })

  it.each([
    new Error('invalid success payload'),
    new DOMException('request interrupted', 'AbortError'),
    new ApiProblemError('untrusted', 500),
    new ApiProblemError('untrusted', 503),
    new ApiProblemError('untrusted', 409, 'other_conflict'),
    new ApiProblemError('untrusted', 403),
    new ApiProblemError('untrusted', 422, 'other_error'),
    new ApiProblemError('untrusted', 429),
    new ApiProblemError('untrusted', 429, 'proxy_error', 60),
  ])('does not turn an unrecognized mutation failure into a rollback: %s', (error) => {
    expect(userFailure(error, true)).toEqual({ key: 'unknown' })
    expect(userFailure(error, false)).toEqual({ key: 'loadFailed' })
  })

  it('carries the finite server cooldown without applying it to an unavailable response', () => {
    expect(userFailure(new ApiProblemError('untrusted', 429, 'login_rate_limited', 60), true))
      .toEqual({ key: 'rateLimited', retryAfterSeconds: 60 })
    expect(userFailure(new ApiProblemError('untrusted', 503, 'login_protection_unavailable', 60), true))
      .toEqual({ key: 'unavailable' })
  })

  it.each([undefined, 0, -1, 1.5, 301, Infinity, NaN, Number.MAX_SAFE_INTEGER])('does not lock the form using a wait outside the account protection contract: %s', (seconds) => {
    expect(userFailure(new ApiProblemError('untrusted', 429, 'login_rate_limited', seconds), true))
      .toEqual({ key: 'rateLimited' })
  })
})

describe('account input constraints', () => {
  it('counts Unicode code points rather than UTF-16 units', () => {
    expect(passwordIssue('😀'.repeat(8), '😀'.repeat(8))).toBe('passwordPolicy')
    expect(passwordIssue('😀'.repeat(15), '😀'.repeat(15))).toBeNull()
    expect(passwordIssue(' '.repeat(15), ' '.repeat(15))).toBeNull()
  })

  it('enforces the UTF-8 byte ceiling and the confirmation separately', () => {
    expect(passwordIssue('😀'.repeat(256), '😀'.repeat(256))).toBeNull()
    expect(passwordIssue('😀'.repeat(257), '😀'.repeat(257))).toBe('passwordPolicy')
    expect(passwordIssue('x'.repeat(15), 'y'.repeat(15))).toBe('passwordMismatch')
    expect(passwordIssue('x'.repeat(14), 'x'.repeat(14))).toBe('passwordPolicy')
  })

  it('normalizes only UUID letter case when comparing response targets', () => {
    expect(sameUser('abcdef01-1234-4567-89ab-cdef01234567', 'ABCDEF01-1234-4567-89AB-CDEF01234567')).toBe(true)
    expect(sameUser('abcdef01-1234-4567-89ab-cdef01234567', 'abcdef01-1234-4567-89ab-cdef01234568')).toBe(false)
  })

  it('looks up the original email within the server search bound without a client-side list filter', () => {
    expect(creationEmailQuery('  account@example.invalid  ')).toBe('account@example.invalid')
    const email = `${'a'.repeat(200)}@example.invalid`
    expect(creationEmailQuery(email)).toBe('a'.repeat(200))
    expect(email.endsWith('@example.invalid')).toBe(true)
    expect([...creationEmailQuery('界'.repeat(220))]).toHaveLength(200)
  })
})
