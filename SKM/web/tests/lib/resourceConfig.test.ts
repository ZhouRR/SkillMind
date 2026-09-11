import { describe, expect, it } from 'vitest'

import type { PublishedTaskRecord } from '../../src/api'
import {
  asResourceProvider,
  accessForCapabilities,
  buildIntegrationConfig,
  buildIntegrationScope,
  capabilitiesForAccess,
  collectRequirementOptions,
  findScopeIssue,
  findWriteConfigIssue,
  isWriteCapability,
  parseListInput,
  requirementOptionsForTask,
  scopeDraftFromScope,
  scopeEntries,
  scopeFromDraft,
  selectBindingCapability,
  summarizeScope,
  taskOptionLabel,
  taskScopeKey,
} from '../../src/lib/resourceConfig'
import { emptyConnectDraft } from '../../src/lib/resourceDrafts'

describe('PostgreSQL and MCP connection configuration', () => {
  it('builds structured PostgreSQL metadata with a numeric port and explicit tables', () => {
    const draft = { ...emptyConnectDraft('postgres'), host: ' db.example.test ', database: 'reports', username: 'reader' }
    expect(buildIntegrationConfig('postgres', draft)).toEqual({ host: 'db.example.test', port: 5432,
      database: 'reports', username: 'reader', sslmode: 'verify-full' })
    expect(buildIntegrationScope('postgres', { issueIds: [], fieldKeys: [], paths: [], revisions: [], tables: ['public.reports'] }))
      .toEqual({ tables: ['public.reports'] })
    expect(findScopeIssue('postgres', { tables: [] }, false)).toBe('tables_required')
  })

  it('builds MCP Streamable HTTP metadata and requires explicit resource URIs', () => {
    expect(buildIntegrationConfig('mcp', { ...emptyConnectDraft('mcp'), serverUrl: ' https://mcp.example.test/mcp ' }))
      .toEqual({ server_url: 'https://mcp.example.test/mcp', transport: 'streamable_http' })
    expect(buildIntegrationScope('mcp', { issueIds: [], fieldKeys: [], paths: [], revisions: [], resourceUris: ['resource://reports/current'] }))
      .toEqual({ resource_uris: ['resource://reports/current'] })
    expect(findScopeIssue('mcp', { resource_uris: [] }, false)).toBe('resource_uris_required')
  })

  it.each(['postgres', 'mcp'] as const)('recognizes %s without granting a write capability', (provider) => {
    expect(asResourceProvider(provider)).toBe(provider)
    expect(capabilitiesForAccess(provider, 'read_write')).toEqual(capabilitiesForAccess(provider, 'read'))
  })
})

/** テスト用の published task descriptor を生成する。 */
function task(overrides: Partial<PublishedTaskRecord> = {}): PublishedTaskRecord {
  return {
    skill_id: '00000000-0000-4000-8000-000000000060',
    skill_version_id: '00000000-0000-4000-8000-000000000061',
    skill_key: 'ticket-review',
    skill_name: 'Ticket Review',
    version: '1.0.0',
    task_key: 'ticket.analyze',
    task_id: '00000000-0000-4000-8000-0000000000aa',
    capability: 'ticket.analyze/v1',
    title: '工单分析',
    task_type: 'analysis',
    input_schema: { type: 'object' },
    output_schema: { type: 'object' },
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

describe('parseListInput', () => {
  it('splits on newlines and commas, trimming and deduplicating', () => {
    // 裸 JSON 入力を置き換える自由 list 入力の正規化を固定する。
    expect(parseListInput('1001\n1002, 1003,\n 1001 ')).toEqual(['1001', '1002', '1003'])
    expect(parseListInput('')).toEqual([])
  })
})

describe('capabilitiesForAccess', () => {
  it('maps the read/write choice onto registered versioned capabilities', () => {
    // 権限档位は capability ID 手入力の置き換えなので、写像は registry と一致し続ける必要がある。
    expect(capabilitiesForAccess('redmine', 'read')).toEqual(['issue.read/v1'])
    expect(capabilitiesForAccess('redmine', 'read_write')).toEqual(['issue.read/v1', 'issue.update/v1'])
  })

  it('offers the registered repository write capability once it exists', () => {
    // §20 で repository.write/v1 が登録されたため、git/svn も read_write を宣言できる。
    expect(capabilitiesForAccess('git', 'read_write'))
      .toEqual(['repository.read/v1', 'repository.write/v1'])
    expect(capabilitiesForAccess('svn', 'read')).toEqual(['repository.read/v1'])
  })
})

describe('accessForCapabilities', () => {
  it('classifies stored integrations back into the two access levels', () => {
    expect(accessForCapabilities(['issue.read/v1'])).toBe('read')
    expect(accessForCapabilities(['issue.read/v1', 'issue.update/v1'])).toBe('read_write')
  })
})

describe('buildIntegrationScope / buildIntegrationConfig', () => {
  it('produces the fixed provider allowlist shapes', () => {
    // Server の normalize_provider_scope が要求する固定 key 集合と一致させる。
    expect(buildIntegrationScope('redmine', {
      issueIds: ['1001'], fieldKeys: ['status_id'], paths: [], revisions: [],
    })).toEqual({ issue_ids: ['1001'], field_keys: ['status_id'] })
    expect(buildIntegrationScope('git', {
      issueIds: [], fieldKeys: [], paths: ['src'], revisions: ['HEAD'],
    })).toEqual({ paths: ['src'], revisions: ['HEAD'] })
  })

  it('defaults the repository revision to HEAD and trims connection values', () => {
    expect(buildIntegrationConfig('redmine', {
      baseUrl: ' https://redmine.example.com ', repositoryUri: '', defaultRevision: '',
    })).toEqual({ base_url: 'https://redmine.example.com' })
    expect(buildIntegrationConfig('svn', {
      baseUrl: '', repositoryUri: 'https://svn.example.com/repo', defaultRevision: ' ',
    })).toEqual({ repository_uri: 'https://svn.example.com/repo', default_revision: 'HEAD' })
  })
})

describe('findScopeIssue', () => {
  it('mirrors the server-side mandatory scope rules', () => {
    // Server が確実に拒否する入力を送信前に検出できることを固定する。
    expect(findScopeIssue('redmine', { issue_ids: [], field_keys: [] }, false)).toBe('issue_ids_required')
    expect(findScopeIssue('redmine', { issue_ids: ['1001'], field_keys: [] }, true)).toBe('field_keys_required')
    expect(findScopeIssue('redmine', { issue_ids: ['1001'], field_keys: [] }, false)).toBeNull()
    expect(findScopeIssue('git', { paths: [], revisions: [] }, false)).toBe('paths_required')
    expect(findScopeIssue('git', { paths: ['src'], revisions: [] }, false)).toBeNull()
  })

  it('accepts the explicit wildcard as a non-empty grant', () => {
    // 「不限」は空 list ではなく明示 token として送るため、必須検証を満たす。
    expect(findScopeIssue('redmine', { issue_ids: ['*'], field_keys: ['*'] }, true)).toBeNull()
  })
})

describe('scope subset drafts', () => {
  it('keeps the wildcard only through the sanctioned keep-all switch', () => {
    const drafts = scopeDraftFromScope({ issue_ids: ['*'], field_keys: ['status_id'] }, { keepAll: true })

    expect(drafts).toEqual([
      { key: 'field_keys', sourceValues: ['status_id'], wildcardSource: false, keepAll: false, picked: ['status_id'], raw: '' },
      { key: 'issue_ids', sourceValues: ['*'], wildcardSource: true, keepAll: true, picked: [], raw: '' },
    ])
    expect(scopeFromDraft(drafts)).toEqual({ field_keys: ['status_id'], issue_ids: ['*'] })
  })

  it('narrows a wildcard source with free-form values and strips typed wildcards', () => {
    // 収窄入力に紛れた "*" は落とす。全許可の維持は keepAll だけを正規経路とする。
    const drafts = scopeDraftFromScope({ issue_ids: ['*'] }, { keepAll: false })
    const narrowed = drafts.map((entry) => ({ ...entry, raw: '1001, *, 1002' }))

    expect(drafts[0]?.keepAll).toBe(false)
    expect(scopeFromDraft(narrowed)).toEqual({ issue_ids: ['1001', '1002'] })
  })

  it('builds explicit subsets from checkbox picks', () => {
    const drafts = scopeDraftFromScope({ issue_ids: ['1001', '1002'], field_keys: [] }, { keepAll: true })
    const picked = drafts.map((entry) => (
      entry.key === 'issue_ids' ? { ...entry, picked: ['1002'] } : entry
    ))

    expect(scopeFromDraft(picked)).toEqual({ field_keys: [], issue_ids: ['1002'] })
  })
})

describe('scopeEntries / summarizeScope', () => {
  it('lists only string-array entries in stable key order', () => {
    expect(scopeEntries({ field_keys: ['status_id'], issue_ids: ['1001'], nested: { a: 1 } })).toEqual([
      { key: 'field_keys', values: ['status_id'] },
      { key: 'issue_ids', values: ['1001'] },
    ])
  })

  it('collapses long value lists into a count suffix', () => {
    expect(summarizeScope({ issue_ids: ['1', '2', '3', '4', '5'], field_keys: [] })).toBe(
      'field_keys: — · issue_ids: 1, 2, 3 +2',
    )
  })

  it('replaces the wildcard token with a reader-facing label', () => {
    expect(summarizeScope(
      { issue_ids: ['*'], field_keys: ['status_id'] },
      { wildcardLabel: '不限' },
    )).toBe('field_keys: status_id · issue_ids: 不限')
  })
})

describe('selectBindingCapability', () => {
  const capabilities = ['issue.read/v1', 'issue.update/v1']

  it('prefers a matching hint for the requested access', () => {
    // Backend `_select_binding_capability` と同じ決定にならないと binding が stale 拒否される。
    expect(selectBindingCapability(capabilities, {
      hints: ['issue.update/v1'], access: 'write', observeCapability: 'issue.read/v1',
    })).toBe('issue.update/v1')
  })

  it('falls back to the observe capability for read access', () => {
    expect(selectBindingCapability(capabilities, {
      hints: [], access: 'read', observeCapability: 'issue.read/v1',
    })).toBe('issue.read/v1')
    expect(isWriteCapability('issue.read/v1')).toBe(false)
  })

  it('returns null when the integration cannot satisfy the access', () => {
    expect(selectBindingCapability(['repository.read/v1'], {
      hints: [], access: 'write', observeCapability: null,
    })).toBeNull()
  })
})

describe('requirement options', () => {
  const readinessTask = task({
    readiness: {
      level: 'RUNNABLE',
      requirements: [{
        key: 'tickets',
        kind: 'issue',
        required: true,
        access: 'read',
        status: 'AVAILABLE',
        reason: 'bound',
        capabilities: ['issue.read/v1'],
        selection_guidance: null,
        candidates: [],
      }],
    },
  })

  it('builds the TASK scope key exactly as run resolution expects', () => {
    // runs/service の task_scope_key(`skill_version_id:task_key`)と一致させる。
    expect(taskScopeKey(readinessTask)).toBe(
      '00000000-0000-4000-8000-000000000061:ticket.analyze',
    )
    expect(taskOptionLabel(readinessTask)).toBe('Ticket Review 1.0.0 · 工单分析')
  })

  it('derives the observe capability from the declared capabilities and access', () => {
    const options = requirementOptionsForTask(readinessTask)
    expect(options).toHaveLength(1)
    expect(options[0]).toMatchObject({
      key: 'tickets',
      kind: 'issue',
      access: 'read',
      observeCapability: 'issue.read/v1',
    })
  })

  it('yields no options when readiness is not computed', () => {
    // 資源要求の宣言元は blueprint(= readiness)一本。二重宣言の退避先は廃止した。
    expect(requirementOptionsForTask(task({ readiness: null }))).toEqual([])
  })

  it('merges the same requirement key across tasks with combined labels', () => {
    const second = task({
      skill_version_id: '00000000-0000-4000-8000-000000000062',
      skill_name: 'Other Skill',
      title: '另一个任务',
      readiness: {
        level: 'RUNNABLE',
        requirements: [{
          key: 'tickets',
          kind: 'issue',
          required: true,
          access: 'read',
          status: 'AVAILABLE',
          reason: 'bound',
          capabilities: ['issue.read/v1'],
          selection_guidance: null,
          candidates: [],
        }],
      },
    })
    const options = collectRequirementOptions([readinessTask, second])
    expect(options).toHaveLength(1)
    expect(options[0]?.taskLabels).toEqual([
      'Ticket Review 1.0.0 · 工单分析',
      'Other Skill 1.0.0 · 另一个任务',
    ])
  })
})

describe('repository write configuration', () => {
  const write = {
    writeEnabled: true,
    writeMode: 'direct' as const,
    writeBranchPrefix: '',
    forgeKind: '' as const,
    forgeApiBaseUrl: '',
    forgeProject: '',
  }

  it('only sends write keys when write is enabled', () => {
    const base = { baseUrl: '', repositoryUri: 'https://git.example.test/p.git', defaultRevision: 'main' }

    expect(buildIntegrationConfig('git', { ...base, write: { ...write, writeEnabled: false } }))
      .toEqual({ repository_uri: 'https://git.example.test/p.git', default_revision: 'main' })
    expect(buildIntegrationConfig('git', { ...base, write }))
      .toEqual({
        repository_uri: 'https://git.example.test/p.git',
        default_revision: 'main',
        write_mode: 'direct',
      })
  })

  it('carries the branch prefix only in branch mode, and the forge trio together', () => {
    const base = { baseUrl: '', repositoryUri: 'https://git.example.test/p.git', defaultRevision: 'main' }

    expect(buildIntegrationConfig('git', {
      ...base,
      write: { ...write, writeMode: 'branch', writeBranchPrefix: 'skillmind/review/' },
    }).write_branch_prefix).toBe('skillmind/review/')
    // direct では branch 接頭辞は意味を持たないため送らない。
    expect(buildIntegrationConfig('git', {
      ...base,
      write: { ...write, writeBranchPrefix: 'skillmind/review/' },
    }).write_branch_prefix).toBeUndefined()
    expect(buildIntegrationConfig('git', {
      ...base,
      write: {
        ...write,
        forgeKind: 'github',
        forgeApiBaseUrl: 'https://api.github.com',
        forgeProject: 'acme/widgets',
      },
    })).toMatchObject({
      forge_kind: 'github',
      forge_api_base_url: 'https://api.github.com',
      forge_project: 'acme/widgets',
    })
  })

  it('mirrors the backend fail-closed rules before submitting', () => {
    // git の direct は「既定 branch」へ書くため、HEAD のままでは server が拒否する。
    expect(findWriteConfigIssue('git', { defaultRevision: 'HEAD', write }))
      .toBe('direct_requires_branch_name')
    expect(findWriteConfigIssue('git', { defaultRevision: 'main', write })).toBeNull()
    // svn の HEAD は正当な revision 式。
    expect(findWriteConfigIssue('svn', { defaultRevision: 'HEAD', write })).toBeNull()
    // branch 接頭辞は予約 namespace の内側でのみ絞り込める。
    expect(findWriteConfigIssue('git', {
      defaultRevision: 'main',
      write: { ...write, writeMode: 'branch', writeBranchPrefix: 'release/' },
    })).toBe('branch_prefix_reserved')
    // forge は三項目そろうか、まったく無いか。
    expect(findWriteConfigIssue('git', {
      defaultRevision: 'main',
      write: { ...write, forgeKind: 'github', forgeApiBaseUrl: '', forgeProject: 'acme/widgets' },
    })).toBe('forge_incomplete')
    expect(findWriteConfigIssue('git', {
      defaultRevision: 'main',
      write: { ...write, forgeApiBaseUrl: 'https://api.github.com' },
    })).toBe('forge_incomplete')
    // 読取だけの Integration は書き込み設定の検証対象外。
    expect(findWriteConfigIssue('git', {
      defaultRevision: 'HEAD',
      write: { ...write, writeEnabled: false },
    })).toBeNull()
  })
})
