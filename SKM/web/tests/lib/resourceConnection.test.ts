import { describe, expect, it } from 'vitest'
import type { IntegrationRecord } from '../../src/api'
import { prepareResourceConnection, resourceWriteEnabled, type ResourceFeatures } from '../../src/lib/resourceConnection'
import { connectDraftFromIntegration, emptyConnectDraft } from '../../src/lib/resourceDrafts'

const closed: ResourceFeatures = { deferredFeaturesEnabled: false, databaseWritesEnabled: false,
  gitWritesEnabled: false, mcpToolsEnabled: false }

describe('resource connection preparation', () => {
  it.each([
    ['SELECT'],
    ['SELECT', 'UPDATE'],
    ['SELECT', 'INSERT', 'UPDATE', 'DELETE'],
  ])('preserves saved native SQL permissions when editing: %j', (...statements) => {
    const capabilities = statements.length === 1 ? ['database.query/v1'] : ['database.query/v1', 'database.execute/v1']
    const config = { access_mode: 'native_sql', host: 'db.example.test', port: 5432,
      database: 'review', username: 'reviewer', sslmode: 'require' }
    const item: IntegrationRecord = { integration_id: 'fixture-integration', project_id: 'fixture-project',
      name: 'Review DB', kind: 'other', provider: 'postgres', status: 'ACTIVE', revision: 5,
      capabilities, scope: { statements }, config_keys: Object.keys(config), secret_reference_id: 'fixture-credential',
      created_by: 'fixture-admin', created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z', disabled_at: null }
    const draft = connectDraftFromIntegration(item, config)
    const result = prepareResourceConnection({ ...draft, name: 'Renamed review DB' }, true)
    expect(result.valid).toBe(true)
    if (!result.valid) throw new Error('Expected connection payload')
    expect(result.input.capabilities).toEqual(capabilities)
    expect(result.input.scope).toEqual(item.scope)
    expect(result.input.config).toEqual(config)
    expect(draft.credentialChoice).toBe(item.secret_reference_id)
  })

  it('does not submit a hidden database write permission or embed a credential', () => {
    const result = prepareResourceConnection({ ...emptyConnectDraft('postgres'), name: 'Review DB', host: 'db.example.test',
      database: 'review', username: 'reviewer', tables: 'public.items', access: 'read_write',
      writeColumns: '*', secretValue: 'fixture-secret', locator: '/fixture/secret' }, resourceWriteEnabled('postgres', closed))
    expect(result.valid).toBe(true)
    if (!result.valid) throw new Error('Expected connection payload')
    expect(result.input.capabilities).toEqual(['database.query/v1'])
    expect(result.input.scope).toEqual({ statements: ['SELECT'] })
    expect(JSON.stringify(result.input)).not.toContain('fixture-secret')
    expect(JSON.stringify(result.input)).not.toContain('/fixture/secret')
  })

  it('retains explicit database write scope and operations when enabled', () => {
    const result = prepareResourceConnection({ ...emptyConnectDraft('postgres'), tables: 'public.items',
      access: 'read_write', writeColumns: 'public.items.status', databaseOperations: ['UPDATE'] }, true)
    expect(result.valid).toBe(true)
    if (!result.valid) throw new Error('Expected connection payload')
    expect(result.input.scope).toEqual({ statements: ['SELECT', 'UPDATE'] })
    expect(result.input.capabilities).toEqual(['database.query/v1', 'database.execute/v1'])
  })

  it('reports missing SQL operations before any credential can be saved', () => {
    expect(prepareResourceConnection({ ...emptyConnectDraft('postgres'), tables: 'public.items', access: 'read_write', databaseOperations: [] }, true))
      .toEqual({ valid: false, scopeIssue: 'database_operations_required' })
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
