import { useEffect, useRef, useState, type FormEvent } from 'react'

import type { ProjectRecord, PublishedTaskRecord, ScheduleRecord, ScheduleStatus } from '../api'
import { DetailDrawer, EmptyState, LoadingSkeleton, PageHeader } from '../components/PageElements'
import { ScheduleActivityPanel } from '../components/ScheduleActivityPanel'
import { ScheduleEditDialog, ScheduleStatusActions, summarizeTiming } from '../components/ScheduleDialog'
import { useSchedules } from '../hooks/useSchedules'
import type { SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { routeHref } from '../lib/routing'
import { formatScheduleTimestamp } from '../lib/scheduleTime'
import { sameUuid } from '../lib/validation'
import '../styles/schedules.css'

/** 精確に認可済みの Project と現在 actor が、一覧と編集の所有者を決める。 */
export interface SchedulesPageProps {
  projectId: string
  actorId: string
  csrfToken: string
  currentProject: ProjectRecord | null
  onSessionEnded?: SessionEnded
}

/** 単独利用でも 401 後の業務成功を補造しない。通常 App は原会話の終了を受け持つ。 */
function retainSession(): void {}

/** 状態 owner の Review が唯一の失敗表示を担い、採用後に親へ古い警告を残さない。 */
function retainStatusFeedback(): void {}

/** Module/task catalog に依存しない、Project 全体の調度管理入口。 */
export function SchedulesPage(props: SchedulesPageProps) {
  return <ScheduleManager key={`${props.actorId.toLowerCase()}:${props.csrfToken}:${props.projectId.toLowerCase()}`} {...props} />
}

/** 行選択・一覧読取と原編集 owner を分離し、refresh で未決書込を消さない。 */
function ScheduleManager({ projectId, currentProject, csrfToken, onSessionEnded = retainSession }: SchedulesPageProps) {
  const messages = useMessages()
  const labels = messages.scheduleManager
  const authorized = Boolean(currentProject && sameUuid(currentProject.project_id, projectId))
  const readonly = !authorized || currentProject?.status !== 'ACTIVE'
  const state = useSchedules(projectId, onSessionEnded, authorized)
  const [q, setQ] = useState('')
  const [status, setStatus] = useState<ScheduleStatus | ''>('')
  const [editor, setEditor] = useState<{ schedule: ScheduleRecord; task: PublishedTaskRecord; revision: number } | null>(null)
  const [editorOpen, setEditorOpen] = useState(false)
  const [editorPending, setEditorPending] = useState(false)
  const [statusPending, setStatusPending] = useState(false)
  const editorLock = useRef(false)
  const editorPendingRef = useRef(false)
  const editorOpenRef = useRef(false)
  const statusLock = useRef(false)
  const heading = useRef<HTMLHeadingElement>(null)
  const selectionLock = editorOpen || editorPending || statusPending
  const detailReady = !state.detail.pending && !state.detail.failure && Boolean(state.record)
  const listReady = !state.list.pending && !state.list.failure && state.list.data !== null
  const queryInvalid = [...q.trim()].length > 200 || q.includes('\u0000')

  useEffect(() => {
    if (detailReady) heading.current?.focus()
  }, [state.selection.revision, detailReady])

  /** 検索は literal のまま送る。画面内の先頭頁を全件と見なして filter しない。 */
  function search(event: FormEvent): void {
    event.preventDefault()
    if (queryInvalid) return
    state.search(q, status)
  }
  /** 一覧行をそのまま書込へ使わず、新しい精確 GET と task 照合を確認する。 */
  function edit(): void {
    if (readonly || editorLock.current || editorPendingRef.current || statusLock.current
      || !state.canEdit() || !state.record || !state.task) return
    editorLock.current = true
    editorOpenRef.current = true
    setEditor((current) => ({
      schedule: state.record!, task: state.task!, revision: (current?.revision ?? 0) + 1,
    }))
    setEditorOpen(true)
  }
  /** 成功回执でも一覧/詳細は再読取する。別の未知 operation の成功証明には使わない。 */
  function changed(): void {
    state.list.refresh()
    state.refreshFacts()
  }

  return <div data-schedule-manager>
    <PageHeader title={messages.routes.schedules.label} description={messages.routes.schedules.description}
      aside={<a className="secondaryButton compactButton" href={routeHref('tasks', projectId)}>{labels.tasksLink}</a>} />
    {!authorized ? <section className="panel"><EmptyState text={labels.needProject} /></section> : <>
      <p className="hint">{currentProject!.name} · <span className="mono">{projectId}</span></p>
      {readonly && <p className="scheduleNotice" role="status">{labels.readOnlyProject}</p>}
      {state.readDenied && <p className="error" role="alert" data-schedule-read-denied>{labels.failures[state.readDenied.key]}</p>}
      <p className="hint">{labels.scopeHint}</p>
      <div className="scheduleManagerLayout">
        <section className="panel scheduleManagerList" aria-label={labels.listTitle}>
          <form className="scheduleFilters" onSubmit={search}>
            <label>{labels.searchLabel}<input data-schedule-search name="q" value={q}
              placeholder={labels.searchPlaceholder} onChange={(event) => setQ(event.target.value)} /></label>
            <label>{labels.statusLabel}<select data-schedule-filter value={status}
              onChange={(event) => setStatus(event.target.value as ScheduleStatus | '')}>
              <option value="">{labels.allStates}</option>
              {(['ACTIVE', 'PAUSED', 'COMPLETED', 'ERROR', 'ARCHIVED'] as const).map((value) => (
                <option key={value} value={value}>{messages.enums.scheduleStatus[value]}</option>
              ))}
            </select></label>
            <button className="primaryButton compactButton" disabled={queryInvalid} type="submit">{labels.search}</button>
            <button className="secondaryButton compactButton" data-schedule-refresh type="button"
              disabled={state.list.pending} onClick={state.list.refresh}>{labels.refresh}</button>
          </form>
          {queryInvalid && <p className="error" role="alert">{labels.invalidSearch}</p>}
          {state.list.pending && <LoadingSkeleton label={labels.loading} rows={3} />}
          {state.list.failure && <p className="error" role="alert">{labels.failures[state.list.failure.key]}</p>}
          {listReady && <>
            <p className="hint" data-schedule-total>{labels.total(state.list.data!.total)}</p>
            {state.list.data!.schedules.length === 0 ? <EmptyState text={labels.empty} /> : <ul className="scheduleRows"
              tabIndex={0} role="list" aria-label={labels.listTitle}>
              {state.list.data!.schedules.map((schedule) => <li key={schedule.schedule_id} data-schedule-row={schedule.schedule_id}>
                <button className="scheduleSelect" data-schedule-select={schedule.schedule_id} type="button"
                  disabled={selectionLock} aria-pressed={sameUuid(schedule.schedule_id, state.selection.id)}
                  onClick={() => {
                    if (editorLock.current || statusLock.current) return
                    setEditor(null); state.select(schedule.schedule_id)
                  }}>
                  <strong>{schedule.name}</strong>
                  <span className={`statusBadge scheduleStatus-${schedule.status.toLowerCase()}`}>{messages.enums.scheduleStatus[schedule.status]}</span>
                  <small className="mono">{schedule.schedule_id}</small>
                  <small>{summarizeTiming(schedule)}</small>
                </button>
              </li>)}
            </ul>}
            <nav className="schedulePager" aria-label={labels.pagination}>
              <button className="secondaryButton compactButton" type="button" data-schedule-previous
                disabled={state.filter.offset === 0} onClick={() => state.turnPage(Math.max(0, state.filter.offset - state.limit))}>{labels.previous}</button>
              <span>{labels.page(state.list.data!.offset, state.list.data!.limit, state.list.data!.total)}</span>
              <button className="secondaryButton compactButton" type="button" data-schedule-next
                disabled={state.list.data!.offset + state.list.data!.limit >= state.list.data!.total}
                onClick={() => state.turnPage(state.filter.offset + state.limit)}>{labels.next}</button>
            </nav>
          </>}
        </section>
        <section className="panel scheduleManagerDetail" data-schedule-detail aria-label={labels.detailTitle}>
          <h2 ref={heading} tabIndex={-1}>{labels.detailTitle}</h2>
          {!state.selection.id && <EmptyState text={labels.selectSchedule} />}
          {state.selection.id && <>
            <button className="secondaryButton compactButton" type="button" data-schedule-detail-refresh
              disabled={state.detail.pending} onClick={state.refreshDetail}>{labels.refresh}</button>
            {state.detail.pending && <p className="hint" role="status">{labels.loadingDetail}</p>}
            {state.detail.failure && <p className="error" role="alert">{labels.failures[state.detail.failure.key]}</p>}
            {state.catalog.pending && <p className="hint" role="status">{labels.taskLoading}</p>}
            {state.catalog.failure && <div data-schedule-catalog-failure><p className="error" role="alert">{labels.catalogUnavailable}</p>
              <button className="secondaryButton compactButton" type="button" onClick={state.refreshCatalog}>{labels.catalogRetry}</button></div>}
            {state.record && <>
              {!detailReady && <p className="hint">{labels.previousFacts}</p>}
              {!state.catalog.pending && !state.catalog.failure && !state.task && <p className="scheduleNotice" data-schedule-task-unavailable>{labels.taskUnavailable}</p>}
              {state.task && <p>{state.task.title} · {state.task.skill_name} v{state.task.version}</p>}
              {state.task && state.taskEligibility !== 'ready' && <p className="scheduleNotice" data-schedule-task-readonly>{
                state.taskEligibility === 'guidanceOnly' ? labels.guidanceOnly : labels.readinessUnconfirmed
              }</p>}
              <ScheduleDetails schedule={state.record} />
              <div className="formRow">
                <button className="primaryButton compactButton" type="button" data-schedule-edit
                  disabled={readonly || !state.canWrite || statusPending || editorPending
                    || ['COMPLETED', 'ARCHIVED'].includes(state.record.status)} onClick={edit}>{labels.edit}</button>
                <ScheduleStatusActions key={state.selection.revision} schedule={state.record} projectId={projectId} csrfToken={csrfToken}
                  disabled={readonly || !state.canWrite || editorOpen || editorPending}
                  isWriteAllowed={() => !editorLock.current && state.canEdit()}
                  onSessionEnded={onSessionEnded} onPendingChange={(pending) => { statusLock.current = pending; setStatusPending(pending) }}
                  onChanged={changed} onError={retainStatusFeedback} />
              </div>
              <p className="hint">{labels.controlHint}</p>
            </>}
            <ScheduleActivityPanel data={state.activity.data} pending={state.activity.pending}
              failure={state.activity.failure} onRefresh={state.activity.refresh} />
          </>}
        </section>
      </div>
      {editor && <>
        {!editorOpen && editorPending && <div className="scheduleNotice" role="status">{labels.editorPending}
          <button className="secondaryButton compactButton" type="button" data-schedule-reopen
            onClick={() => { editorLock.current = true; editorOpenRef.current = true; setEditorOpen(true) }}>{labels.reopenEditor}</button></div>}
        <ScheduleEditDialog key={`${editor.schedule.schedule_id}:${editor.revision}`} schedule={editor.schedule} task={editor.task}
          projectId={projectId} csrfToken={csrfToken} open={editorOpen} disabled={readonly || statusPending || !state.canWrite}
          isWriteAllowed={() => !statusLock.current && state.canEdit()}
          onSessionEnded={onSessionEnded} onPendingChange={(pending) => {
            editorPendingRef.current = pending; editorLock.current = editorOpenRef.current || pending; setEditorPending(pending)
          }}
          onClose={() => { editorOpenRef.current = false; editorLock.current = editorPendingRef.current; setEditorOpen(false) }}
          onSaved={() => {
            editorOpenRef.current = false; editorPendingRef.current = false; editorLock.current = false
            setEditorOpen(false); setEditorPending(false); changed()
          }} />
      </>}
    </>}
  </div>
}

/** 原 input/sources と上書きされる摘要を分け、摘要を発火台帳や成功数に見せない。 */
export function ScheduleDetails({ schedule }: { schedule: ScheduleRecord }) {
  const messages = useMessages()
  const labels = messages.scheduleManager
  const stamp = (value: string | null): string => value ? formatScheduleTimestamp(value, schedule.timezone) : '—'
  return <div className="scheduleFacts">
    <h3>{schedule.name}</h3>
    <dl>
      <div><dt>{labels.statusLabel}</dt><dd>{messages.enums.scheduleStatus[schedule.status]}</dd></div>
      <div><dt>{labels.fields.task}</dt><dd className="mono">{schedule.task_key}</dd></div>
    </dl>
    <DetailDrawer title={messages.elements.technicalDetails}>
    <dl>
      <div><dt>{labels.fields.id}</dt><dd className="mono">{schedule.schedule_id}</dd></div>
      <div><dt>{labels.fields.version}</dt><dd className="mono">{schedule.skill_version_id}</dd></div>
      <div><dt>{labels.fields.rowVersion}</dt><dd>{schedule.row_version}</dd></div>
      <div><dt>{labels.fields.creator}</dt><dd className="mono">{schedule.created_by}</dd></div>
      <div><dt>{labels.fields.created}</dt><dd>{stamp(schedule.created_at)}</dd></div>
      <div><dt>{labels.fields.updated}</dt><dd>{stamp(schedule.updated_at)}</dd></div>
    </dl>
    </DetailDrawer>
    <h3>{labels.definitionTitle}</h3>
    <dl>
      <div><dt>{messages.schedules.kindLabel}</dt><dd>{messages.enums.scheduleKind[schedule.kind]}</dd></div>
      <div><dt>{messages.schedules.timezoneLabel}</dt><dd>{schedule.timezone}</dd></div>
      <div><dt>{schedule.kind === 'CRON' ? messages.schedules.cronLabel : messages.schedules.runAtLabel}</dt>
        <dd>{schedule.kind === 'CRON' ? schedule.cron_expression : stamp(schedule.run_at)}</dd></div>
      <div><dt>{messages.schedules.endAtLabel}</dt><dd>{stamp(schedule.end_at)}</dd></div>
      <div><dt>{messages.schedules.maxRunsLabel}</dt><dd>{schedule.max_runs ?? labels.unlimited}</dd></div>
      <div><dt>{labels.fields.nextAt}</dt><dd>{stamp(schedule.next_run_at)}</dd></div>
    </dl>
    <h3>{labels.summaryTitle}</h3><p className="hint">{labels.summaryHint}</p>
    <dl>
      <div><dt>{labels.fields.runCount}</dt><dd>{schedule.run_count}</dd></div>
      <div><dt>{labels.fields.missedCount}</dt><dd>{schedule.missed_count}</dd></div>
      <div><dt>{labels.fields.lastAt}</dt><dd>{stamp(schedule.last_run_at)}</dd></div>
      <div><dt>{labels.fields.lastOutcome}</dt><dd>{schedule.last_outcome ? labels.outcomes[schedule.last_outcome] : '—'}</dd></div>
      <div><dt>{labels.fields.lastRun}</dt><dd>{schedule.last_run_id
        ? <a className="mono" href={routeHref('workspace', schedule.project_id, { runId: schedule.last_run_id })}>{schedule.last_run_id}</a> : '—'}</dd></div>
    </dl>
    {schedule.last_error && <p className="hint">{schedule.last_error}</p>}
    <details><summary>{labels.inputTitle}</summary><pre>{JSON.stringify(schedule.input, null, 2)}</pre></details>
    <details><summary>{labels.sourcesTitle}</summary><pre>{JSON.stringify(schedule.sources, null, 2)}</pre></details>
  </div>
}
