import { describe, expect, it } from 'vitest'

import type { ProjectRecord } from '../../src/api'
import { projectRequestFromHash, resolveProjectSelection } from '../../src/lib/projectContext'
import { DEMO_PROJECT } from '../fixtures'

/** 参照可能な 2 件目の Project。既定選択が先頭を採ることの確認に使う。 */
const OTHER_PROJECT: ProjectRecord = {
  ...DEMO_PROJECT,
  project_id: '00000000-0000-4000-8000-000000000011',
  key: 'platform-team',
  name: 'Platform Team',
}

const UNKNOWN_PROJECT_ID = '00000000-0000-4000-8000-0000000000ff'
const ARCHIVED_PROJECT: ProjectRecord = {
  ...OTHER_PROJECT,
  project_id: UNKNOWN_PROJECT_ID,
  status: 'ARCHIVED',
}

describe('resolveProjectSelection', () => {
  it('keeps the explicit project ahead of the saved preference without granting access', () => {
    /** この純関数は対象だけを選び、権限の確認は詳細 API に委ねる。 */
    const selection = resolveProjectSelection(
      [DEMO_PROJECT, OTHER_PROJECT],
      OTHER_PROJECT.project_id,
      DEMO_PROJECT.project_id,
    )

    expect(selection).toBe(OTHER_PROJECT.project_id)
  })

  it('falls back to the saved preference when the URL names no project', () => {
    const selection = resolveProjectSelection(
      [DEMO_PROJECT, OTHER_PROJECT],
      null,
      OTHER_PROJECT.project_id,
    )

    expect(selection).toBe(OTHER_PROJECT.project_id)
  })

  it('selects the first active project when no explicit target or preference exists', () => {
    const selection = resolveProjectSelection([DEMO_PROJECT, OTHER_PROJECT], null, null)

    expect(selection).toBe(DEMO_PROJECT.project_id)
  })

  it('selects the first active project when only the saved preference is stale', () => {
    const selection = resolveProjectSelection([DEMO_PROJECT], null, UNKNOWN_PROJECT_ID)

    expect(selection).toBe(DEMO_PROJECT.project_id)
  })

  it('retains an explicit target absent from the list instead of substituting the preference', () => {
    const selection = resolveProjectSelection(
      [DEMO_PROJECT],
      UNKNOWN_PROJECT_ID,
      DEMO_PROJECT.project_id,
    )

    expect(selection).toBe(UNKNOWN_PROJECT_ID)
  })

  it('retains the explicit target even when the active list is empty', () => {
    const selection = resolveProjectSelection([], UNKNOWN_PROJECT_ID, DEMO_PROJECT.project_id)

    expect(selection).toBe(UNKNOWN_PROJECT_ID)
  })

  it('retains an explicitly requested archived target for separate authorized detail lookup', () => {
    expect(resolveProjectSelection(
      [DEMO_PROJECT, ARCHIVED_PROJECT], ARCHIVED_PROJECT.project_id, DEMO_PROJECT.project_id,
    )).toBe(ARCHIVED_PROJECT.project_id)
  })

  it('does not interpret an explicit empty target as an absent parameter', () => {
    expect(resolveProjectSelection([DEMO_PROJECT], '', DEMO_PROJECT.project_id)).toBe('')
  })

  it('does not silently normalize the spelling of an explicit UUID', () => {
    const requested = UNKNOWN_PROJECT_ID.toUpperCase()
    expect(resolveProjectSelection([DEMO_PROJECT], requested, null)).toBe(requested)
  })

  it('matches a remembered UUID case-insensitively but returns the current active record identity', () => {
    const activeProject = { ...ARCHIVED_PROJECT, status: 'ACTIVE' as const }
    expect(resolveProjectSelection(
      [DEMO_PROJECT, activeProject], null, activeProject.project_id.toUpperCase(),
    )).toBe(activeProject.project_id)
  })

  it('excludes archived projects from both preference selection and first-item fallback', () => {
    expect(resolveProjectSelection(
      [ARCHIVED_PROJECT, DEMO_PROJECT], null, ARCHIVED_PROJECT.project_id,
    )).toBe(DEMO_PROJECT.project_id)
    expect(resolveProjectSelection([ARCHIVED_PROJECT, DEMO_PROJECT], null, null))
      .toBe(DEMO_PROJECT.project_id)
  })

  it.each([{ projects: [] }, { projects: [ARCHIVED_PROJECT] }])('selects nothing without an explicit target or any active record', ({ projects }) => {
    expect(resolveProjectSelection(projects, null, ARCHIVED_PROJECT.project_id)).toBe('')
  })
})

describe('projectRequestFromHash', () => {
  it.each(['', '#/', '#/workspace', '#/workspace?', '#/workspace?run=old&task=old'])('distinguishes an absent parameter in %s', (hash) => {
    expect(projectRequestFromHash(hash)).toEqual({ kind: 'absent' })
  })

  it.each([UNKNOWN_PROJECT_ID, UNKNOWN_PROJECT_ID.toUpperCase()])('preserves one explicit UUID without authorizing or normalizing it: %s', (projectId) => {
    expect(projectRequestFromHash(`#/workspace?run=old&project=${projectId}&task=old`))
      .toEqual({ kind: 'explicit', projectId })
  })

  it.each([
    ['project=', ''],
    ['project', ''],
    ['project=not-a-uuid', 'not-a-uuid'],
    ['project=%20', ' '],
    ['project=..%2Fother', '../other'],
    [`project=${UNKNOWN_PROJECT_ID}&project=${UNKNOWN_PROJECT_ID}`, UNKNOWN_PROJECT_ID],
    [`project=${UNKNOWN_PROJECT_ID}&project=${DEMO_PROJECT.project_id}`, UNKNOWN_PROJECT_ID],
    [`project=&project=${UNKNOWN_PROJECT_ID}`, ''],
    [`project=${UNKNOWN_PROJECT_ID}&project=`, UNKNOWN_PROJECT_ID],
  ])('keeps malformed or repeated project parameters invalid: %s', (query, value) => {
    expect(projectRequestFromHash(`#/workspace?${query}&run=old`)).toEqual({ kind: 'invalid', value })
  })

  it.each([
    '#/accounts', '#/accounts?project=', '#/accounts/?project=invalid',
    `#/accounts?project=${UNKNOWN_PROJECT_ID}`, '#/accounts?project=&project=invalid&run=old&task=old',
  ])('ignores every project parameter on the account route: %s', (hash) => {
    expect(projectRequestFromHash(hash)).toEqual({ kind: 'absent' })
  })
})
