import { useState } from 'react'
import { createRoot } from 'react-dom/client'

import type { UserAccountRecord } from '../../src/api'
import { ActionMenu } from '../../src/components/ActionMenu'
import { ModalDialog } from '../../src/components/PageElements'
import { UserSummaryList } from '../../src/components/UserAccountElements'
import { LanguageProvider, useMessages } from '../../src/i18n'
import type { UiLanguage } from '../../src/lib/i18n/messages'
import '../../src/styles.css'

/** 実 component と一覧 class に合成データを渡し、API/永続化なしで余白と scroll を観測する。 */
function Fixture() {
  const messages = useMessages()
  const [modal, setModal] = useState<'short' | 'long' | null>(null)
  const [selected, setSelected] = useState('')
  const longLabel = `${messages.routes.documents.description} · ${'synthetic_long_document_name_'.repeat(5)}`
  const users: UserAccountRecord[] = Array.from({ length: 16 }, (_, index) => ({
    user_id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`,
    email: `synthetic_${index}@example.com`,
    display_name: index === 0 ? longLabel : `${messages.account.fields.name} ${index + 1}`,
    system_role: 'USER',
    status: 'ACTIVE',
    row_version: 1,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }))

  return <main className="accountsPage" style={{ maxWidth: 880, margin: '0 auto', padding: 16 }}>
    <h1>Popup and list spacing fixture</h1>
    <div className="inlineActions" style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
      <button id="open-short-modal" className="secondaryButton" type="button" onClick={() => setModal('short')}>{messages.account.edit}</button>
      <button id="open-long-modal" className="secondaryButton" type="button" onClick={() => setModal('long')}>{messages.account.securityEvents}</button>
      <ActionMenu id="short-menu" label="Short synthetic menu" items={[
        { id: 'edit', label: messages.account.edit, onSelect: () => setSelected('edit') },
        { id: 'close', label: messages.elements.close, onSelect: () => setSelected('close') },
      ]} />
      <ActionMenu id="long-menu" label="Long synthetic menu" items={Array.from({ length: 24 }, (_, index) => ({
        id: `item-${index}`,
        label: `${index + 1} · ${index % 3 === 0 ? longLabel : messages.account.securityEvents}`,
        separatorBefore: index === 12,
        onSelect: () => setSelected(`item-${index}`),
      }))} />
    </div>
    <output id="selected-action" style={{ display: 'block', overflowWrap: 'anywhere' }}>{selected}</output>
    <div className="accountsLayout" style={{ marginTop: 16 }}>
      <section id="short-users" className="panel accountDirectory">
        <h2>{messages.account.manageUsers}</h2>
        <UserSummaryList users={users.slice(0, 1)} disabled={false} onSelect={setSelected} />
      </section>
      <section id="long-users" className="panel accountDirectory">
        <h2>{messages.account.manageUsers}</h2>
        <UserSummaryList users={users} disabled={false} onSelect={setSelected} />
      </section>
      {[1, 24].map((count) => <section id={count === 1 ? 'short-events' : 'long-events'} className="panel accountEvents" key={count}>
        <h2>{messages.account.securityEvents}</h2>
        {/* 読込 hook を呼ばず、UserSecurityEvents と同じ scroll 容器と行構造を使う。 */}
        <ul className="accountEventList" tabIndex={0} aria-label={messages.account.securityEvents}>
          {Array.from({ length: count }, (_, index) => <li key={index}>
            <details><summary>{count === 1 || index % 3 !== 0 ? messages.account.securityEvents : longLabel} · {index + 1}</summary>
              <dl className="accountFacts"><div><dt>{messages.account.fields.version}</dt><dd>1</dd></div></dl>
            </details>
          </li>)}
        </ul>
      </section>)}
    </div>
    <ModalDialog open={modal !== null} title={messages.account.edit} onClose={() => setModal(null)}>
      {Array.from({ length: modal === 'long' ? 24 : 1 }, (_, index) => <label key={index}>
        {messages.account.fields.name} {index + 1}
        <input aria-label={`Synthetic field ${index + 1}`} defaultValue={index === 0 ? longLabel : `synthetic-${index}`} />
      </label>)}
    </ModalDialog>
  </main>
}

const query = new URLSearchParams(window.location.search)
const requestedLanguage = query.get('lang')
const language: UiLanguage = requestedLanguage === 'zh' || requestedLanguage === 'ja' ? requestedLanguage : 'en'
document.documentElement.lang = language
document.documentElement.dataset.theme = query.get('theme') === 'dark' ? 'dark' : 'light'
const root = document.getElementById('root')
if (!root) throw new Error('Fixture root missing')
createRoot(root).render(<LanguageProvider language={language}><Fixture /></LanguageProvider>)
