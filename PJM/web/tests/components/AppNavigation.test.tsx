import { renderToStaticMarkup } from 'react-dom/server'
import type { ComponentProps } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ProjectModuleRecord } from '../../src/api'
import { routeHref, type AppRoute } from '../../src/lib/routing'
import { AppNavigation } from '../../src/components/AppNavigation'
import { MESSAGES } from '../../src/lib/i18n/messages'
import { DEMO_PROJECT, demoUser } from '../fixtures'

const PROJECT = DEMO_PROJECT

afterEach(() => vi.unstubAllGlobals())

/** sidebar 子菜单に並ぶ業務模块。束縛スキルは導航の表示に関与しないため空でよい。 */
function moduleRecord(moduleId: string, name: string): ProjectModuleRecord {
  return {
    module_id: moduleId,
    project_id: PROJECT.project_id,
    name,
    description: '',
    skills: [],
    created_at: '2026-07-12T10:00:00Z',
    updated_at: '2026-07-12T10:00:00Z',
  }
}

const MODULES = [
  moduleRecord('00000000-0000-4000-8000-000000000070', '品质分析'),
  moduleRecord('00000000-0000-4000-8000-000000000071', '仕样评审'),
]

/** 指定 route・module 選択・module 一覧で sidebar を静的描画する。 */
function navigation(
  currentRoute: AppRoute,
  activeModuleId = MODULES[0]!.module_id,
  modules: ProjectModuleRecord[] = MODULES,
  overrides: Partial<ComponentProps<typeof AppNavigation>> = {},
): string {
  return renderToStaticMarkup(
    <AppNavigation
      currentRoute={currentRoute}
      metaState={{ status: 'loading' }}
      projectId={PROJECT.project_id}
      projectState={{ status: 'ready', projects: [PROJECT] }}
      onSelectLanguage={vi.fn()}
      onSelectProject={vi.fn()}
      onSelectModule={vi.fn()}
      activeModuleId={activeModuleId}
      modules={modules}
      user={demoUser()}
      onLogout={vi.fn()}
      logoutError={null}
      {...overrides}
    />,
  )
}

describe('AppNavigation grouping', () => {
  it('forwards current project detail without hiding the independent list failure', () => {
    const html = navigation('history', '', [], {
      currentProject: { ...PROJECT, status: 'ARCHIVED' },
      projectState: { status: 'error', message: MESSAGES.zh.elements.projectListFailed },
      onRefreshProjects: vi.fn(),
    })
    expect(html).toContain(`Quality Team · quality-team · ${MESSAGES.zh.elements.archivedProject}`)
    expect(html).toContain(`role="alert">${MESSAGES.zh.elements.projectListFailed}</p>`)
    expect(html).toContain(`type="button">${MESSAGES.zh.runHistory.retry}</button>`)
  })

  it.each([
    ['empty', '#/workspace?project=&run=old-run', '#/history?project='],
    ['duplicate', '#/workspace?project=first&project=second&task=old-task', '#/history?project=first&amp;project=second'],
    ['malformed', '#/workspace?project=not-a-uuid&run=old-run', '#/history?project=not-a-uuid'],
  ])('preserves the %s explicit project query instead of replacing it with a fallback', (_case, projectHash, historyHref) => {
    const html = navigation('workspace', '', [], { projectHash, pendingProjectId: '' })
    expect(html).toContain(`href="${historyHref}"`)
    expect(html).toContain('href="#/accounts"')
    expect(html).not.toContain('old-run')
    expect(html).not.toContain('old-task')
    expect(html).not.toContain(`href="${routeHref('history', PROJECT.project_id)}"`)
  })

  it('preserves an unavailable explicit project in navigation links while leaving accounts independent', () => {
    const unavailableId = '00000000-0000-4000-8000-000000000099'
    const html = navigation('workspace', '', [], { projectId: unavailableId, pendingProjectId: '' })
    for (const route of ['home', 'skills', 'projects', 'tasks', 'schedules', 'workspace', 'history', 'documents', 'resources'] as const) {
      expect(html).toContain(`href="${routeHref(route, unavailableId)}"`)
    }
    expect(html).toContain('href="#/accounts"')
    expect(html).not.toContain('#/accounts?')
    expect(html).not.toContain(`href="${routeHref('workspace', PROJECT.project_id)}"`)
  })

  it('connects one disclosure button to the complete navigation without duplicating controls', () => {
    const html = navigation('accounts')
    const controlsId = html.match(/aria-controls="([^"]+)"/)?.[1]
    expect(controlsId).toBeTruthy()
    expect(html).toContain(`id="${controlsId}"`)
    expect(html).toContain('aria-expanded="false"')
    expect(html).toContain(MESSAGES.zh.nav.openMenu)
    expect(html.split('class="sidebarMenuToggle"').length - 1).toBe(1)
    expect(html.split('class="navigationPanel"').length - 1).toBe(1)
    expect(html.split('class="sidebarLogout"').length - 1).toBe(1)
    expect(html.split('<select').length - 1).toBe(2)
    expect(html).not.toMatch(/class="navigationPanel"[^>]*hidden/)
  })

  it('initially hides the full mobile disclosure while keeping all operations in the same panel', () => {
    // 静的描画は初期属性だけを証明する。実 focus/resize/keyboard は browser 回帰で守る。
    vi.stubGlobal('window', { matchMedia: vi.fn().mockReturnValue({ matches: true }) })
    const html = navigation('workspace')
    expect(html).toMatch(/class="navigationPanel"[^>]*hidden=""/)
    expect(html).toContain('aria-expanded="false"')
    expect(html).toContain('href="#/accounts"')
    expect(html).toContain('class="sideNavProject"')
    expect(html).toContain('class="sidebarLogout"')
    expect(html).toContain('class="sidebarLanguage"')
  })

  it('provides the account entry outside the project scope', () => {
    /** 本人安全は Project 未所属でも到達でき、通常 USER の主導航に置く。 */
    const html = navigation('accounts')
    expect(html).toContain('href="#/accounts"')
    expect(html).toContain('账户与安全')
    expect(html.indexOf('账户与安全')).toBeLessThan(html.indexOf('当前项目'))
    expect(html).toContain('aria-current="page"')
  })

  it('splits navigation into platform and current-project groups with a single project switcher', () => {
    const html = navigation('workspace')

    // 平台組と当前项目組(select label が見出しを兼ねる)が両方描画される。
    expect(html).toContain('平台')
    expect(html).toContain('当前项目')
    expect(html).toContain('Quality Team · quality-team')
    // select は Project 切替と言語切替の 2 つだけ。Project 切替入口は「当前项目」1 箇所に限る。
    expect(html.split('<select').length - 1).toBe(2)
    expect(html.split('当前项目').length - 1).toBe(1)
    expect(html).toContain('界面语言')
    // 名前の不在を indexOf=-1 で見逃さず、実際の link が正しい分組にあることを守る。
    expect(html.indexOf('概览')).toBeLessThan(html.indexOf('当前项目'))
    expect(html.indexOf('项目管理')).toBeLessThan(html.indexOf('当前项目'))
    expect(html).toContain(`href="${routeHref('skills', PROJECT.project_id)}"`)
    expect(html.indexOf(`href="${routeHref('skills', PROJECT.project_id)}"`)).toBeLessThan(html.indexOf('当前项目'))
    expect(html.indexOf('当前项目')).toBeLessThan(html.indexOf('工作空间'))
    expect(html).toContain('aria-current="page"')
  })

  it('leads the project group with 工作空间 and nests the module submenu under it', () => {
    // 項目の主画面は工作空间。子菜单の遷移先も同じ画面なので、親子で強調が割れない。
    const html = navigation('workspace')

    expect(html.indexOf(`href="${routeHref('workspace', PROJECT.project_id)}"`)).toBeLessThan(html.indexOf('sideNavSub'))
    expect(html.indexOf('sideNavSub')).toBeLessThan(html.indexOf(`href="${routeHref('tasks', PROJECT.project_id)}"`))
    expect(html).toContain('品质分析')
  })

  it('lists only modules, with no catch-all entry', () => {
    // 「全部任务」入口は置かない。task が増えるほど全件表示は読めず、模块で見る導線と二重になる。
    const html = navigation('workspace')

    expect(html).not.toContain('全部任务')
    expect(html.split('sideNavSubItem').length - 1).toBe(MODULES.length)
  })

  it('hides the submenu entirely for a project without modules', () => {
    // 空の子菜单(guide 線だけ)を残すと、設定漏れではなく描画崩れに見える。
    expect(navigation('workspace', '', [])).not.toContain('sideNavSub')
  })

  it('keeps the module submenu current on every screen the filter reaches', () => {
    // 絞り込みが効く画面では子菜单の現在地を保ち、効かない画面では強調しない。
    expect(navigation('workspace')).toContain('aria-current="true"')
    expect(navigation('tasks')).toContain('aria-current="true"')
    expect(navigation('documents')).not.toContain('aria-current="true"')
    expect(navigation('schedules')).not.toContain('aria-current="true"')
  })
})
