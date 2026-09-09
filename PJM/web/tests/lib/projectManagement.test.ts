import { describe, expect, it } from 'vitest'

import { projectCreateInput, projectDraft, projectIntent, projectUpdateInput, validProjectDraft } from '../../src/lib/projectManagement'
import { DEMO_PROJECT as PROJECT } from '../fixtures'

describe('Project management immutable draft', () => {
  it('offers an empty new-project draft without an existing project identity', () => {
    expect(projectDraft()).toEqual({ key: '', name: '', description: '', retentionDays: '90' })
  })

  it('captures original nested settings, identity, version and draft independently of later list updates', () => {
    const original = { ...PROJECT, row_version: 7, settings: { report: { mode: 'original' } } }
    const draft = { ...projectDraft(original), name: 'Human draft' }
    const intent = projectIntent('edit', original, draft)
    original.name = 'Updated elsewhere'
    original.settings.report.mode = 'changed'
    draft.name = 'New draft'
    expect(intent.original?.name).toBe(PROJECT.name)
    expect(intent.original?.row_version).toBe(7)
    expect(intent.original?.settings).toEqual({ report: { mode: 'original' } })
    expect(intent.draft.name).toBe('Human draft')
  })

  it('sends the exact adopted version and preserves unedited settings by omitting them', () => {
    const intent = projectIntent('edit', { ...PROJECT, row_version: 3 }, { ...projectDraft(PROJECT), name: 'Draft name' })
    const adopted = { ...intent, base: { ...PROJECT, row_version: 9, settings: { newSetting: true } } }
    expect(projectUpdateInput(adopted)).toEqual({ expected_row_version: 9, name: 'Draft name', description: PROJECT.description, retention_days: PROJECT.retention_days })
    expect(projectUpdateInput(adopted)).not.toHaveProperty('settings')
    expect(projectUpdateInput(adopted)).not.toHaveProperty('key')
    expect(projectUpdateInput(adopted)).not.toHaveProperty('status')
    expect(adopted.original?.row_version).toBe(3)
  })

  it('does not invent an expected version for an absent original project', () => {
    expect(() => projectUpdateInput(projectIntent('create', null))).toThrow('original identity')
  })

  it('creates from only the frozen draft without an expected version', () => {
    const draft = { key: 'new-project', name: 'New project', description: 'Draft', retentionDays: '3650' }
    expect(projectCreateInput(draft)).toEqual({ key: 'new-project', name: 'New project', description: 'Draft', retention_days: 3650, settings: {} })
    expect(validProjectDraft(draft, true)).toBe(true)
  })

  it.each(['archive', 'restore', 'delete'] as const)('retains legal historical metadata unchanged for %s', (action) => {
    // API が許可する空白 name は編集草稿では拒否しても、原対象の lifecycle は妨げない。
    const historical = { ...PROJECT, name: '   ', row_version: 12 }
    const intent = projectIntent(action, historical)
    expect(intent.base?.name).toBe('   ')
    expect(intent.base?.row_version).toBe(12)
    expect(intent.draft.name).toBe('   ')
    expect(validProjectDraft(intent.draft, false)).toBe(false)
  })

  it.each(['', '0', '3651', '1.5', 'NaN'])('refuses invalid retention days %s', (retentionDays) => {
    expect(validProjectDraft({ ...projectDraft(PROJECT), retentionDays }, false)).toBe(false)
  })

  it.each(['', '-bad', 'Uppercase', 'with space', 'x'.repeat(101)])('refuses invalid new identity keys', (key) => {
    expect(validProjectDraft({ ...projectDraft(PROJECT), key }, true)).toBe(false)
  })

  it('rejects blank/oversized names and oversized descriptions without silently truncating', () => {
    expect(validProjectDraft({ ...projectDraft(PROJECT), name: '  ' }, false)).toBe(false)
    expect(validProjectDraft({ ...projectDraft(PROJECT), name: 'x'.repeat(201) }, false)).toBe(false)
    expect(validProjectDraft({ ...projectDraft(PROJECT), description: 'x'.repeat(4001) }, false)).toBe(false)
  })
})
