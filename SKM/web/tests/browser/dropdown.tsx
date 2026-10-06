import { useState, type ChangeEvent, type FormEvent } from 'react'
import { createRoot } from 'react-dom/client'

import { AppNavigation } from '../../src/components/AppNavigation'
import { DEMO_PROJECT, demoUser } from '../fixtures'
import { ModalDialog } from '../../src/components/PageElements'
import { LanguageProvider, useMessages } from '../../src/i18n'
import type { UiLanguage } from '../../src/lib/i18n/messages'
import '../../src/styles/base.css'
import '../../src/styles/shell.css'
import '../../src/styles/presentation.css'

/** 共有 catalog の長文を再利用し、三語の折返しを実データに依存せず確認する。 */
function LongOptions() {
  const messages = useMessages()
  return <>
    {Array.from({ length: 36 }, (_, index) => <option key={index} value={`long-${index}`}>
      {`${String(index + 1).padStart(2, '0')} · ${messages.routes.documents.description} · ${messages.routes.schedules.description} · project_document_${'long_name_'.repeat(7)}${index}`}
    </option>)}
  </>
}

/** React controlled の native form/disabled/picker/modal を API なしで観測する隔離 fixture。 */
function Fixture() {
  const messages = useMessages()
  const [value, setValue] = useState('alpha')
  const [changes, setChanges] = useState(0)
  const [required, setRequired] = useState('')
  const [long, setLong] = useState('long-0')
  const [modalValue, setModalValue] = useState('alpha')
  const [modal, setModal] = useState(false)
  const [submitted, setSubmitted] = useState('')
  const [sizeOne, setSizeOne] = useState('alpha')
  const [listbox, setListbox] = useState('alpha')
  const [multiple, setMultiple] = useState(['alpha'])
  const onChange = (event: ChangeEvent<HTMLSelectElement>) => {
    setValue(event.target.value)
    setChanges((count) => count + 1)
  }
  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setSubmitted(JSON.stringify(Object.fromEntries(new FormData(event.currentTarget))))
  }
  return <main style={{ maxWidth: 880, margin: '0 auto', padding: '20px 16px 170px' }}>
    <h1>Native dropdown fixture</h1>
    <p className="hint">{messages.routes.projects.description}</p>
    <form id="native-form" className="panel" onSubmit={onSubmit} style={{ maxWidth: 460 }}>
      <label htmlFor="basic-select">{messages.elements.projectLabel}</label>
      <select id="basic-select" name="project" value={value} onChange={onChange}>
        <option value="alpha">{messages.routes.projects.label}</option>
        <option value="blocked" disabled>{messages.elements.projectUnavailable}</option>
        <optgroup label={messages.elements.archivedProject} disabled>
          <option value="archived-a">{messages.routes.history.label}</option>
          <option value="archived-b">{messages.routes.schedules.label}</option>
        </optgroup>
        <option value="beta">{messages.routes.documents.label}</option>
        <option value="gamma">{messages.routes.skills.label}</option>
      </select>
      <output id="selection" style={{ display: 'block', marginBlock: 8 }}>{value}</output>
      <output id="change-count" style={{ display: 'block', marginBlock: 8 }}>{changes}</output>
      <label htmlFor="required-select">{messages.elements.selectProject}</label>
      <select id="required-select" name="requiredProject" required value={required} onChange={(event) => setRequired(event.target.value)}>
        <option value="" disabled>{messages.elements.selectProject}</option>
        <option value="required-alpha">{messages.routes.projects.label}</option>
        <option value="required-beta">{messages.routes.documents.label}</option>
      </select>
      <label htmlFor="disabled-select" style={{ marginTop: 12 }}>{messages.elements.noAccessibleProjects}</label>
      <select id="disabled-select" name="disabledProject" disabled value="unavailable" onChange={() => setChanges((count) => count + 100)}>
        <option value="unavailable">{messages.elements.projectUnavailable}</option>
        <option value="unexpected">{messages.routes.projects.label}</option>
      </select>
      <button id="submit-form" type="submit" className="secondaryButton" style={{ marginTop: 12 }}>{messages.account.save}</button>
      <output id="submitted" style={{ display: 'block', overflowWrap: 'anywhere' }}>{submitted}</output>
    </form>
    <button id="open-modal" type="button" className="secondaryButton" onClick={() => setModal(true)} style={{ marginTop: 16 }}>{messages.account.edit}</button>
    <button id="outside" type="button" className="secondaryButton" style={{ margin: 16 }}>{messages.elements.close}</button>
    <section className="panel" style={{ display: 'grid', gap: 12, maxWidth: 460, marginTop: 12 }}>
      <label>size=1
        <select id="size-one-select" size={1} value={sizeOne} onChange={(event) => setSizeOne(event.target.value)}>
          <option value="alpha">{messages.routes.projects.label}</option><option value="beta">{messages.routes.documents.label}</option>
        </select>
      </label>
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
      <select id="long-select" value={long} onChange={(event) => setLong(event.target.value)}><LongOptions /></select>
    </div>
    <ModalDialog open={modal} title={messages.account.edit} onClose={() => setModal(false)}>
      <label htmlFor="modal-select">{messages.elements.projectLabel}</label>
      <select id="modal-select" value={modalValue} onChange={(event) => setModalValue(event.target.value)}>
        <option value="alpha">{messages.routes.projects.label}</option>
        <option value="beta">{messages.routes.documents.label}</option>
        <option value="gamma">{messages.routes.skills.label}</option>
      </select>
      <output id="modal-selection">{modalValue}</output>
    </ModalDialog>
  </main>
}

/** 本物の緊凑導航を local state だけで動かし、pending badge の API 読取を明示的に止める。 */
function NavigationFixture({ initialLanguage }: { initialLanguage: UiLanguage }) {
  const [language, setLanguage] = useState(initialLanguage)
  const [project, setProject] = useState(DEMO_PROJECT.project_id)
  return <LanguageProvider language={language}>
    <div className="appFrame">
      <AppNavigation
        currentRoute="home"
        metaState={{ status: 'ready', meta: { name: 'skillmind', version: 'fixture', phase: 'test', task: 'dropdown', ingress: '/skillmind' } }}
        projectId={project}
        pendingProjectId=""
        projectState={{ status: 'ready', projects: [DEMO_PROJECT, { ...DEMO_PROJECT, project_id: '00000000-0000-4000-8000-000000000011', name: 'Synthetic alternate project', key: 'alternate' }] }}
        onSelectLanguage={setLanguage}
        onSelectProject={setProject}
        onSelectModule={() => undefined}
        activeModuleId=""
        modules={[]}
        user={demoUser()}
        onLogout={() => undefined}
        logoutError={null}
      />
      <main className="shell"><h1>Navigation dropdown fixture</h1></main>
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
