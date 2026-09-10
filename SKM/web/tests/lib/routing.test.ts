import { describe, expect, it } from 'vitest'

import {
  APP_ROUTES,
  projectIdFromHash,
  routeFromHash,
  routeHref,
  routeHrefWithProject,
} from '../../src/lib/routing'

describe('application routing', () => {
  it('maps every public screen to a stable hash URL', () => {
    /** Traefik context path に依存せず同じ静的 document 内を遷移できることを守る。 */
    expect(routeHref('home')).toBe('#/')
    expect(routeHref('skills')).toBe('#/skills')
    expect(routeHref('projects')).toBe('#/projects')
    expect(routeHref('accounts')).toBe('#/accounts')
    expect(routeHref('documents')).toBe('#/documents')
    expect(routeHref('resources')).toBe('#/resources')
    expect(routeHref('workspace')).toBe('#/workspace')
    expect(routeHref('history')).toBe('#/history')
    expect(routeHref('schedules')).toBe('#/schedules')
  })

  it('falls back to home for an unknown or empty route', () => {
    /** 不明 URL で空画面を出さず、常に安全な入口へ戻ることを守る。 */
    expect(routeFromHash('')).toBe('home')
    expect(routeFromHash('#/unknown')).toBe('home')
    expect(routeFromHash('#/skills/')).toBe('skills')
  })

  it('keeps accounts independent of project and execution context', () => {
    /** Organization 画面に Project/Run の URL 文脈を残さない。 */
    expect(routeHref('accounts', 'project-a', { runId: 'run-a', taskId: 'task-a' })).toBe('#/accounts')
    expect(routeFromHash('#/accounts?project=project-a')).toBe('accounts')
    expect(projectIdFromHash('#/accounts?project=project-a')).toBeNull()
    expect(APP_ROUTES.find(({ route }) => route === 'accounts')?.scope).toBe('platform')
  })

  it('carries an encoded project context without changing the route', () => {
    const projectId = '00000000-0000-4000-8000-000000000010'

    expect(routeHref('workspace', projectId)).toBe(`#/workspace?project=${projectId}`)
    expect(routeFromHash(`#/workspace?project=${projectId}`)).toBe('workspace')
    expect(projectIdFromHash(`#/workspace?project=${projectId}`)).toBe(projectId)
    expect(projectIdFromHash('#/workspace')).toBeNull()
  })

  it('keeps a selected run or task in deep links', () => {
    const projectId = '00000000-0000-0000-0000-000000000010'
    const runId = '00000000-0000-0000-0000-000000000011'
    const taskId = 'skill:review'

    expect(routeHref('workspace', projectId, { runId })).toBe(
      `#/workspace?project=${projectId}&run=${runId}`,
    )
    expect(routeHref('workspace', projectId, { taskId })).toBe(
      `#/workspace?project=${projectId}&task=${encodeURIComponent(taskId)}`,
    )
  })

  it('leads the project group with the workspace, the project main screen', () => {
    /** 配列順が sidebar の並び順。主画面が先頭でないと、模块子菜单の親も入れ替わる。 */
    const projectRoutes = APP_ROUTES.filter(({ scope }) => scope === 'project')
      .map(({ route }) => route)

    expect(projectRoutes).toEqual(['workspace', 'history', 'tasks', 'schedules', 'documents', 'resources'])
  })

  it('resolves the project-wide schedule route independently', () => {
    expect(routeFromHash('#/schedules?project=original')).toBe('schedules')
  })
})

describe('routeHrefWithProject', () => {
  const projectId = '00000000-0000-4000-8000-0000000000ff'
  const fallbackProjectId = '00000000-0000-4000-8000-000000000010'

  it.each([
    'project=',
    'project=invalid',
    'project=..%2Fother%3Fvalue%3D1',
    `project=${projectId}`,
    `project=${projectId}&project=${fallbackProjectId}`,
    `project=${projectId}&project=${projectId}`,
    `project=&project=${projectId}`,
    `project=${projectId}&project=`,
  ])('preserves explicit project values and order but removes old execution context: %s', (query) => {
    const hash = `#/workspace?run=old-run&${query}&task=old-task&other=ignored`
    expect(routeHrefWithProject('history', hash, fallbackProjectId)).toBe(`#/history?${query}`)
  })

  it('keeps a bare project parameter explicitly empty instead of applying a fallback', () => {
    expect(routeHrefWithProject('workspace', '#/history?project&run=old', fallbackProjectId))
      .toBe('#/workspace?project=')
  })

  it.each(APP_ROUTES.filter(({ route }) => route !== 'accounts'))('preserves invalid targets when navigating to $route', ({ route }) => {
    expect(routeHrefWithProject(route, '#/workspace?project=&project=invalid&run=old&task=old'))
      .toBe(`${routeHref(route)}?project=&project=invalid`)
  })

  it.each(['#/workspace', '#/history?', '#/tasks?run=old&task=old'])('uses the fallback only when the source has no explicit project: %s', (hash) => {
    expect(routeHrefWithProject('documents', hash, fallbackProjectId))
      .toBe(`#/documents?project=${fallbackProjectId}`)
    expect(routeHrefWithProject('documents', hash)).toBe('#/documents')
  })

  it.each([
    '#/workspace?project=&project=invalid&run=old&task=old',
    `#/history?project=${projectId}&run=old`,
    '#/accounts?project=invalid&task=old',
  ])('removes all project and execution parameters when entering accounts: %s', (hash) => {
    expect(routeHrefWithProject('accounts', hash, fallbackProjectId)).toBe('#/accounts')
  })

  it.each([
    '#/accounts?project=invalid&run=old&task=old',
    `#/accounts/?project=${projectId}&project=`,
  ])('does not import ignored account parameters into a project route: %s', (hash) => {
    expect(routeHrefWithProject('workspace', hash, fallbackProjectId))
      .toBe(`#/workspace?project=${fallbackProjectId}`)
    expect(routeHrefWithProject('workspace', hash)).toBe('#/workspace')
  })
})
