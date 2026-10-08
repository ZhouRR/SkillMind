import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import type { AuthSessionRecord } from '../../src/api'
import { ResourceConnectionForm } from '../../src/components/ResourceConnectionForm'
import { ScheduleRecurrenceField } from '../../src/components/ScheduleRecurrenceField'
import { SchemaTaskInput } from '../../src/components/SchemaTaskInput'
import { Select } from '../../src/components/Select'
import { UserCreatePanel } from '../../src/components/UserCreatePanel'
import { LanguageProvider, useMessages } from '../../src/i18n'
import type { UiLanguage } from '../../src/lib/i18n/messages'
import { emptyConnectDraft } from '../../src/lib/resourceDrafts'
import '../../src/styles.css'
import '../../src/styles/evaluations.css'

/** 作成ボタンは押さず、初期表示と短い role 選択だけを使う合成 session。 */
const session: AuthSessionRecord = {
  user: { user_id: '00000000-0000-4000-8000-000000000001', organization_id: '00000000-0000-4000-8000-000000000002',
    email: 'synthetic@example.invalid', display_name: 'Synthetic user', system_role: 'ADMIN' },
  csrf_token: 'synthetic-not-a-credential', absolute_expires_at: '2099-01-01T00:00:00Z',
}

/** 実 form と同じ局所 layout、実 Select を通信なしで測る。実消費側への指定は component 回帰も確認する。 */
function Fixture() {
  const messages = useMessages()
  const [schemaValue, setSchemaValue] = useState<Record<string, unknown>>({ enabled: true, project: 'long' })
  const [recurrence, setRecurrence] = useState('30 9 * * 1')
  const [connection, setConnection] = useState(emptyConnectDraft('postgres'))
  const [status, setStatus] = useState('')
  const [score, setScore] = useState('3')
  const longLabel = `${messages.routes.documents.description} · ${'synthetic_long_document_name_'.repeat(6)}`
  return <main style={{ maxWidth: 1024, margin: '0 auto', padding: 16 }}>
    <h1>Short form control fixture</h1>
    <section className="panel" id="filters">
      <div className="formRow documentManagementToolbar">
        <input aria-label={messages.fileManagement.search} />
        <label className="documentSort shortField">{messages.fileManagement.sort}
          <Select id="sort" className="shortControl shortControlNarrow" density="compact" defaultValue="name">
            <option value="name">{messages.fileManagement.byName}</option><option value="date">{messages.fileManagement.byDate}</option><option value="size">{messages.fileManagement.bySize}</option>
          </Select></label>
      </div>
      <div className="skillLibraryFilters">
        <label>{messages.skills.librarySearch}<input /></label>
        <label className="shortField">{messages.skills.libraryStatus}
          <Select id="skill-status" className="shortControl" density="compact" defaultValue="all">
            <option value="all">{messages.skills.libraryAllStatuses}</option>
            {(['PUBLISHED', 'DRAFT', 'DEPRECATED'] as const).map((value) => <option key={value} value={value}>{messages.enums.skillVersionStatus[value]}</option>)}
          </Select></label>
      </div>
      <div className="taskFilters" style={{ marginTop: 16 }}>
        <label>{messages.scheduleManager.searchLabel}<input /></label>
        <label className="shortField">{messages.scheduleManager.statusLabel}
          <Select id="task-status" className="shortControl" density="compact" value={status} onValueChange={setStatus}>
            <option value="">{messages.scheduleManager.allStates}</option><option value="UNCONFIGURED">{messages.tasks.noSchedule}</option>
            {(['ACTIVE', 'PAUSED', 'COMPLETED', 'ERROR', 'ARCHIVED'] as const).map((value) => <option key={value} value={value}>{messages.enums.scheduleStatus[value]}</option>)}
          </Select></label>
      </div>
    </section>
    <UserCreatePanel session={session} onSessionEnded={() => {}} onCreated={() => {}} onSelect={() => {}} />
    <section className="panel evaluationSection" id="evaluation" style={{ marginTop: 16 }}>
      <div className="formRow evaluationRatingFields">
        <label>{messages.runResult.ratingLabel}<Select id="score" className="shortControl shortControlNarrow" density="compact" value={score} onValueChange={setScore}>
          {[1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>{value}</option>)}</Select></label>
        <label>{messages.runResult.verdictLabel}<Select id="verdict" defaultValue="partially_accurate">
          {(['accurate', 'partially_accurate', 'inaccurate', 'uncertain'] as const).map((value) => <option key={value} value={value}>{messages.enums.verdict[value]}</option>)}</Select></label>
      </div>
      <label>{messages.account.fields.name}<Select id="long-short-label" className="shortControl" density="compact" defaultValue="long">
        <option value="short">{messages.elements.close}</option><option value="long">{longLabel}</option>
      </Select></label>
    </section>
    <section className="panel" id="schema" style={{ marginTop: 16 }}>
      <SchemaTaskInput schema={{ type: 'object', properties: {
        enabled: { type: 'boolean', title: messages.account.fields.status },
        project: { type: 'string', title: messages.elements.projectLabel, enum: ['long', longLabel] },
      } }} rawValue={JSON.stringify(schemaValue)} onRawChange={() => {}} value={schemaValue} onChange={setSchemaValue} />
    </section>
    <section className="panel scheduleConfiguration" id="schedule" style={{ marginTop: 16 }}>
      <label>{messages.schedules.kindLabel}<Select id="kind" className="shortControl" density="compact" defaultValue="CRON">
        <option value="CRON">{messages.enums.scheduleKind.CRON}</option><option value="ONCE">{messages.enums.scheduleKind.ONCE}</option>
      </Select></label>
      <ScheduleRecurrenceField value={recurrence} onChange={setRecurrence} />
      <label>{messages.schedules.maxRunsLabel}<input className="shortNumberControl" name="max_runs" type="number" min={1} max={100000} defaultValue={100000} /></label>
      <label>{messages.schedules.runAtLabel}<input type="datetime-local" name="run_at" /></label>
    </section>
    <section className="panel" id="resource" style={{ marginTop: 16 }}>
      <ResourceConnectionForm connectDraft={connection} setConnectDraft={setConnection} secrets={[]} editingIntegration={null}
        connectWriteEnabled={false} mcpToolsEnabled={false} busy={null} error={null} locked={false}
        feedback={null} submitConnect={async () => {}} discoverTools={async () => {}} onClose={() => {}} />
    </section>
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
