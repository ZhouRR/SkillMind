import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { ApiProblemError } from '../../src/api'
import { MESSAGES } from '../../src/lib/i18n/messages'
import {
  ProjectsPage,
  projectDeleteErrorMessage,
  staleBindingIds,
} from '../../src/pages/ProjectsPage'
import { DEMO_PROJECT as PROJECT, demoSession } from '../fixtures'

describe('ProjectsPage layout', () => {
  it('splits projects, archive and modules into always-mounted page tabs', () => {
    const html = renderToStaticMarkup(page('ADMIN'))

    expect(html).toContain('role="tablist"')
    expect(html).toContain('模块配置')
    expect(html).toContain('归档项目')
    // 非活性 panel は hidden で DOM に残す(本 file の他断言はこの前提に依存する)。
    expect(html).toContain('hidden=""')
  })

  it('provides an ADMIN members tab with an associated panel but no eager member subtree', () => {
    const html = renderToStaticMarkup(page('ADMIN'))
    expect(html).toContain('id="project-tab-members"')
    expect(html).toContain('aria-controls="project-panel-members"')
    expect(html).toContain('id="project-panel-members" aria-labelledby="project-tab-members"')
    expect(html).toContain('data-project-tab="members"')
    expect(html).toContain(MESSAGES.zh.projectMembers.tab)
    expect(html).not.toContain('data-project-members=')
  })

  it('does not expose a members tab or panel to USER', () => {
    const html = renderToStaticMarkup(page('USER'))
    expect(html).not.toContain('project-tab-members')
    expect(html).not.toContain('project-panel-members')
    expect(html).not.toContain('data-project-members=')
  })
})

describe('ProjectsPage role projection', () => {
  it('shows accessible projects but hides management controls from USER', () => {
    const html = renderToStaticMarkup(page('USER'))

    expect(html).toContain('Quality Team')
    expect(html).toContain('普通成员只能访问已加入的活动项目')
    expect(html).not.toContain('创建项目')
    expect(html).not.toContain('>归档<')
  })

  it('shows create, edit and archive controls to ADMIN', () => {
    const html = renderToStaticMarkup(page('ADMIN'))

    expect(html).toContain('创建项目')
    expect(html).toContain('>归档<')
    // 作成後に名称・説明・保持日数を直せないと、作り直すしか手が無くなる。
    expect(html).toContain('>编辑<')
  })

  it('surfaces the archived project section so a reserved key can be recovered', () => {
    /** Archive は key の一意制約を解かない。一覧から消えたまま同じ key で作り直せない
        詰まりを避けるため、ADMIN には復元と削除の導線を出し続ける。 */
    const html = renderToStaticMarkup(page('ADMIN'))

    expect(html).toContain('已归档项目')
    expect(html).toContain('归档不会释放项目 Key')
  })

  it('hides the archived project section from USER', () => {
    expect(renderToStaticMarkup(page('USER'))).not.toContain('已归档项目')
  })

  it('renders the project module configuration section with the admin form', () => {
    const html = renderToStaticMarkup(page('ADMIN'))

    expect(html).toContain('项目模块')
    expect(html).toContain('创建模块')
    expect(html).toContain('绑定已发布技能')
  })

  it('keeps module configuration read-only for USER', () => {
    const html = renderToStaticMarkup(page('USER'))

    expect(html).toContain('项目模块')
    expect(html).not.toContain('创建模块')
    expect(html).toContain('模块由管理员配置')
  })
})

/** 指定 system role の Project page を生成する。 */
function page(role: 'ADMIN' | 'USER') {
  const session = demoSession(role)
  return <ProjectsPage
    onProjectArchived={vi.fn()}
    onProjectChanged={vi.fn()}
    projectId={PROJECT.project_id}
    projectState={{ status: 'ready', projects: [PROJECT] }}
    session={session}
    setProjectId={vi.fn()}
  />
}

describe('staleBindingIds', () => {
  const options = [
    { skill_version_id: 'cfb24bbb', label: 'ticket-quality v0.1.1' },
  ]

  it('surfaces bound versions that dropped out of the candidate list', () => {
    // 廃止/無効化された旧版は候補から消えるが束縛には残る。描画対象として返さないと
    // checkbox が無く外せず、保存が永久に失敗する(実際に発生した死結)。
    expect(staleBindingIds(['82dd4756', 'cfb24bbb'], options)).toEqual(['82dd4756'])
  })

  it('returns nothing when every selection is still a candidate', () => {
    expect(staleBindingIds(['cfb24bbb'], options)).toEqual([])
    expect(staleBindingIds([], options)).toEqual([])
  })
})

describe('projectDeleteErrorMessage', () => {
  const messages = MESSAGES.zh

  it('explains that run history blocks deletion instead of showing the raw detail', () => {
    // 監査記録は削除で失えないため、利用者には「消せない理由」を語彙で伝える。
    const error = new ApiProblemError('Project still has 3 run(s)', 409, 'project_delete_blocked_by_runs')

    expect(projectDeleteErrorMessage(error, messages)).toBe(messages.projects.deleteBlockedByRuns)
  })

  it('points at the archive step when the project is still active', () => {
    const error = new ApiProblemError('not archived', 409, 'project_delete_requires_archive')

    expect(projectDeleteErrorMessage(error, messages)).toBe(messages.projects.deleteNeedsArchive)
  })

  it.each(['zh', 'ja', 'en'] as const)('explains the membership audit deletion guard safely in %s', (language) => {
    const error = new ApiProblemError('private audit detail', 409, 'project_delete_blocked_by_member_audit')
    const message = projectDeleteErrorMessage(error, MESSAGES[language])
    expect(message).toBe(MESSAGES[language].projects.deleteBlockedByMemberAudit)
    expect(message).not.toContain('private audit detail')
  })

  it('classifies unexpected writes as unknown without exposing server text', () => {
    expect(projectDeleteErrorMessage(new Error('private network detail'), messages)).toBe(messages.projectManagement.failures.unknown)
  })
})
