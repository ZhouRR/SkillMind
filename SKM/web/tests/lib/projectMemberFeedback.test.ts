import { describe, expect, it } from 'vitest'

import { ApiProblemError } from '../../src/api'
import { projectMemberFailure, PROJECT_MEMBER_REQUEST_POLICY } from '../../src/lib/projectMemberFeedback'

describe('membership failure projection', () => {
  it.each([
    [401, undefined, 'sessionExpired'], [403, 'csrf_rejected', 'csrfRejected'],
    [403, 'administrator_required', 'adminRequired'], [404, 'project_not_found', 'notFound'],
    [404, 'user_not_found', 'notFound'], [422, 'validation_error', 'invalidRequest'],
  ] as const)('projects only the documented refusal %s/%s', (status, code, key) => {
    expect(projectMemberFailure(new ApiProblemError('private server detail', status, code), true)).toEqual({ key })
  })

  it.each([200, 202, 403, 404, 409, 422, 429, 500, 503])('keeps an unclassified %s write unknown', (status) => {
    const error = new ApiProblemError('private server detail', status, 'unexpected_code')
    const failure = projectMemberFailure(error, true)
    expect(failure).toEqual({ key: 'unknown' })
    expect(PROJECT_MEMBER_REQUEST_POLICY.blocks(failure)).toBe(true)
    expect(projectMemberFailure(error, false)).toEqual({ key: 'loadFailed' })
  })

  it('treats transport abort and validation failures as unknown after a write', () => {
    expect(projectMemberFailure(new Error('invalid success body'), true)).toEqual({ key: 'unknown' })
    expect(projectMemberFailure(new DOMException('aborted', 'AbortError'), true)).toEqual({ key: 'unknown' })
  })
})
