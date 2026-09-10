import { describe, expect, it } from 'vitest'

import { ApiProblemError } from '../../src/api'
import { projectFailure, PROJECT_REQUEST_POLICY, type ProjectFailure } from '../../src/lib/projectFeedback'

describe('projectFailure', () => {
  it.each<[number, string, ProjectFailure['key']]>([
    [401, 'authentication_required', 'sessionExpired'],
    [403, 'csrf_rejected', 'csrfRejected'], [403, 'administrator_required', 'adminRequired'],
    [404, 'project_not_found', 'notFound'], [422, 'validation_error', 'invalidRequest'],
    [409, 'project_key_conflict', 'keyConflict'], [409, 'project_version_conflict', 'versionConflict'],
    [409, 'project_version_exhausted', 'versionExhausted'], [409, 'project_delete_requires_archive', 'needsArchive'],
    [409, 'project_delete_blocked_by_runs', 'blockedByRuns'], [409, 'project_delete_blocked_by_schedules', 'blockedBySchedules'],
    [409, 'project_delete_blocked_by_member_audit', 'blockedByMemberAudit'],
    [409, 'project_delete_blocked_by_document_uploads', 'blockedByDocumentUploads'],
  ])('recognizes only the documented rejection %s / %s', (status, code, key) => {
    expect(projectFailure(new ApiProblemError('private server body', status, code), true)).toEqual({ key })
  })

  it.each([403, 404, 409, 422, 500])('does not turn an arbitrary proxy %s into a known rejection', (status) => {
    const error = new ApiProblemError('private proxy body', status, 'proxy_failed')
    expect(projectFailure(error, true)).toEqual({ key: 'unknown' })
    expect(projectFailure(error, false)).toEqual({ key: 'loadFailed' })
  })

  it.each([new Error('connection lost'), new SyntaxError('invalid success JSON'), null])('keeps transport or invalid success as unknown', (error) => {
    expect(projectFailure(error, true)).toEqual({ key: 'unknown' })
    expect(projectFailure(error, false)).toEqual({ key: 'loadFailed' })
  })

  it('blocks unknown and version conflict, but treats version exhaustion as a known rejection', () => {
    expect(PROJECT_REQUEST_POLICY.blocks({ key: 'unknown' })).toBe(true)
    expect(PROJECT_REQUEST_POLICY.blocks({ key: 'versionConflict' })).toBe(true)
    expect(PROJECT_REQUEST_POLICY.blocks({ key: 'versionExhausted' })).toBe(false)
    expect(PROJECT_REQUEST_POLICY.readTimeout).toEqual({ key: 'loadFailed' })
    expect(PROJECT_REQUEST_POLICY.writeTimeout).toEqual({ key: 'unknown' })
  })
})
