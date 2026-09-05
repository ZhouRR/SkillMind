import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import type { ProjectModuleRecord } from '../../src/api'
import type { AppRoute } from '../../src/lib/routing'
import { AppNavigation } from '../../src/components/AppNavigation'
import { DEMO_PROJECT, demoUser } from '../fixtures'

const PROJECT = DEMO_PROJECT

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
    />,
  )
}

describe('AppNavigation grouping', () => {
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
    // 分組順:平台(概览・Skills 解析・项目管理)が Project 切替より前、工作空间等が後。
    expect(html.indexOf('概览')).toBeLessThan(html.indexOf('当前项目'))
    expect(html.indexOf('项目管理')).toBeLessThan(html.indexOf('当前项目'))
    // Skills 解析は資産を作る平台能力として platform 組へ移設した。
    expect(html.indexOf('Skills 解析')).toBeLessThan(html.indexOf('当前项目'))
    expect(html.indexOf('当前项目')).toBeLessThan(html.indexOf('工作空间'))
    expect(html).toContain('aria-current="page"')
  })

  it('leads the project group with 工作空间 and nests the module submenu under it', () => {
    // 項目の主画面は工作空间。子菜单の遷移先も同じ画面なので、親子で強調が割れない。
    const html = navigation('workspace')

    expect(html.indexOf('href="#/workspace"')).toBeLessThan(html.indexOf('sideNavSub'))
    expect(html.indexOf('sideNavSub')).toBeLessThan(html.indexOf('href="#/tasks"'))
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
  })
})
