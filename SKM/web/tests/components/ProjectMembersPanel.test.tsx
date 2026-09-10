import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import type { ProjectMemberRecord, UserAccountRecord } from '../../src/api'
import { MemberIntentConfirmation, MemberIntentFacts, MemberResponseNotice, ProjectMemberCandidateList, ProjectMemberList, type MemberIntent } from '../../src/components/ProjectMemberElements'
import { ProjectMemberReview } from '../../src/components/ProjectMemberReview'
import { ProjectMembersPanel } from '../../src/components/ProjectMembersPanel'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES, type UiLanguage } from '../../src/lib/i18n/messages'
import { DEMO_PROJECT as PROJECT, demoSession } from '../fixtures'

const ACCOUNT: UserAccountRecord = {
  user_id: 'abcdefab-0000-4000-8000-000000000100',
  email: 'member@example.test', display_name: 'Member', system_role: 'USER', status: 'ACTIVE',
  row_version: 1, created_at: '2026-07-05T10:00:00Z', updated_at: '2026-07-05T10:00:00Z',
}
const MEMBER: ProjectMemberRecord = {
  user_id: ACCOUNT.user_id, email: ACCOUNT.email, display_name: ACCOUNT.display_name,
  status: 'ACTIVE', joined_at: '2026-07-05T10:00:00Z',
}
const INTENT: MemberIntent = { action: 'remove', project: PROJECT, target: ACCOUNT, before: MEMBER }

/** 同じ構造を三言語で確認し、HTTP と event の証明は実 browser suite が担う。 */
function render(node: ReactNode, language: UiLanguage = 'zh'): string {
  return renderToStaticMarkup(<LanguageProvider language={language}>{node}</LanguageProvider>)
}

describe('ProjectMembersPanel access projection', () => {
  it.each(['zh', 'ja', 'en'] as const)('shows the exact authorized identity and role boundaries in %s', (language) => {
    const messages = MESSAGES[language]
    const html = render(<ProjectMembersPanel projectContextId={PROJECT.project_id} currentProject={PROJECT}
      session={demoSession('ADMIN')} onSessionEnded={vi.fn()} />, language)
    expect(html).toContain('data-member-context="ready"')
    expect(html).toContain(PROJECT.project_id)
    expect(html).toContain(PROJECT.name)
    expect(html).toContain(PROJECT.key)
    expect(html).toContain(messages.projectMembers.adminBypass)
    expect(html).toContain(messages.projectMembers.relationshipHint)
    expect(html).toContain('data-member-search=""')
    expect(html).not.toContain(demoSession('ADMIN').csrf_token)
  })

  it.each(['zh', 'ja', 'en'] as const)('does not expose member controls to USER in %s', (language) => {
    const html = render(<ProjectMembersPanel projectContextId={PROJECT.project_id} currentProject={PROJECT}
      session={demoSession('USER')} onSessionEnded={vi.fn()} />, language)
    expect(html).toContain(MESSAGES[language].projectMembers.adminOnly)
    expect(html).not.toContain('data-project-members')
    expect(html).not.toContain('data-member-search')
    expect(html).not.toContain(PROJECT.project_id)
  })

  it('keeps the interaction subtree hidden until exact Project authorization exists', () => {
    const html = render(<ProjectMembersPanel projectContextId={PROJECT.project_id} currentProject={null}
      session={demoSession('ADMIN')} onSessionEnded={vi.fn()} />)
    expect(html).toContain('data-member-context="unavailable"')
    expect(html).toContain('class="projectMembersContent" hidden=""')
    expect(html).toContain(MESSAGES.zh.projectMembers.needProject)
    expect(html).toMatch(/data-member-refresh="" disabled=""/)
  })

  it('does not substitute a different authorized detail for the selected Project', () => {
    const html = render(<ProjectMembersPanel projectContextId="00000000-0000-4000-8000-000000000011" currentProject={PROJECT}
      session={demoSession('ADMIN')} onSessionEnded={vi.fn()} />)
    expect(html).toContain('data-member-context="unavailable"')
    expect(html).toContain('class="projectMembersContent" hidden=""')
  })

  it('hides and disables the candidate flow for an archived Project', () => {
    const html = render(<ProjectMembersPanel projectContextId={PROJECT.project_id} currentProject={{ ...PROJECT, status: 'ARCHIVED' }}
      session={demoSession('ADMIN')} onSessionEnded={vi.fn()} />)
    expect(html).toContain(MESSAGES.zh.projectMembers.archivedHint)
    expect(html).toContain('<div hidden=""><section class="memberCandidates"')
    expect(html.match(/<input[^>]*name="q"[^>]*>/)?.[0]).toContain('disabled=""')
  })
})

describe('Project member row projections', () => {
  it('retains removed relationship rows without implying account disablement', () => {
    const removed = { ...MEMBER, user_id: 'abcdefab-0000-4000-8000-000000000101', status: 'REMOVED' as const }
    const html = render(<ProjectMemberList members={[MEMBER, removed]} locked={false} onRemove={vi.fn()} />)
    expect(html.match(/data-member-id=/g)).toHaveLength(2)
    expect(html.match(/data-member-action="remove"/g)).toHaveLength(1)
    expect(html).toContain(MESSAGES.zh.projectMembers.states.ACTIVE)
    expect(html).toContain(MESSAGES.zh.projectMembers.states.REMOVED)
    expect(html).not.toContain(MESSAGES.zh.account.statuses.DISABLED)
    expect(html).toContain(`dateTime="${MEMBER.joined_at}"`)
  })

  it('locks the existing remove action without hiding its original target', () => {
    const html = render(<ProjectMemberList members={[MEMBER]} locked onRemove={vi.fn()} />)
    expect(html).toContain(MEMBER.user_id)
    expect(html).toMatch(/data-member-action="remove"[^>]*disabled=""/)
  })

  it('renders the empty relationship state explicitly', () => {
    const html = render(<ProjectMemberList members={[]} locked={false} onRemove={vi.fn()} />)
    expect(html).toContain(MESSAGES.zh.projectMembers.emptyMembers)
    expect(html).not.toContain('data-member-action')
  })

  it.each(['zh', 'ja', 'en'] as const)('keeps every server candidate with complete role and account status in %s', (language) => {
    const messages = MESSAGES[language]
    const removed = { ...ACCOUNT, user_id: 'abcdefab-0000-4000-8000-000000000101', email: 'removed@example.test' }
    const disabled = { ...ACCOUNT, user_id: 'abcdefab-0000-4000-8000-000000000102', status: 'DISABLED' as const }
    const admin = { ...ACCOUNT, user_id: 'abcdefab-0000-4000-8000-000000000103', system_role: 'ADMIN' as const }
    const html = render(<ProjectMemberCandidateList accounts={[ACCOUNT, removed, disabled, admin]}
      members={[MEMBER, { ...MEMBER, user_id: removed.user_id, status: 'REMOVED' }]} locked={false} onAdd={vi.fn()} />, language)
    expect(html.match(/data-member-candidate=/g)).toHaveLength(4)
    expect(html.match(/data-member-action="add"/g)).toHaveLength(4)
    expect(html.match(/disabled=""/g)).toHaveLength(2)
    for (const account of [ACCOUNT, removed, disabled, admin]) {
      expect(html).toContain(account.user_id)
      expect(html).toContain(account.email)
      expect(html).toContain(messages.account.roles[account.system_role])
      expect(html).toContain(messages.account.statuses[account.status])
    }
    expect(html).toContain(messages.projectMembers.alreadyMember)
    expect(html).toContain(messages.projectMembers.disabledCandidate)
  })

  it.each([true, false])('recognizes the same ACTIVE member across UUID casing (uppercase member: %s)', (uppercaseMember) => {
    const member = { ...MEMBER, user_id: uppercaseMember ? MEMBER.user_id.toUpperCase() : MEMBER.user_id }
    const account = { ...ACCOUNT, user_id: uppercaseMember ? ACCOUNT.user_id : ACCOUNT.user_id.toUpperCase() }
    const html = render(<ProjectMemberCandidateList accounts={[account]} members={[member]} locked={false} onAdd={vi.fn()} />)
    expect(html).toContain(MESSAGES.zh.projectMembers.alreadyMember)
    expect(html).toMatch(/data-member-action="add"[^>]*disabled=""/)
  })

  it('locks all new additions while facts or the previous write remain unresolved', () => {
    const html = render(<ProjectMemberCandidateList accounts={[ACCOUNT]} members={[]} locked onAdd={vi.fn()} />)
    expect(html).toMatch(/data-member-action="add"[^>]*disabled=""/)
  })
})

describe('Project member frozen intent and recovery', () => {
  it('keeps original Project/account/relationship facts and escapes returned names', () => {
    const html = render(<MemberIntentFacts intent={{ ...INTENT, target: { ...ACCOUNT, display_name: '<script>private-body</script>' } }} />)
    expect(html).toContain(PROJECT.project_id)
    expect(html).toContain(ACCOUNT.user_id)
    expect(html).toContain(ACCOUNT.email)
    expect(html).toContain(MESSAGES.zh.projectMembers.states.ACTIVE)
    expect(html).toContain('&lt;script&gt;private-body&lt;/script&gt;')
    expect(html).not.toContain('<script>')
  })

  it('requires one explicit confirmation for the frozen normal action', () => {
    const html = render(<MemberIntentConfirmation intent={INTENT} disabled={false} changed={false} onConfirm={vi.fn()} onCancel={vi.fn()} />)
    expect(html).toContain('data-member-intent=""')
    expect(html).toContain('data-member-confirm=""')
    expect(html).toContain(MESSAGES.zh.projectMembers.confirmRemove)
    expect(html).not.toContain('disabled=""')
    expect(html).not.toContain('type="checkbox"')
  })

  it('disables confirmation when the latest facts no longer authorize the frozen action', () => {
    const html = render(<MemberIntentConfirmation intent={INTENT} disabled={false} changed onConfirm={vi.fn()} onCancel={vi.fn()} />)
    expect(html).toMatch(/data-member-confirm="" disabled=""/)
    expect(html).not.toMatch(/data-member-cancel="" disabled=""/)
    expect(html).toContain(MESSAGES.zh.projectMembers.selectionChanged)
  })

  it.each(['zh', 'ja', 'en'] as const)('offers explicit reconciliation before reading or acknowledging in %s', (language) => {
    const html = render(<ProjectMemberReview intent={INTENT} enabled onSessionEnded={vi.fn()} onAcknowledge={vi.fn()} />, language)
    expect(html).toContain(MESSAGES[language].projectMembers.unknownHint)
    expect(html).toContain(MESSAGES[language].projectMembers.reconcileHint)
    expect(html).toContain(PROJECT.project_id)
    expect(html).toContain(ACCOUNT.user_id)
    // enabled=false の query.pending を初回の照合 button 禁止に流用しない。
    expect(html).toContain('data-member-reconcile="">')
    expect(html).not.toContain('data-member-acknowledge=')
    expect(html).not.toContain('data-member-reviewed=')
  })

  it('pauses reconciliation while original Project authorization is being checked', () => {
    const html = render(<ProjectMemberReview intent={INTENT} enabled={false} onSessionEnded={vi.fn()} onAcknowledge={vi.fn()} />)
    expect(html).toContain('data-member-reconcile="" disabled=""')
  })

  it.each(['sessionExpired', 'csrfRejected', 'adminRequired', 'notFound', 'invalidRequest', 'unknown', 'loadFailed'] as const)('renders a fixed safe message for %s', (key) => {
    const html = render(<MemberResponseNotice failure={{ key }} />)
    expect(html).toContain('role="alert"')
    expect(html).toContain(MESSAGES.zh.projectMembers.failures[key])
  })
})
