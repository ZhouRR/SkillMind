import { describe, expect, it } from 'vitest'
import { prepareResourceConnection, resourceWriteEnabled, type ResourceFeatures } from '../../src/lib/resourceConnection'
import { emptyConnectDraft } from '../../src/lib/resourceDrafts'

const closed: ResourceFeatures = { deferredFeaturesEnabled: false, databaseWritesEnabled: false,
  gitWritesEnabled: false, mcpToolsEnabled: false }

describe('resource connection preparation', () => {
  it('does not submit a hidden database write permission or embed a credential', () => {
    const result = prepareResourceConnection({ ...emptyConnectDraft('postgres'), name: 'Review DB', host: 'db.example.test',
      database: 'review', username: 'reviewer', tables: 'public.items', access: 'read_write',
      writeColumns: '*', secretValue: 'fixture-secret', locator: '/fixture/secret' }, resourceWriteEnabled('postgres', closed))
    expect(result.valid).toBe(true)
    if (!result.valid) throw new Error('Expected connection payload')
    expect(result.input.capabilities).toEqual(['database.read/v1'])
    expect(result.input.scope).toEqual({ tables: ['public.items'] })
    expect(JSON.stringify(result.input)).not.toContain('fixture-secret')
    expect(JSON.stringify(result.input)).not.toContain('/fixture/secret')
  })

  it('retains explicit database write scope and operations when enabled', () => {
    const result = prepareResourceConnection({ ...emptyConnectDraft('postgres'), tables: 'public.items',
      access: 'read_write', writeColumns: 'public.items.status', databaseOperations: ['UPDATE'] }, true)
    expect(result.valid).toBe(true)
    if (!result.valid) throw new Error('Expected connection payload')
    expect(result.input.scope).toEqual({ tables: ['public.items'], write_columns: ['public.items.status'], operations: ['UPDATE'] })
    expect(result.input.capabilities).toEqual(['database.read/v1', 'database.write/v1'])
  })

  it('reports missing write columns before any credential can be saved', () => {
    expect(prepareResourceConnection({ ...emptyConnectDraft('postgres'), tables: 'public.items', access: 'read_write' }, true))
      .toEqual({ valid: false, scopeIssue: 'write_columns_required' })
  })

  it('retains the exact repository branch and path restrictions', () => {
    const result = prepareResourceConnection({ ...emptyConnectDraft('git'), access: 'read_write',
      repositoryUri: 'https://repo.example.test/project', defaultRevision: 'main', revisions: 'main',
      paths: 'plans\nresults', writeMode: 'branch', writeBranchPrefix: 'skillmind/' }, true)
    expect(result.valid).toBe(true)
    if (!result.valid) throw new Error('Expected connection payload')
    expect(result.input.scope).toMatchObject({ paths: ['plans', 'results'], revisions: ['main'] })
    expect(result.input.config).toMatchObject({ write_mode: 'branch', write_branch_prefix: 'skillmind/' })
  })

  it('keeps provider-specific feature availability independent', () => {
    const git = { ...closed, gitWritesEnabled: true }
    expect(resourceWriteEnabled('git', git)).toBe(true)
    expect(resourceWriteEnabled('postgres', git)).toBe(false)
    expect(resourceWriteEnabled('mcp', git)).toBe(false)
    expect(resourceWriteEnabled('redmine', git)).toBe(false)
  })
})
