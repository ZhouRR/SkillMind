import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import type { InterpretationExecutionRecord, SkillParseResult, SkillVersionRecord } from '../../src/api'
import { LanguageProvider } from '../../src/i18n'
import type { UiLanguage } from '../../src/lib/i18n/messages'
import {
  InterpretStreamView,
  InterpretationExecutionView,
  SkillLibraryPanel,
  SkillParseSummary,
  SkillsPage,
  SkillVersionDetail,
  UploadedSourceFiles,
  readUploadedSourcePreview,
} from '../../src/pages/SkillsPage'

const PREVIEW = {
  normalized_package: {
    package_format: 'skillmind.normalized/v1',
    source: { type: 'directory', content_hash: 'sha256:source', detected_adapter: 'directory-skill/v1', files: [] },
    metadata: { name: 'Repository Review', description: 'Review a repository change.', argument_hint: null },
    resources: { scripts: [], references: ['references/checklist.md'], assets: [] },
    declared_tools: ['Read', 'Grep'],
    diagnostics: [],
  },
  runtime_manifest_draft: { identity: { skill_key: 'repository-review' } },
  capability_blueprint: {
    blueprint_version: 'skillmind.capability-blueprint/v1',
    capabilities: [{ key: 'repository.review', title: 'Repository Review' }],
    tasks: [
      {
        key: 'review-file',
        capability: 'repository.review',
        objective: 'Complete the Repository Review task declared by the Skill.',
      },
    ],
    resource_requirements: [
      {
        key: 'repository-source',
        kind: 'repository',
        required: true,
        access: 'read',
        capabilities: ['repository.read/v1'],
      },
    ],
    guidance: {
      required_rules: [],
      recommended_steps: [],
      quality_criteria: [],
      prohibited_actions: [],
    },
    interaction_points: [],
    effect_intents: [],
  },
}

const REPORT = {
  report_version: 'skillmind.skill-interpretation-report/v1',
  summary: 'Adapted repository review skill with read-only evidence.',
  compatibility_level: 'adapted',
  confidence: { capabilities: 0.8, tasks: 0.75, resources: 0.7, tools: 0.72, workflows: 0.6, schemas: 0.65 },
  assumptions: [{ key: 'read_only', text: 'Skill only reads repository content.' }],
  questions: [
    { key: 'target_branch', text: 'Which branch is reviewed by default?', required: true },
    { key: 'depth', text: 'Should the review include history?', required: false },
  ],
  diagnostics: [{
    severity: 'warning',
    code: 'unmapped_reference',
    message: 'A reference could not be resolved.',
    path: 'references/checklist.md',
    line: 12,
  }],
  source_traces: [
    { target: '/capabilities/0', path: 'SKILL.md', line: 3, reason: 'Repository read capability inferred.' },
  ],
  unmapped_references: [{ path: 'references/legacy.md', reason: 'File not present.' }],
}

/** テスト用の PREVIEW_READY interpretation 実行 record を生成する。 */
function execution(overrides: Partial<InterpretationExecutionRecord> = {}): InterpretationExecutionRecord {
  return {
    interpretation_id: '00000000-0000-4000-8000-000000000050',
    skill_source_id: '00000000-0000-4000-8000-000000000040',
    organization_id: '00000000-0000-4000-8000-000000000002',
    status: 'PREVIEW_READY',
    origin: 'model',
    model: 'claude-sonnet-5',
    interpreter_version: 'skillmind-skill-interpreter/1.0.0',
    execution_key: 'sha256:execkey',
    error_code: null,
    compatibility_level: 'adapted',
    confidence: 0.72,
    summary: REPORT.summary,
    created_at: '2026-07-08T10:00:00Z',
    preview: PREVIEW as unknown as InterpretationExecutionRecord['preview'],
    report: REPORT as unknown as InterpretationExecutionRecord['report'],
    reused: false,
    parent_interpretation_id: null,
    adjustment: null,
    diff: { has_changes: false },
    ...overrides,
  }
}

/** InterpretationExecutionView を静的 markup へ描画する。 */
function renderView(record: InterpretationExecutionRecord, language: UiLanguage = 'zh'): string {
  return renderToStaticMarkup(
    <LanguageProvider language={language}>
      <InterpretationExecutionView
        execution={record}
        instruction=""
        onInstructionChange={vi.fn()}
        onAdjust={vi.fn()}
        onRegenerate={vi.fn()}
        onCreateDraft={vi.fn()}
        adjustState={{ status: 'idle' }}
        versionBusy={false}
      />
    </LanguageProvider>,
  )
}

describe('InterpretStreamView', () => {
  it('shows the actual prompt and streamed model output like a run', () => {
    const html = renderToStaticMarkup(
      <InterpretStreamView
        prompt={'SYSTEM PROMPT BODY\n\n---\n\n{"interpreter":{}}'}
        output="Analyzing the declared tools…"
      />,
    )

    expect(html).toContain('模型解释进行中')
    expect(html).toContain('发送给模型的提示词')
    expect(html).toContain('SYSTEM PROMPT BODY')
    expect(html).toContain('Analyzing the declared tools…')
    // 出力があるときは待機文言ではなく本文 + cursor を出す。
    expect(html).toContain('streamCursor')
    expect(html).not.toContain('正在等待模型输出')
  })

  it('shows a waiting hint before any model output arrives', () => {
    const html = renderToStaticMarkup(<InterpretStreamView prompt="" output="" />)

    expect(html).toContain('正在等待模型输出')
    // prompt 未達なら details は描画しない。
    expect(html).not.toContain('发送给模型的提示词')
  })
})

describe('SkillsPage entry affordances', () => {
  it('always offers both the inline parse form and the directory upload entry', () => {
    // 目录 upload 入口は P10 交付機能。回帰で静かに消えないことを描画で保証する。
    const html = renderToStaticMarkup(<SkillsPage csrfToken={'c'.repeat(32)} projectId="00000000-0000-4000-8000-000000000020" />)

    expect(html).toContain('解析技能')
    expect(html).toContain('选择技能目录')
    expect(html).toContain('aria-label="上传技能目录"')
    expect(html).toContain('skillUpload')
  })

  it('keeps the organization library usable without a selected project', () => {
    const html = renderToStaticMarkup(
      <SkillsPage csrfToken={'c'.repeat(32)} projectId="" />,
    )

    expect(html).toContain('解析技能')
    expect(html).toContain('组织技能库')
    expect(html).toContain('无需选择项目')
    expect(html).not.toContain('请先在侧栏选择项目')
  })

  it('splits the workbench and the organization library into page tabs', () => {
    // 作業台と組織 library は tab で切り替える。非活性側も hidden で DOM に残す
    // (上の toContain 断言群はこの前提に依存する)。
    const html = renderToStaticMarkup(
      <SkillsPage csrfToken={'c'.repeat(32)} projectId="" />,
    )

    expect(html).toContain('role="tablist"')
    expect(html).toContain('导入技能')
    expect(html).toContain('class="detailDisclosure skillTextSource"')
    expect(html).toContain('role="tabpanel" hidden=""')
    expect(html).toContain('hidden=""')
  })
})

describe('readUploadedSourcePreview', () => {
  it('reads text content, marks binaries, and pins SKILL.md first', async () => {
    const files = [
      new File(['# Rules\n'], 'rules.md', { type: 'text/markdown' }),
      new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'logo.png', { type: 'image/png' }),
      new File(['---\nname: Probe\n---\n'], 'SKILL.md', { type: 'text/markdown' }),
    ]

    const entries = await readUploadedSourcePreview(files)

    expect(entries.map((entry) => entry.path)).toEqual(['SKILL.md', 'logo.png', 'rules.md'])
    expect(entries[0]).toMatchObject({ kind: 'text', content: '---\nname: Probe\n---\n' })
    expect(entries[1]).toMatchObject({ kind: 'binary', size: 4 })
    expect(entries[1]?.content).toBeUndefined()
  })

  it('removes the browser-selected directory name from preview paths', async () => {
    const skill = new File(['# Skill\n'], 'SKILL.md', { type: 'text/markdown' })
    const rules = new File(['# Rules\n'], 'rules.md', { type: 'text/markdown' })
    Object.defineProperty(skill, 'webkitRelativePath', { value: 'repository-review/SKILL.md' })
    Object.defineProperty(rules, 'webkitRelativePath', { value: 'repository-review/references/rules.md' })

    const entries = await readUploadedSourcePreview([rules, skill])

    expect(entries.map((entry) => entry.path)).toEqual(['SKILL.md', 'references/rules.md'])
  })
})

describe('UploadedSourceFiles display', () => {
  it('shows text content read-only, binary rows, and the clear affordance', () => {
    const html = renderToStaticMarkup(
      <UploadedSourceFiles
        files={[
          { path: 'probe/SKILL.md', size: 20, kind: 'text', content: '# Probe body' },
          { path: 'probe/assets/logo.png', size: 4096, kind: 'binary' },
          { path: 'probe/big.md', size: 500_000, kind: 'oversized' },
        ]}
        onClear={vi.fn()}
      />,
    )

    expect(html).toContain('已上传 3 个文件')
    expect(html).toContain('# Probe body')
    expect(html).toContain('probe/assets/logo.png')
    expect(html).toContain('二进制资源')
    expect(html).toContain('文本过大')
    expect(html).toContain('清除，改用手动输入')
    // SKILL.md は初期展開、他は畳んだまま。
    expect(html).toContain('open=""')
  })
})

describe('SkillParseSummary diagnostics', () => {
  it('shows a diagnostic duplicated across package and manifest compatibility only once', () => {
    // Parser package の診断は manifest compatibility 側へ複製されるため、二重表示しないことを確認する。
    const shared = {
      severity: 'info',
      code: 'declared_tools_not_authorized',
      message: 'Declared tools are recorded but not authorized.',
      path: 'SKILL.md',
      line: 4,
    }
    const manifestOnly = {
      severity: 'warning',
      code: 'unmapped_reference',
      message: 'A reference could not be resolved.',
      path: null,
      line: null,
    }
    const result = {
      normalized_package: { ...PREVIEW.normalized_package, diagnostics: [shared] },
      runtime_manifest_draft: {
        identity: { skill_key: 'repository-review', source_hash: 'sha256:s', interpreter_version: 'v1' },
        compatibility: { level: 'native', confidence: 0.9, diagnostics: [shared, manifestOnly] },
        tools: [],
        extensions: {},
      },
    } as unknown as SkillParseResult

    const html = renderToStaticMarkup(<SkillParseSummary result={result} />)

    expect(html.split('declared_tools_not_authorized').length - 1).toBe(1)
    expect(html).toContain('unmapped_reference')
  })
})

describe('InterpretationExecutionView display', () => {
  it('renders summary, per-field confidence, assumptions, questions, traces and diagnostics', () => {
    const html = renderView(execution())

    expect(html).toContain(REPORT.summary)
    expect(html).toContain('capabilities')
    expect(html).toContain('0.80')
    expect(html).toContain('Skill only reads repository content.')
    // 必答 question にはマーカーが付き、任意 question はそのまま出る。
    expect(html).toContain('Which branch is reviewed by default?（必答）')
    expect(html).toContain('Should the review include history?')
    expect(html).toContain('/capabilities/0')
    expect(html).toContain('SKILL.md:3')
    expect(html).toContain('A reference could not be resolved.')
    // 親のない初回解釈は diff 対比なしを明示する。
    expect(html).toContain('首次解释')
  })

  it('shows parent lineage, the adjustment instruction and structured diff lines for a reinterpretation', () => {
    const html = renderView(execution({
      interpretation_id: '00000000-0000-4000-8000-000000000051',
      parent_interpretation_id: '00000000-0000-4000-8000-000000000050',
      adjustment: { instruction: 'Only review a single file.' },
      diff: {
        has_changes: true,
        tasks: { added: [], removed: [], changed: ['repository.review'] },
        confidence: { changed: { tasks: { from: 0.75, to: 0.8 } } },
      },
    }))

    expect(html).toContain('00000000-0000-4000-8000-000000000050')
    expect(html).toContain('Only review a single file.')
    expect(html).toContain('有差异')
    expect(html).toContain('tasks: +0 / -0 / ~1')
    expect(html).toContain('confidence: 1 项变更')
  })

  it('marks a failed interpretation and does not offer a publishable draft path', () => {
    const html = renderView(execution({ status: 'FAILED', error_code: 'provider_timeout', report: null }))

    expect(html).toContain('解释失败：provider_timeout')
    expect(html).toContain('失败不会产出可发布的草稿')
    // 失敗時は report 本文（summary・source trace・confidence）を一切描画しない。
    expect(html).not.toContain(REPORT.summary)
    expect(html).not.toContain('/capabilities/0')
    expect(html).not.toContain('校验错误详情')
  })

  it.each([
    ['zh', '校验错误详情'],
    ['ja', '検証エラーの詳細'],
    ['en', 'Validation error details'],
  ] as const)('shows failed validation details collapsed by default in %s', (language, label) => {
    const html = renderView(execution({
      status: 'FAILED', error_code: 'schema_validation_failed', report: null,
      validation_attempts: ['path=/report validator=required', 'path=/tasks validator=type'],
    }), language)

    expect(html).toContain(`<details class="rawResult"><summary>${label}</summary><ol>`)
    expect(html).toContain('<li><pre>path=/report validator=required</pre></li><li><pre>path=/tasks validator=type</pre></li>')
    expect(html.indexOf('schema_validation_failed')).toBeLessThan(html.indexOf(label))
  })

  it('renders validation diagnostics as escaped text in at most two ordered entries', () => {
    const html = renderView(execution({
      status: 'FAILED', error_code: 'schema_validation_failed', report: null,
      validation_attempts: ['<script>alert("fixture")</script> & text', '[link](javascript:alert(1))', 'third-attempt-hidden'],
    }))

    expect(html).toContain('<pre>&lt;script&gt;alert(&quot;fixture&quot;)&lt;/script&gt; &amp; text</pre>')
    expect(html).toContain('<pre>[link](javascript:alert(1))</pre>')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<a href="javascript:')
    expect(html).not.toContain('third-attempt-hidden')
  })

  it.each(['PREVIEW_READY', 'UNKNOWN', 'RUNNING'])('hides validation details for %s records', (status) => {
    const html = renderView(execution({ status, validation_attempts: ['diagnostic-hidden'] }))

    expect(html).not.toContain('校验错误详情')
    expect(html).not.toContain('diagnostic-hidden')
  })

  it('does not invent diagnostics for an empty validation attempt list', () => {
    const html = renderView(execution({
      status: 'FAILED', error_code: 'schema_validation_failed', report: null, validation_attempts: [],
    }))

    expect(html).toContain('schema_validation_failed')
    expect(html).not.toContain('校验错误详情')
  })

  it('shows the capability blueprint dimensions instead of only schema and gate', () => {
    const html = renderView(execution())

    expect(html).toContain('能力蓝图')
    expect(html).toContain('Repository Review')
    expect(html).toContain('Complete the Repository Review task declared by the Skill.')
    // 資源前提は必需性・access・capability hint まで逐項表示する。
    expect(html).toContain('repository-source')
    expect(html).toContain('必需')
    expect(html).toContain('repository.read/v1')
  })

  it('states that an effect intent is not a permission', () => {
    const blueprint = {
      ...PREVIEW.capability_blueprint,
      resource_requirements: [
        { key: 'review_tracker', kind: 'issue', required: false, access: 'write' },
      ],
      effect_intents: [
        {
          key: 'update-tracker',
          mode: 'apply',
          resource_key: 'review_tracker',
          operation: 'Record the review outcome on the tracked issue.',
          risk: 'medium',
          approval_mode: 'ask',
        },
      ],
    }
    const record = execution()
    const html = renderView({
      ...record,
      preview: { ...record.preview, capability_blueprint: blueprint },
    } as unknown as InterpretationExecutionRecord)

    expect(html).toContain('外部变更意图')
    expect(html).toContain('apply · risk medium')
    expect(html).toContain('实际写入仍需注册对应外部系统并经用户批准')
  })

  it('splits report, blueprint, contracts and diff into always-mounted detail tabs', () => {
    // 詳細 4 区分は tab 化した。非活性 tab も hidden panel として mount されたままなので、
    // 本 describe の内容断言(報告・蓝图・diff)は tab 選択に依存しない。
    const html = renderView(execution())

    expect(html).toContain('解释报告')
    expect(html).toContain('生成的任务契约')
    expect(html.split('role="tabpanel"').length - 1).toBe(4)
  })

  it('omits blueprint sections that the Skill did not declare', () => {
    const record = execution()
    const html = renderView({
      ...record,
      preview: {
        ...record.preview,
        capability_blueprint: {
          ...PREVIEW.capability_blueprint,
          resource_requirements: [],
        },
      },
    } as unknown as InterpretationExecutionRecord)

    expect(html).not.toContain('资源前提')
    expect(html).not.toContain('外部变更意图')
    expect(html).not.toContain('必需规则')
  })
})

/** テスト用の SkillVersion record を生成する。 */
function version(overrides: Partial<SkillVersionRecord> = {}): SkillVersionRecord {
  return {
    skill_id: '00000000-0000-4000-8000-000000000060',
    skill_version_id: '00000000-0000-4000-8000-000000000061',
    skill_source_id: '00000000-0000-4000-8000-000000000040',
    interpretation_id: '00000000-0000-4000-8000-000000000050',
    organization_id: '00000000-0000-4000-8000-000000000002',
    skill_key: 'repository-review',
    name: 'Repository Review',
    description: 'リポジトリ変更を読み取り専用で分析する。',
    version: '1.0.0',
    status: 'DRAFT',
    manifest_checksum: `sha256:${'a'.repeat(64)}`,
    manifest: {},
    gate_passed: true,
    gate_findings: [],
    interpretation_diff: { has_changes: false },
    created_at: '2026-07-08T10:05:00Z',
    published_by: null,
    published_at: null,
    ...overrides,
  }
}

describe('SkillVersionDetail gate display', () => {
  it('enables publishing when the deterministic gate passes', () => {
    const html = renderToStaticMarkup(<SkillVersionDetail version={version()} onPublish={vi.fn()} />)

    expect(html).toContain('通过')
    expect(html).toContain('发布版本')
    expect(html).not.toContain('请先解决未通过的检查，再发布。')
  })

  it('renders gate findings and blocks publishing when a hard gate fails', () => {
    const html = renderToStaticMarkup(
      <SkillVersionDetail
        version={version({
          gate_passed: false,
          gate_findings: [{
            code: 'tool_not_registered',
            severity: 'error',
            message: 'Declared tool is not in the registry.',
            path: '/tools/0',
          }],
        })}
        onPublish={vi.fn()}
      />,
    )

    expect(html).toContain('Declared tool is not in the registry.')
    expect(html).toContain('请先解决未通过的检查，再发布。')
    // gate 未通過の DRAFT は publish ボタンが disabled になる。
    expect(html).toContain('disabled')
  })
})

describe('SkillLibraryPanel project enablement', () => {
  it('offers an exact published version for explicit project enablement', () => {
    const published = version({ status: 'PUBLISHED' })
    const html = renderToStaticMarkup(
      <SkillLibraryPanel
        libraryState={{ status: 'ready', versions: [published] }}
        enablementState={{ status: 'ready', enablements: [] }}
        projectId="00000000-0000-4000-8000-000000000020"
        busyVersionId={null}
        onDeprecate={vi.fn()}
        onDelete={vi.fn()}
        onEnable={vi.fn()}
        onDisable={vi.fn()}
      />,
    )

    expect(html).toContain('为项目启用')
    expect(html).toContain(published.skill_key)
    expect(html).toContain('启用后，技能中的任务才会出现在项目中')
  })

  it('shows an active relationship as disableable instead of re-enabling it', () => {
    const published = version({ status: 'PUBLISHED' })
    const html = renderToStaticMarkup(
      <SkillLibraryPanel
        libraryState={{ status: 'ready', versions: [published] }}
        enablementState={{
          status: 'ready',
          enablements: [{
            project_id: '00000000-0000-4000-8000-000000000020',
            organization_id: published.organization_id,
            skill_version: published,
            enabled_by: '00000000-0000-4000-8000-000000000070',
            enabled_at: '2026-07-10T02:00:00Z',
            disabled_at: null,
          }],
        }}
        projectId="00000000-0000-4000-8000-000000000020"
        busyVersionId={null}
        onDeprecate={vi.fn()}
        onDelete={vi.fn()}
        onEnable={vi.fn()}
        onDisable={vi.fn()}
      />,
    )

    expect(html).toContain('项目已启用')
    expect(html).toContain('从项目停用')
    expect(html).not.toContain('为项目启用')
  })
})

describe('SkillLibraryPanel version identity and cleanup', () => {
  /** 指定 status の library panel を静的 HTML へ描画する。 */
  function renderLibrary(status: SkillVersionRecord['status']): string {
    return renderToStaticMarkup(
      <SkillLibraryPanel
        libraryState={{ status: 'ready', versions: [version({ status })] }}
        enablementState={{ status: 'ready', enablements: [] }}
        projectId="00000000-0000-4000-8000-000000000020"
        busyVersionId={null}
        onDeprecate={vi.fn()}
        onDelete={vi.fn()}
        onEnable={vi.fn()}
        onDisable={vi.fn()}
      />,
    )
  }

  it('shows the SKILL.md description instead of the internal version UUID', () => {
    /** UUID は利用者の判断材料にならない。原文の説明が読める識別を担う。 */
    const html = renderLibrary('PUBLISHED')

    expect(html).toContain('リポジトリ変更を読み取り専用で分析する。')
    expect(html).not.toContain(version().skill_version_id)
  })

  it('offers cleanup only for deprecated versions', () => {
    /** 廃止済みは行が残り続けて一覧が伸びる。片付け経路は廃止後にだけ出す。 */
    expect(renderLibrary('DEPRECATED')).toContain('彻底删除')
    expect(renderLibrary('PUBLISHED')).not.toContain('彻底删除')
    expect(renderLibrary('DRAFT')).not.toContain('彻底删除')
  })
})
