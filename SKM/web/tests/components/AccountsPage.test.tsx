import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { AuthSessionRecord, UserAccountRecord } from '../../src/api'
import { UserAccountFacts, UserPager, UserResponseNotice, UserSummaryList } from '../../src/components/UserAccountElements'
import { UserCreatePanel } from '../../src/components/UserCreatePanel'
import { LanguageProvider } from '../../src/i18n'
import { MESSAGES, UI_LANGUAGES } from '../../src/lib/i18n/messages'
import { formatLocalTimestamp } from '../../src/lib/presentation'
import { AccountsPage } from '../../src/pages/AccountsPage'

/** 公開 field だけの架空 account。実 credential や外部接続は含まない。 */
const account: UserAccountRecord = {
  user_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  email: 'person@example.invalid',
  display_name: 'Example person',
  system_role: 'USER',
  status: 'ACTIVE',
  row_version: 7,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-02T00:00:00Z',
}

/** Page test は actor の表示可否を確かめるだけで、認可の証明を代用しない。 */
function session(role: 'ADMIN' | 'USER'): AuthSessionRecord {
  return {
    user: { ...account, organization_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', system_role: role },
    csrf_token: 'test-only-non-credential',
    absolute_expires_at: '2026-01-03T00:00:00Z',
  }
}

describe.each(UI_LANGUAGES)('real account components in %s', (language) => {
  const messages = MESSAGES[language].account

  it('offers personal security without requiring any Project or offering ADMIN controls to a USER', () => {
    const html = renderToStaticMarkup(<LanguageProvider language={language}>
      <AccountsPage session={session('USER')} onSessionEnded={() => {}} onAccountChanged={() => {}} />
    </LanguageProvider>)
    expect(html).toContain(MESSAGES[language].routes.accounts.label)
    expect(html).toContain(messages.myAccount)
    expect(html).not.toContain(messages.draftMemoryOnly)
    expect(html).toContain('data-account-own')
    expect(html).not.toContain('data-account-directory')
    expect(html).not.toContain('data-account-form="create"')
    expect(html).not.toContain('test-only-non-credential')
  })

  it('renders the actual ADMIN search and creation forms without persisting inputs in URLs', () => {
    const html = renderToStaticMarkup(<LanguageProvider language={language}>
      <AccountsPage session={session('ADMIN')} onSessionEnded={() => {}} onAccountChanged={() => {}} />
    </LanguageProvider>)
    expect(html).toContain('data-account-directory')
    expect(html).toContain('class="detailDisclosure accountDirectorySection"')
    expect(html).toContain('data-account-form="search"')
    expect(html).toContain('data-account-form="create"')
    expect(html).toContain(messages.manageUsers)
    expect(html).toContain(messages.initialPassword)
    expect(html).not.toContain('action=')
    expect(html).not.toContain('test-only-non-credential')
  })

  it('starts creation with an explicit role choice and labelled, empty password controls', () => {
    const html = renderToStaticMarkup(<LanguageProvider language={language}>
      <UserCreatePanel session={session('ADMIN')} onSessionEnded={() => {}} onCreated={() => {}} onSelect={() => {}} />
    </LanguageProvider>)
    expect(html).toContain(`<option value="" selected="">${messages.rolePlaceholder}</option>`)
    expect(html).toContain(`<label>${messages.fields.email}<input`)
    expect(html).toContain(`<label>${messages.initialPassword}<input`)
    expect(html).toContain(`<label>${messages.confirmPassword}<input`)
    expect(html.match(/type="password"/g)).toHaveLength(2)
    expect(html.match(/autoComplete="new-password"/g)).toHaveLength(2)
    expect(html.match(/minLength="8"/g)).toHaveLength(2)
    expect(html).toContain(messages.passwordPolicy)
    expect(messages.passwordPolicy).toContain('8 ')
    expect(html).toContain(messages.createHint)
  })

  it('keeps original version, account identity and status explicit and escapes untrusted names', () => {
    const html = renderToStaticMarkup(<LanguageProvider language={language}>
      <UserAccountFacts account={{ ...account, display_name: '<img src=x onerror=alert(1)>' }} />
    </LanguageProvider>)
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;')
    expect(html).not.toContain('<img')
    expect(html).toContain(account.user_id)
    expect(html).toContain(`<dt>${messages.fields.version}</dt><dd>7</dd>`)
    expect(html).toContain(messages.statuses.ACTIVE)
  })

  it('selects exact user identities with individually named buttons and displays current server facts', () => {
    const html = renderToStaticMarkup(<LanguageProvider language={language}>
      <UserSummaryList users={[account]} disabled={false} onSelect={() => {}} />
    </LanguageProvider>)
    expect(html).toContain(`aria-label="${messages.edit}: ${account.email}"`)
    expect(html).toContain(`class="accountUserList" tabindex="0" aria-label="${messages.manageUsers}"`)
    expect(html).toContain(messages.roles.USER)
    expect(html).toContain(account.email)
    expect(html).not.toContain(messages.createdSuccess)
  })

  it('distinguishes account timestamps on different days instead of showing only the same clock time', () => {
    /** 作成日と更新日が違う事実を、省略した時刻表示で同一に見せない。 */
    const html = renderToStaticMarkup(<LanguageProvider language={language}>
      <UserAccountFacts account={account} />
    </LanguageProvider>)
    const created = formatLocalTimestamp(account.created_at)
    const updated = formatLocalTimestamp(account.updated_at)
    expect(created).not.toBe(updated)
    expect(html).toContain(`>${created}</time>`)
    expect(html).toContain(`>${updated}</time>`)
    expect(html).toContain(`dateTime="${account.created_at}"`)
  })

  it('does not present an email conflict or a newer version as creation success', () => {
    const html = renderToStaticMarkup(<LanguageProvider language={language}>
      <UserResponseNotice failure={{ key: 'emailConflict' }} />
      <UserResponseNotice failure={{ key: 'versionConflict' }} />
    </LanguageProvider>)
    expect(html).toContain(messages.failures.emailConflict)
    expect(html).toContain(messages.failures.versionConflict)
    expect(html).toContain('role="alert"')
    expect(html).not.toContain(messages.createdSuccess)
  })
})

describe('server account pagination', () => {
  it.each([
    [0, 25, 51, 1],
    [25, 25, 51, 0],
    [50, 1, 51, 1],
    [0, 0, 0, 2],
  ])('allows only available page transitions at offset %s', (offset, count, total, disabled) => {
    const html = renderToStaticMarkup(<UserPager offset={offset} count={count} total={total} limit={25} pending={false} onChange={() => {}} />)
    expect(html.match(/disabled=""/g) ?? []).toHaveLength(disabled)
  })

  it('blocks both transitions while the server page is unresolved', () => {
    const html = renderToStaticMarkup(<UserPager offset={25} count={25} total={100} limit={25} pending onChange={() => {}} />)
    expect(html.match(/disabled=""/g)).toHaveLength(2)
  })
})
