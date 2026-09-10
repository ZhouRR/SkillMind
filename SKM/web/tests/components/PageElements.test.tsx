import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { ConfirmDialog, EmptyState, EventTimelineItem, LoadingSkeleton, ModalDialog, ProjectContextSelect } from '../../src/components/PageElements'
import type { RunEventRecord } from '../../src/api'
import { DEMO_PROJECT as PROJECT } from '../fixtures'
import { MESSAGES } from '../../src/lib/i18n/messages'

describe('ProjectContextSelect', () => {
  it.each(['idle', 'loading'] as const)('shows authorized archived detail while preserving the list %s status', (status) => {
    const html = renderToStaticMarkup(
      <ProjectContextSelect
        projectId={PROJECT.project_id}
        projectState={{ status }}
        currentProject={{ ...PROJECT, status: 'ARCHIVED' }}
        onSelect={vi.fn()}
      />,
    )
    expect(html).toContain(`Quality Team · ${MESSAGES.zh.elements.archivedProject}`)
    expect(html).toContain(`<option value="${PROJECT.project_id}" selected="">`)
    expect(html).toContain(`role="status">${MESSAGES.zh.elements.loadingProjects}</p>`)
    expect(html).not.toContain('<select disabled')
    expect(html).not.toContain(MESSAGES.zh.elements.projectUnavailable)
  })

  it('preserves the list failure alert even when authorized current detail is available', () => {
    const html = renderToStaticMarkup(
      <ProjectContextSelect
        projectId={PROJECT.project_id}
        projectState={{ status: 'error', message: MESSAGES.zh.elements.projectListFailed }}
        currentProject={{ ...PROJECT, status: 'ARCHIVED' }}
        onSelect={vi.fn()}
      />,
    )
    expect(html).toContain(`Quality Team · ${MESSAGES.zh.elements.archivedProject}`)
    expect(html).toContain(`role="alert">${MESSAGES.zh.elements.projectListFailed}</p>`)
    expect(html).not.toContain('<select disabled')
  })

  it('offers an explicit list retry only when the parent supplies a recovery action', () => {
    const html = renderToStaticMarkup(
      <ProjectContextSelect
        projectId={PROJECT.project_id}
        projectState={{ status: 'error', message: MESSAGES.zh.elements.projectListFailed }}
        currentProject={PROJECT}
        onSelect={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )
    expect(html).toContain(`type="button">${MESSAGES.zh.runHistory.retry}</button>`)
    expect(html).toContain(`role="alert">${MESSAGES.zh.elements.projectListFailed}</p>`)
    expect(html).toContain(`<option value="${PROJECT.project_id}" selected="">`)
  })

  it('does not offer another retry while the list request is already loading', () => {
    const html = renderToStaticMarkup(
      <ProjectContextSelect
        projectId={PROJECT.project_id}
        projectState={{ status: 'loading' }}
        currentProject={PROJECT}
        onSelect={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )
    expect(html).toContain('role="status"')
    expect(html).not.toContain('<button')
  })

  it('uses current detail once while retaining every other accessible candidate', () => {
    const other = { ...PROJECT, project_id: '00000000-0000-4000-8000-000000000011', name: 'Other accessible project' }
    const html = renderToStaticMarkup(
      <ProjectContextSelect
        projectId={PROJECT.project_id}
        projectState={{ status: 'ready', projects: [PROJECT, other] }}
        currentProject={{ ...PROJECT, name: 'Current authorized detail', status: 'ARCHIVED' }}
        onSelect={vi.fn()}
      />,
    )
    expect(html.split('<option').length - 1).toBe(2)
    expect(html).toContain(`Current authorized detail · ${MESSAGES.zh.elements.archivedProject}`)
    expect(html).toContain('Other accessible project')
    expect(html).not.toContain('Quality Team')
    expect(html).not.toContain('role="status"')
    expect(html).not.toContain('role="alert"')
  })

  it('does not reuse an old detail record as the newly selected project', () => {
    const html = renderToStaticMarkup(
      <ProjectContextSelect
        projectId="00000000-0000-4000-8000-000000000099"
        projectState={{ status: 'ready', projects: [] }}
        currentProject={PROJECT}
        onSelect={vi.fn()}
      />,
    )
    expect(html).not.toContain('Quality Team')
    expect(html).toContain(MESSAGES.zh.elements.projectUnavailable)
    expect(html).toContain('<select disabled')
  })

  it('keeps an unavailable explicit target selected instead of implying the first accessible project', () => {
    const unavailableId = '00000000-0000-4000-8000-000000000099'
    const html = renderToStaticMarkup(
      <ProjectContextSelect
        projectId={unavailableId}
        projectState={{ status: 'ready', projects: [PROJECT] }}
        onSelect={vi.fn()}
      />,
    )
    expect(html).toContain(`<option disabled="" value="${unavailableId}" selected="">${MESSAGES.zh.elements.projectUnavailable}</option>`)
    expect(html).toContain(`<option value="${PROJECT.project_id}">`)
    expect(html).not.toContain('<select disabled')
    expect(html).toContain('projectContextId')
  })

  it('distinguishes an unavailable target from an empty project list', () => {
    const html = renderToStaticMarkup(
      <ProjectContextSelect
        projectId={PROJECT.project_id}
        projectState={{ status: 'ready', projects: [] }}
        onSelect={vi.fn()}
      />,
    )
    expect(html).toContain(MESSAGES.zh.elements.projectUnavailable)
    expect(html).not.toContain(MESSAGES.zh.elements.noAccessibleProjects)
    expect(html).toContain('<select disabled')
  })

  it('retains the unresolved identity while showing a loading placeholder', () => {
    const html = renderToStaticMarkup(
      <ProjectContextSelect projectId={PROJECT.project_id} projectState={{ status: 'loading' }} onSelect={vi.fn()} />,
    )
    expect(html).toContain(`<option disabled="" value="${PROJECT.project_id}" selected="">`)
    expect(html).toContain(MESSAGES.zh.elements.loadingProjects)
    expect(html).not.toContain(MESSAGES.zh.elements.projectUnavailable)
  })

  it('marks an authorized archived record without presenting it as an active project', () => {
    const html = renderToStaticMarkup(
      <ProjectContextSelect
        projectId={PROJECT.project_id}
        projectState={{ status: 'ready', projects: [{ ...PROJECT, status: 'ARCHIVED' }] }}
        onSelect={vi.fn()}
      />,
    )
    expect(html).toContain(`Quality Team · ${MESSAGES.zh.elements.archivedProject}`)
    expect(html).toContain(`<option value="${PROJECT.project_id}" selected="">`)
    expect(html).not.toContain(MESSAGES.zh.elements.projectUnavailable)
  })

  it('offers projects by name and shows the selected id as a secondary line', () => {
    const html = renderToStaticMarkup(
      <ProjectContextSelect
        projectId={PROJECT.project_id}
        projectState={{ status: 'ready', projects: [PROJECT] }}
        onSelect={vi.fn()}
      />,
    )

    expect(html).toContain(`<option value="${PROJECT.project_id}" selected="">${PROJECT.name}</option>`)
    expect(html).not.toContain(PROJECT.key)
    // UUID は手入力欄ではなく補助行として表示する。
    expect(html).toContain('projectContextId')
    expect(html).toContain(PROJECT.project_id)
    expect(html).not.toContain('<input')
  })

  it('shows a placeholder and keeps the control disabled while nothing is selectable', () => {
    const loading = renderToStaticMarkup(
      <ProjectContextSelect projectId="" projectState={{ status: 'loading' }} onSelect={vi.fn()} />,
    )
    expect(loading).toContain('正在加载项目…')
    expect(loading).toContain('disabled')

    const empty = renderToStaticMarkup(
      <ProjectContextSelect
        projectId=""
        projectState={{ status: 'ready', projects: [] }}
        onSelect={vi.fn()}
      />,
    )
    expect(empty).toContain('没有可访问的项目')
  })

  it('surfaces a load failure as an alert', () => {
    const html = renderToStaticMarkup(
      <ProjectContextSelect
        projectId=""
        projectState={{ status: 'error', message: '无法加载项目列表。' }}
        onSelect={vi.fn()}
      />,
    )

    expect(html).toContain('项目列表加载失败')
    expect(html).toContain('无法加载项目列表。')
    expect(html).toContain('role="alert"')
  })
})

describe('LoadingSkeleton', () => {
  it('renders the requested placeholder rows as one labelled status region', () => {
    const html = renderToStaticMarkup(<LoadingSkeleton label="正在读取执行历史" rows={3} />)

    expect(html).toContain('role="status"')
    expect(html).toContain('aria-label="正在读取执行历史"')
    expect(html.split('skeletonRow').length - 1).toBe(3)
  })
})

describe('EventTimelineItem', () => {
  it('shows a friendly event label and keeps the contract code in details', () => {
    const event: RunEventRecord = {
      run_id: PROJECT.project_id,
      run_attempt_id: null,
      agent_session_id: null,
      sequence: 1,
      event_type: 'RUN_SNAPSHOT',
      occurred_at: '2026-07-28T12:00:00Z',
      payload: { status: 'RUNNING' },
      trace_id: null,
    }

    const html = renderToStaticMarkup(<EventTimelineItem event={event} />)

    expect(html).toContain('执行状态更新')
    expect(html).toContain('事件类型')
    expect(html).toContain('RUN_SNAPSHOT')
  })
})

describe('EmptyState', () => {
  it('keeps the reason text while adding only a decorative glyph', () => {
    const html = renderToStaticMarkup(<EmptyState text="当前项目还没有文档。" />)

    expect(html).toContain('当前项目还没有文档。')
    // Icon は装飾のため読み上げ対象から外す。
    expect(html).toContain('aria-hidden="true"')
  })
})

describe('ModalDialog', () => {
  /** 指定の開閉状態で modal を静的描画する。 */
  function dialog(open: boolean): string {
    return renderToStaticMarkup(
      <ModalDialog open={open} title="新建执行" onClose={vi.fn()}>
        <p>表单内容</p>
      </ModalDialog>,
    )
  }

  it('keeps its content mounted while closed so page tests can still assert on it', () => {
    // 条件描画にすると renderToStaticMarkup + toContain の頁面測試が一斉に壊れる。
    const closed = dialog(false)

    expect(closed).toContain('表单内容')
    expect(closed).toContain('hidden=""')
  })

  it('exposes dialog semantics and a close affordance when open', () => {
    const open = dialog(true)

    expect(open).toContain('role="dialog"')
    expect(open).toContain('aria-modal="true"')
    expect(open).toContain('aria-label="新建执行"')
    expect(open).toContain('关闭')
    expect(open).not.toContain('hidden=""')
  })

  it('places caller-supplied meta and actions in the header', () => {
    const html = renderToStaticMarkup(
      <ModalDialog open title="report.md" meta="12 KB · text/markdown" actions={<a href="#x">下载</a>} onClose={vi.fn()}>
        <pre>正文</pre>
      </ModalDialog>,
    )

    expect(html).toContain('12 KB · text/markdown')
    expect(html).toContain('下载')
  })
})

describe('ConfirmDialog', () => {
  /** 指定の確認要求で dialog を静的描画する。null は閉じた状態。 */
  function render(request: Parameters<typeof ConfirmDialog>[0]['request']): string {
    return renderToStaticMarkup(
      <ConfirmDialog request={request} onConfirm={vi.fn()} onCancel={vi.fn()} />,
    )
  }

  it('stays closed and leaks no operation label while nothing is being confirmed', () => {
    // 常時 mount のため、閉じている間に前回の実行 label が残ると誤操作を招く。
    const html = render(null)

    expect(html).toContain('hidden=""')
    expect(html).toContain('取消')
    expect(html).not.toContain('彻底删除')
  })

  it('states the operation, its consequence and repeats the verb on the action button', () => {
    const html = render({
      title: '彻底删除',
      message: '将从组织技能库中彻底删除已废止的版本。是否继续?',
      confirmLabel: '彻底删除',
      destructive: true,
    })

    expect(html).toContain('role="dialog"')
    expect(html).toContain('将从组织技能库中彻底删除已废止的版本。')
    // 取り消せない操作は danger 実心 button で示す(一覧行の控えめな dangerButton とは別)。
    expect(html).toContain('destructiveButton')
    expect(html).not.toContain('hidden=""')
  })

  it('uses the neutral primary button for reversible operations', () => {
    const html = render({ title: '归档', message: '历史审计数据不会被删除。', confirmLabel: '归档' })

    expect(html).toContain('primaryButton')
    expect(html).not.toContain('destructiveButton')
  })
})
