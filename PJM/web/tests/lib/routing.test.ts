import { describe, expect, it } from 'vitest'

import {
  APP_ROUTES,
  projectIdFromHash,
  routeFromHash,
  routeHref,
  routeUsesModuleFilter,
} from '../../src/lib/routing'

describe('application routing', () => {
  it('maps every public screen to a stable hash URL', () => {
    /** Traefik context path に依存せず同じ静的 document 内を遷移できることを守る。 */
    expect(routeHref('home')).toBe('#/')
    expect(routeHref('skills')).toBe('#/skills')
    expect(routeHref('projects')).toBe('#/projects')
    expect(routeHref('documents')).toBe('#/documents')
    expect(routeHref('resources')).toBe('#/resources')
    expect(routeHref('workspace')).toBe('#/workspace')
    expect(routeHref('history')).toBe('#/history')
  })

  it('falls back to home for an unknown or empty route', () => {
    /** 不明 URL で空画面を出さず、常に安全な入口へ戻ることを守る。 */
    expect(routeFromHash('')).toBe('home')
    expect(routeFromHash('#/unknown')).toBe('home')
    expect(routeFromHash('#/skills/')).toBe('skills')
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

    expect(projectRoutes).toEqual(['workspace', 'history', 'tasks', 'documents', 'resources'])
  })

  it('keeps the module filter active on both screens that read it', () => {
    /** 任务中心と工作空间は同じ module 選択を読む。片方だけ強調すると選択が消えて見える。 */
    expect(routeUsesModuleFilter('tasks')).toBe(true)
    expect(routeUsesModuleFilter('workspace')).toBe(true)
    expect(routeUsesModuleFilter('home')).toBe(false)
    expect(routeUsesModuleFilter('projects')).toBe(false)
  })
})
