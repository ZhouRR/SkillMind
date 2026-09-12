import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import type {
  ProjectModuleRecord,
  PublishedTaskRecord,
  TaskReadinessRecord,
} from '../../src/api'
import { ModuleNavList } from '../../src/components/AppNavigation'
import { SourceRequirementField, TaskReadinessPanel } from '../../src/components/TaskLaunchFields'
import { filterTasksByModule, type SourceRequirementChoice } from '../../src/lib/taskDraft'
import { WorkspacePage } from '../../src/pages/WorkspacePage'

const VERSION_ID = '00000000-0000-4000-8000-000000000061'

/** テスト用の module record を生成する。 */
function moduleRecord(overrides: Partial<ProjectModuleRecord> = {}): ProjectModuleRecord {
  return {
    module_id: '00000000-0000-4000-8000-000000000070',
    project_id: '00000000-0000-4000-8000-000000000020',
    name: '品质分析',
    description: '单票据品质分析模块',
    skills: [{
      skill_version_id: VERSION_ID,
      skill_id: '00000000-0000-4000-8000-000000000060',
      skill_key: 'repository-review',
      skill_name: 'Repository Review',
      version: '1.0.0',
      sort_order: 0,
    }],
    created_at: '2026-07-12T10:00:00Z',
    updated_at: '2026-07-12T10:00:00Z',
    ...overrides,
  }
}

/** テスト用の published task descriptor を生成する。 */
function task(overrides: Partial<PublishedTaskRecord> = {}): PublishedTaskRecord {
  return {
    skill_id: '00000000-0000-4000-8000-000000000060',
    skill_version_id: VERSION_ID,
    skill_key: 'repository-review',
    skill_name: 'Repository Review',
    version: '1.0.0',
    task_key: 'repository.review',
    task_id: '00000000-0000-4000-8000-0000000000aa',
    capability: 'repository.review/v1',
    title: '仓库评审',
    task_type: 'analysis',
    input_schema: {
      type: 'object',
      properties: { subject: { type: 'string', description: '评审对象' } },
      required: ['subject'],
      additionalProperties: false,
    },
    output_schema: { type: 'object', properties: { summary: { type: 'string' } } },
    input_schema_checksum: `sha256:${'1'.repeat(64)}`,
    output_schema_checksum: `sha256:${'2'.repeat(64)}`,
    task_output_schema: null,
    task_output_schema_checksum: null,
    workflow: 'default',
    view: 'standard',
    default_view: 'standard',
    compatibility_level: 'native',
    tool_requirements: [],
    published_at: '2026-07-12T09:00:00Z',
    readiness: null,
    last_run: null,
    ...overrides,
  }
}

describe('WorkspacePage layout', () => {
  it('keeps the rail as launcher plus a history shortcut and hosts the run form in a modal', () => {
    // 履歴の検索とページングは専用画面へ分離し、Workspace には入口だけを残して観測領域を圧迫しない。
    const html = renderToStaticMarkup(
      <WorkspacePage actorId="actor" csrfToken={'c'.repeat(32)} moduleId="" projectId="" />,
    )

    expect(html).toContain('新建执行')
    expect(html).toContain('执行历史')
    expect(html).toContain('historyShortcut')
    expect(html).toContain('href="#/history"')
    expect(html).toContain('modalOverlay')
    expect(html).toContain('hidden=""')
  })

  it('explains that the full history lives at project scope', () => {
    // 履歴画面は module filter の対象外なので、Workspace の入口でも作用域を明示する。
    const html = renderToStaticMarkup(
      <WorkspacePage actorId="actor" csrfToken={'c'.repeat(32)} moduleId="" projectId="" />,
    )

    expect(html).toContain('全项目')
  })
})

describe('filterTasksByModule', () => {
  it('keeps only tasks whose skill version is bound to the module', () => {
    const bound = task()
    const unbound = task({
      skill_version_id: '00000000-0000-4000-8000-000000000099',
      task_key: 'other.review',
      title: '其他评审',
    })

    expect(filterTasksByModule([bound, unbound], moduleRecord())).toEqual([bound])
  })

  it('returns the full catalog for a project without modules', () => {
    const tasks = [task()]

    expect(filterTasksByModule(tasks, null)).toBe(tasks)
  })
})

describe('ModuleNavList', () => {
  it('renders module entries only and marks the active one', () => {
    const html = renderToStaticMarkup(
      <ModuleNavList
        modules={[moduleRecord()]}
        activeModuleId={moduleRecord().module_id}
        onSelect={vi.fn()}
      />,
    )

    expect(html).toContain('品质分析')
    // 「全部」入口は持たない。常にどれか一つの模块が現在の作業範囲になる。
    expect(html.split('aria-current="true"').length - 1).toBe(1)
    expect(html.split('sideNavSubItem').length - 1).toBe(1)
  })

  it('highlights nothing on routes the module filter does not reach', () => {
    const html = renderToStaticMarkup(
      <ModuleNavList modules={[moduleRecord()]} activeModuleId={null} onSelect={vi.fn()} />,
    )

    expect(html).not.toContain('aria-current="true"')
  })
})

describe('SourceRequirementField', () => {
  /** 指定の候補数で source requirement を組み立てる。 */
  function requirement(overrides: Partial<SourceRequirementChoice> = {}): SourceRequirementChoice {
    return {
      key: 'issue_provider',
      kind: 'issue',
      access: 'read',
      required: true,
      options: [{ value: 'integration:abc', label: '我的 Redmine · redmine' }],
      ...overrides,
    }
  }

  /** SourceRequirementField を既定言語(zh)で静的描画する。 */
  function field(choice: SourceRequirementChoice, value = ''): string {
    return renderToStaticMarkup(
      <SourceRequirementField requirement={choice} value={value} onChange={vi.fn()} />,
    )
  }

  it('shows a friendly kind label instead of the raw requirement key', () => {
    // 技術 key(issue_provider)を主表示から外し、資源種別の友好名で見せる。
    const html = field(requirement({ options: [
      { value: 'a', label: 'A · redmine' },
      { value: 'b', label: 'B · redmine' },
    ] }))

    expect(html).toContain('问题跟踪来源')
    expect(html).not.toContain('>issue_provider<')
    // 追跡用に原 key は title へ退避する。
    expect(html).toContain('title="issue_provider"')
  })

  it('collapses a single required candidate into a static line with no dropdown', () => {
    // 元の選択と唯一の候補が一致する場合だけ、静的な使用先として表示する。
    const html = field(requirement(), 'integration:abc')

    expect(html).toContain('将使用：我的 Redmine · redmine')
    expect(html).not.toContain('<select')
  })

  it('does not label an empty selection as the single available source', () => {
    const html = field(requirement())
    expect(html).toContain('<select')
    expect(html).toContain('value="" selected=""')
    expect(html).not.toContain('将使用：我的 Redmine · redmine')
  })

  it('guides to Resources when a required source has no candidate', () => {
    const html = field(requirement({ options: [] }))

    expect(html).toContain('尚未接入可用来源')
    expect(html).not.toContain('<select')
  })

  it('keeps a dropdown when several candidates exist', () => {
    const html = field(requirement({ options: [
      { value: 'a', label: 'A · redmine' },
      { value: 'b', label: 'B · redmine' },
    ] }))

    expect(html).toContain('<select')
    expect(html).toContain('A · redmine')
    expect(html).toContain('B · redmine')
  })
})

describe('TaskReadinessPanel', () => {
  /** 就緒度 1 件分の readiness record を生成する。 */
  function readiness(overrides: Partial<TaskReadinessRecord['requirements'][number]> = {}): TaskReadinessRecord {
    return {
      level: 'RUNNABLE',
      requirements: [{
        key: 'issue',
        kind: 'issue',
        required: true,
        access: 'read',
        status: 'AVAILABLE',
        // backend の reason は status と 1:1 の英語固定文。表示されてはいけない。
        reason: 'The project has at least one bindable resource',
        capabilities: ['issue.read/v1'],
        selection_guidance: 'Project の ResourceBinding により解決される。',
        candidates: [{
          key: 'integration:abc',
          kind: 'issue',
          provider: 'redmine',
          label: 'Test Redmine',
          capabilities: ['issue.read/v1'],
          integration_id: '00000000-0000-4000-8000-000000000080',
          revision: '1',
          scope: {},
        }],
        ...overrides,
      }],
    }
  }

  it('states the readiness reason in the display language, not the API English', () => {
    /** status と 1:1 の英語固定文をそのまま出すと、画面が中日英の混在になる。 */
    const html = renderToStaticMarkup(<TaskReadinessPanel readiness={readiness()} />)

    expect(html).toContain('本项目已有可绑定的资源。')
    expect(html).not.toContain('The project has at least one bindable resource')
  })

  it('leads with the resource kind and keeps the contract key secondary', () => {
    const html = renderToStaticMarkup(<TaskReadinessPanel readiness={readiness()} />)

    expect(html).toContain('问题跟踪来源')
    expect(html).toContain('<code class="mono">issue</code>')
    // 種別と key を英語 2 連で並べる旧表示に戻っていないことを確認する。
    expect(html).not.toContain('issue · issue')
  })

  it('keeps Skill-authored selection guidance on its own line', () => {
    /** 選択指針は Skill 原文由来で言語が平台文言と異なりうるため、出典を行で分ける。 */
    const html = renderToStaticMarkup(<TaskReadinessPanel readiness={readiness()} />)

    expect(html).toContain('readinessGuidance')
    expect(html).toContain('Project の ResourceBinding により解決される。')
  })

  it('tells the user where to connect a resource when none is bound', () => {
    const html = renderToStaticMarkup(
      <TaskReadinessPanel readiness={readiness({ status: 'UNAVAILABLE', candidates: [] })} />,
    )

    expect(html).toContain('缺少资源')
    expect(html).toContain('请先在「资源管理」中接入')
  })
})
