import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { ProjectComparison, ProjectIntentConfirmation, ProjectResponseNotice } from '../../src/components/ProjectManagementElements'
import { ProjectManagementPanel, ProjectMetadataForm, ProjectRows } from '../../src/components/ProjectManagementPanel'
import { ProjectManagementReview } from '../../src/components/ProjectManagementReview'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES, type UiLanguage } from '../../src/lib/i18n/messages'
import { projectDraft, projectIntent } from '../../src/lib/projectManagement'
import { DEMO_PROJECT as PROJECT, demoSession } from '../fixtures'

/** Static projection は文案と構造を検証し、同期 click と HTTP は browser suite に任せる。 */
function render(node: ReactNode, language: UiLanguage = 'zh'): string {
  return renderToStaticMarkup(<LanguageProvider language={language}>{node}</LanguageProvider>)
}

describe('Project management role and form projection', () => {
  it.each(['zh', 'ja', 'en'] as const)('keeps creation accessible with no current Project in %s', (language) => {
    const html = render(<ProjectManagementPanel tab="projects" projectId="" projectState={{ status: 'ready', projects: [] }}
      session={demoSession('ADMIN')} onSessionEnded={vi.fn()} setProjectId={vi.fn()} onProjectChanged={vi.fn()}
      onProjectArchived={vi.fn()} onEditTab={vi.fn()} />, language)
    expect(html).toContain('data-project-form=""')
    expect(html).toContain(MESSAGES[language].projects.createTitle)
    expect(html).not.toContain('<fieldset disabled')
    expect(html).not.toContain(demoSession('ADMIN').csrf_token)
  })

  it('does not render management forms or archive operations for USER', () => {
    const html = render(<ProjectManagementPanel tab="projects" projectId={PROJECT.project_id} projectState={{ status: 'ready', projects: [PROJECT] }}
      session={demoSession('USER')} onSessionEnded={vi.fn()} setProjectId={vi.fn()} onProjectChanged={vi.fn()}
      onProjectArchived={vi.fn()} onEditTab={vi.fn()} />)
    expect(html).toContain('data-project-action="select"')
    expect(html).toContain(PROJECT.name)
    expect(html).not.toContain('data-project-form')
    expect(html).not.toContain('data-project-action="archive"')
  })

  it('preserves editable values while making identity and settings non-editable', () => {
    const draft = { ...projectDraft(PROJECT), name: 'Human draft', retentionDays: '180' }
    const html = render(<ProjectMetadataForm draft={draft} editing locked={false} onChange={vi.fn()} onSubmit={vi.fn()} onReset={vi.fn()} />)
    expect(html).toContain('value="Human draft"')
    expect(html).toContain('value="180"')
    expect(html).not.toContain('name="key"')
    expect(html).not.toContain('name="settings"')
    expect(html).not.toContain('name="status"')
    expect(html).toContain(MESSAGES.zh.projectManagement.settingsPreserved)
  })

  it('uses a disabled fieldset to lock every input after freezing an intent', () => {
    const html = render(<ProjectMetadataForm draft={projectDraft(PROJECT)} editing locked onChange={vi.fn()} onSubmit={vi.fn()} onReset={vi.fn()} />)
    expect(html).toContain('<fieldset disabled="">')
    expect(html).toContain(PROJECT.name)
  })

  it('shows original row identities, versions and status with lifecycle-specific actions', () => {
    const archived = { ...PROJECT, project_id: '00000000-0000-4000-8000-000000000011', row_version: 9, status: 'ARCHIVED' as const }
    const html = render(<ProjectRows projects={[PROJECT, archived]} projectId={PROJECT.project_id} locked={false} admin onSelect={vi.fn()} onAction={vi.fn()} />)
    expect(html).toContain(`data-project-row="${PROJECT.project_id}"`)
    expect(html).toContain(`data-project-row="${archived.project_id}"`)
    expect(html).toContain('data-project-action="archive"')
    expect(html).toContain('data-project-action="restore"')
    expect(html).toContain('data-project-action="delete"')
    expect(html).toContain(`${MESSAGES.zh.projectManagement.version}: 9`)
  })

  it('keeps lifecycle actions available for a legal historical whitespace name', () => {
    const html = render(<ProjectRows projects={[{ ...PROJECT, name: '   ' }]} projectId={PROJECT.project_id} locked={false} admin onSelect={vi.fn()} onAction={vi.fn()} />)
    expect(html).toContain('data-project-action="archive"')
    expect(html).not.toContain('disabled=""')
    expect(html).toContain(PROJECT.project_id)
  })
})

describe('Project original confirmation and three-way comparison', () => {
  it.each(['create', 'edit', 'archive', 'restore', 'delete'] as const)('requires a separate original %s confirmation', (action) => {
    const intent = projectIntent(action, action === 'create' ? null : PROJECT, projectDraft(PROJECT))
    const html = render(<ProjectIntentConfirmation intent={intent} busy={false} onConfirm={vi.fn()} onCancel={vi.fn()} />)
    expect(html).toContain('data-project-intent=""')
    expect(html).toContain('data-project-confirm=""')
    expect(html).toContain(MESSAGES.zh.projectManagement.actions[action])
    expect(html).toContain(PROJECT.key)
    if (action !== 'create') expect(html).toContain(PROJECT.project_id)
    expect(html).not.toContain('disabled=""')
  })

  it('disables both confirmation and cancellation during the original write', () => {
    const html = render(<ProjectIntentConfirmation intent={projectIntent('delete', PROJECT)} busy onConfirm={vi.fn()} onCancel={vi.fn()} />)
    expect(html.match(/disabled=""/g)).toHaveLength(2)
  })

  it.each(['zh', 'ja', 'en'] as const)('shows original, draft and current values separately in %s', (language) => {
    const intent = projectIntent('edit', { ...PROJECT, name: 'Original name', row_version: 2 }, { ...projectDraft(PROJECT), name: 'My draft' })
    const current = { ...PROJECT, name: 'Someone else edited', row_version: 3 }
    const html = render(<ProjectComparison intent={intent} current={current} />, language)
    for (const label of ['Original name', 'My draft', 'Someone else edited', MESSAGES[language].projectManagement.original, MESSAGES[language].projectManagement.current]) expect(html).toContain(label)
    expect(html).toContain('data-project-comparison=""')
    expect(html).toContain('<dd>2</dd>')
    expect(html).toContain('<dd>3</dd>')
  })

  it('does not equate a currently inaccessible original ID with a confirmed deletion', () => {
    const html = render(<ProjectComparison intent={projectIntent('delete', PROJECT)} current={null} />)
    expect(html).toContain(PROJECT.project_id)
    expect(html).toContain(MESSAGES.zh.projectManagement.notAccessibleFact)
  })
})

describe('Project conflict and unknown recovery projection', () => {
  it.each(['zh', 'ja', 'en'] as const)('requires an explicit initial read before any unknown acknowledgment in %s', (language) => {
    const html = render(<ProjectManagementReview intent={projectIntent('archive', PROJECT)} unknown onSessionEnded={vi.fn()} onAdopt={vi.fn()} onAcknowledge={vi.fn()} onCancel={vi.fn()} />, language)
    expect(html).toContain('data-project-unknown=""')
    expect(html).toContain('data-project-reconcile="">')
    expect(html).toContain(MESSAGES[language].projectManagement.unknownHint)
    expect(html).not.toContain('data-project-acknowledge=')
    expect(html).not.toContain('data-project-cancel=')
    expect(html).not.toContain('data-project-adopt=')
  })

  it('allows cancellation of a known conflict but never exposes an unread version adoption', () => {
    const html = render(<ProjectManagementReview intent={projectIntent('edit', PROJECT)} unknown={false} onSessionEnded={vi.fn()} onAdopt={vi.fn()} onAcknowledge={vi.fn()} onCancel={vi.fn()} />)
    expect(html).toContain('data-project-conflict=""')
    expect(html).toContain('data-project-cancel=""')
    expect(html).not.toContain('data-project-adopt=')
  })

  it('retains the original creation key even without a confirmed project ID', () => {
    const html = render(<ProjectManagementReview intent={projectIntent('create', null, { ...projectDraft(), key: 'original-create-key', name: 'Original draft' })}
      unknown onSessionEnded={vi.fn()} onAdopt={vi.fn()} onAcknowledge={vi.fn()} onCancel={vi.fn()} />)
    expect(html).toContain('original-create-key')
    expect(html).toContain('Original draft')
    expect(html).not.toContain(PROJECT.project_id)
  })

  it('uses a safe fixed response message rather than Error.message', () => {
    const html = render(<ProjectResponseNotice failure={{ key: 'versionExhausted' }} />)
    expect(html).toContain('role="alert"')
    expect(html).toContain(MESSAGES.zh.projectManagement.failures.versionExhausted)
  })
})
