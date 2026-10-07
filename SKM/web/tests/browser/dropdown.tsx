import { useEffect, useState, type FormEvent } from 'react'
import { createRoot } from 'react-dom/client'

import { AppNavigation } from '../../src/components/AppNavigation'
import { ModalDialog } from '../../src/components/PageElements'
import { Select } from '../../src/components/Select'
import { LanguageProvider, useMessages } from '../../src/i18n'
import type { UiLanguage } from '../../src/lib/i18n/messages'
import { HomePage } from '../../src/pages/HomePage'
import { DEMO_PROJECT, demoUser } from '../fixtures'
import '../../src/styles.css'

/** 共有 Select の controlled/form/disabled/popup/modal 契約を API なしで観測する。 */
function Fixture() {
  const messages = useMessages()
  const [value, setValue] = useState('alpha')
  const [changes, setChanges] = useState<string[]>([])
  const [required, setRequired] = useState('')
  const [numeric, setNumeric] = useState(1)
  const [long, setLong] = useState('long-0')
  const [modalValue, setModalValue] = useState('alpha')
  const [modal, setModal] = useState(false)
  const [submitted, setSubmitted] = useState('')
  const [listbox, setListbox] = useState('alpha')
  const [multiple, setMultiple] = useState(['alpha'])
  const onChange = (nextValue: string) => {
    setValue(nextValue)
    setChanges((previous) => [...previous, nextValue])
  }
  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setSubmitted(JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))))
  }
  return <main style={{ maxWidth: 880, margin: '0 auto', padding: '20px 16px 170px' }}>
    <h1>Shared dropdown fixture</h1>
    <p className="hint">{messages.routes.projects.description}</p>
    <form id="dropdown-form" className="panel" onSubmit={onSubmit} onReset={() => {
      // Controlled form は他の入力と同様、所有者が reset 後の state を復元する。
      setValue('alpha')
      setRequired('')
      setNumeric(1)
      setChanges([])
      setSubmitted('')
    }} style={{ maxWidth: 460 }}>
      <label htmlFor="basic-select">{messages.elements.projectLabel}</label>
      <Select id="basic-select" name="project" value={value} onValueChange={onChange}>
        <option value="alpha">Alpha · {messages.routes.projects.label}</option>
        <option value="blocked" disabled>Beta unavailable · {messages.elements.projectUnavailable}</option>
        <optgroup label={messages.elements.archivedProject} disabled>
          <option value="archived-a">{messages.routes.history.label}</option>
          <option value="archived-b">{messages.routes.schedules.label}</option>
        </optgroup>
        <option value="beta">Beta · {messages.routes.documents.label}</option>
        <option value="gamma">Gamma · {messages.routes.skills.label}</option>
      </Select>
      <output id="selection" style={{ display: 'block', marginBlock: 8 }}>{value}</output>
      <output id="change-count" style={{ display: 'block', marginBlock: 8 }}>{changes.length}</output>
      <output id="change-log" hidden>{JSON.stringify(changes)}</output>
      <button id="after-basic" type="button" className="secondaryButton">{messages.elements.close}</button>
      <label htmlFor="required-select">{messages.elements.selectProject}</label>
      <Select id="required-select" name="requiredProject" required value={required} onValueChange={setRequired}>
        <option value="" disabled>{messages.elements.selectProject}</option>
        <option value="required-alpha">{messages.routes.projects.label}</option>
        <option value="required-beta">{messages.routes.documents.label}</option>
      </Select>
      <label htmlFor="numeric-select">{messages.routes.schedules.label}</label>
      <Select id="numeric-select" name="numericProject" value={numeric} onValueChange={(next) => setNumeric(Number(next))}>
        <option value={1}>1 · {messages.routes.projects.label}</option>
        <option value={2}>2 · {messages.routes.documents.label}</option>
      </Select>
      <output id="numeric-selection">{numeric}</output>
      <label htmlFor="disabled-select" style={{ marginTop: 12 }}>{messages.elements.noAccessibleProjects}</label>
      <Select id="disabled-select" name="disabledProject" disabled value="unavailable" onValueChange={() => setChanges((previous) => [...previous, 'unexpected'])}>
        <option value="unavailable">{messages.elements.projectUnavailable}</option>
        <option value="unexpected">{messages.routes.projects.label}</option>
      </Select>
      <button id="submit-form" type="submit" className="secondaryButton" style={{ marginTop: 12 }}>{messages.account.save}</button>
      <button id="reset-form" type="reset" className="secondaryButton">Reset</button>
      <output id="submitted" style={{ display: 'block', overflowWrap: 'anywhere' }}>{submitted}</output>
    </form>
    <form id="uncontrolled-form" className="panel" style={{ maxWidth: 460, marginTop: 12 }}>
      <label htmlFor="uncontrolled-select">{messages.elements.projectLabel}</label>
      <Select id="uncontrolled-select" name="uncontrolledProject" defaultValue="beta">
        <option value="alpha">Alpha · {messages.routes.projects.label}</option>
        <option value="beta">Beta · {messages.routes.documents.label}</option>
        <option value="gamma">Gamma · {messages.routes.skills.label}</option>
      </Select>
      <button id="reset-uncontrolled" type="reset" className="secondaryButton">Reset</button>
    </form>
    <button id="open-modal" type="button" className="secondaryButton" onClick={() => setModal(true)} style={{ marginTop: 16 }}>{messages.account.edit}</button>
    <button id="outside" type="button" className="secondaryButton" style={{ margin: 16 }}>{messages.elements.close}</button>
    <section className="panel" style={{ display: 'grid', gap: 12, maxWidth: 460, marginTop: 12 }}>
      <label>size=3
        <select id="listbox-select" size={3} value={listbox} onChange={(event) => setListbox(event.target.value)}>
          <option value="alpha">{messages.routes.projects.label}</option><option value="beta">{messages.routes.documents.label}</option><option value="gamma">{messages.routes.skills.label}</option>
        </select>
      </label>
      <label>multiple
        <select id="multiple-select" multiple value={multiple} onChange={(event) => setMultiple(Array.from(event.target.selectedOptions, (option) => option.value))}>
          <option value="alpha">{messages.routes.projects.label}</option><option value="beta">{messages.routes.documents.label}</option><option value="gamma">{messages.routes.skills.label}</option>
        </select>
      </label>
    </section>
    <div id="clipped-container" style={{ position: 'fixed', bottom: 10, right: 10, width: 'min(320px, calc(100vw - 20px))', height: 126, overflow: 'hidden', padding: 8, border: '1px solid var(--border)', borderRadius: 10, background: 'var(--surface)' }}>
      <label htmlFor="long-select">{messages.routes.documents.label}</label>
      <Select id="long-select" value={long} onValueChange={setLong}>
        {Array.from({ length: 36 }, (_, index) => <option key={index} value={`long-${index}`}>
          {`${String(index + 1).padStart(2, '0')} · ${messages.routes.documents.description} · project_document_${'long_name_'.repeat(5)}${index}`}
        </option>)}
      </Select>
      <output id="long-selection" hidden>{long}</output>
    </div>
    <ModalDialog open={modal} title={messages.account.edit} onClose={() => setModal(false)}>
      <button id="modal-before" type="button" className="secondaryButton">{messages.routes.projects.label}</button>
      <label htmlFor="modal-select">{messages.elements.projectLabel}</label>
      <Select id="modal-select" value={modalValue} onValueChange={setModalValue}>
        <option value="alpha">{messages.routes.projects.label}</option>
        <option value="beta">{messages.routes.documents.label}</option>
        <option value="gamma">{messages.routes.skills.label}</option>
      </Select>
      <button id="modal-after" type="button" className="secondaryButton">{messages.routes.documents.label}</button>
      <output id="modal-selection">{modalValue}</output>
    </ModalDialog>
  </main>
}

/** 実際の HomePage と導航を同じ Provider で描画し、言語変更の到達先を検証する。 */
function NavigationFixture({ initialLanguage }: { initialLanguage: UiLanguage }) {
  const [language, setLanguage] = useState(initialLanguage)
  const [project, setProject] = useState(DEMO_PROJECT.project_id)
  const [languageChanges, setLanguageChanges] = useState(0)
  useEffect(() => { document.documentElement.lang = language }, [language])
  const metaState = { status: 'ready' as const, meta: { name: 'skillmind', version: 'fixture', phase: 'test', task: 'dropdown', ingress: '/skillmind' } }
  return <LanguageProvider language={language}>
    <div className="appFrame">
      <AppNavigation
        currentRoute="home"
        metaState={metaState}
        projectId={project}
        pendingProjectId=""
        projectState={{ status: 'ready', projects: [DEMO_PROJECT, { ...DEMO_PROJECT, project_id: '00000000-0000-4000-8000-000000000011', name: 'Synthetic alternate project', key: 'alternate' }] }}
        onSelectLanguage={(next) => { setLanguage(next); setLanguageChanges((count) => count + 1) }}
        onSelectProject={setProject}
        onSelectModule={() => undefined}
        activeModuleId=""
        modules={[]}
        user={demoUser()}
        onLogout={() => undefined}
        logoutError={null}
      />
      <main className="shell" data-page="home">
        {/* 未選択の HomePage は実装自身が API を呼ばないため、通信を mock に差替える必要がない。 */}
        <HomePage metaState={metaState} project={null} projectId="" />
        <output id="navigation-language" hidden>{language}</output>
        <output id="navigation-language-changes" hidden>{languageChanges}</output>
        <output id="navigation-project" hidden>{project}</output>
      </main>
    </div>
  </LanguageProvider>
}

const query = new URLSearchParams(window.location.search)
const requestedLanguage = query.get('lang')
const language: UiLanguage = requestedLanguage === 'zh' || requestedLanguage === 'ja' ? requestedLanguage : 'en'
document.documentElement.lang = language
document.documentElement.dataset.theme = query.get('theme') === 'dark' ? 'dark' : 'light'
const root = document.getElementById('root')
if (!root) throw new Error('Fixture root missing')
createRoot(root).render(query.get('navigation') === '1'
  ? <NavigationFixture initialLanguage={language} />
  : <LanguageProvider language={language}><Fixture /></LanguageProvider>)
