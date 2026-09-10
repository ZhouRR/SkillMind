import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { HomePage } from '../../src/pages/HomePage'
import { DEMO_PROJECT as PROJECT } from '../fixtures'

/** 概览を静的 markup へ描画する。 */
function render(projectId: string): string {
  return renderToStaticMarkup(
    <HomePage
      metaState={{
        status: 'ready',
        meta: { name: 'Skillmind API', version: '0.1.0', phase: 'M0', task: 'generic', ingress: 'traefik' },
      }}
      project={projectId ? PROJECT : null}
      projectId={projectId}
    />,
  )
}

describe('HomePage', () => {
  it('gives the project a single primary task entry without invented totals', () => {
    const html = render(PROJECT.project_id)
    expect(html).toContain('class="homeHero"')
    expect(html).toContain('class="homeAttention"')
    expect(html).not.toContain('class="statGrid"')
    expect(html).not.toContain('只读工具')
    expect(html).not.toContain('class="statCard"')
  })

  it('leads with what is waiting for the user', () => {
    // Run は待機中に lease も wall timeout も持たない。気付かれない待機はそのまま停止になるため、
    // 「待你处理」は最近执行より前に置く。
    const html = render(PROJECT.project_id)

    const pendingAt = html.indexOf('待你处理')
    const recentAt = html.indexOf('最近执行')
    expect(pendingAt).toBeGreaterThan(-1)
    expect(recentAt).toBeGreaterThan(-1)
    expect(pendingAt).toBeLessThan(recentAt)
  })

  it('shows the project name as the primary value', () => {
    const html = render(PROJECT.project_id)

    expect(html).toContain('Quality Team')
    expect(html).toContain(PROJECT.project_id)
  })

  it('does not duplicate the sidebar navigation as cards', () => {
    // 以前ここに全画面への card 一覧があったが、それは sidebar の複製で、概览が答えるべき
    // 「私が今なにをすべきか」には何も足していなかった。
    const html = render(PROJECT.project_id)

    expect(html).not.toContain('moduleIcon')
    expect(html).not.toContain('moduleCard')
  })

  it('sends the user to the task center rather than a bare workspace', () => {
    // 「次に何を走らせるか」を選ぶ場所は任务中心。工作空间は今の一つを観る場所。
    const html = render(PROJECT.project_id)

    expect(html).toContain('查看任务')
    expect(html).toContain(`#/tasks?project=${PROJECT.project_id}`)
  })

  it('explains how to select a project when none is chosen', () => {
    const html = render('')

    expect(html).toContain('未选择')
    expect(html).toContain('选择项目后，这里会显示最近的执行记录。')
  })
})
