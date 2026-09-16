import { useEffect, useRef, useState } from 'react'

import type { ProjectRecord, PublishedTaskRecord, ScheduleRecord } from '../api'
import { DetailDrawer, EmptyState } from '../components/PageElements'
import { ScheduleActivityPanel } from '../components/ScheduleActivityPanel'
import { ScheduleEditDialog, ScheduleStatusActions } from '../components/ScheduleDialog'
import { useSchedules } from '../hooks/useSchedules'
import type { SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { formatScheduleTimestamp } from '../lib/scheduleTime'
import { routeHref } from '../lib/routing'
import { sameUuid } from '../lib/validation'
import '../styles/schedules.css'

/** Task カードから選んだ原 Schedule の詳細・編集・状態変更を一つの owner に固定する。 */
interface TaskScheduleDetailsProps {
  projectId: string
  scheduleId: string
  csrfToken: string
  currentProject: ProjectRecord | null
  schedulingEnabled: boolean
  onSessionEnded: SessionEnded
  onChanged: () => void
  onBusyChange: (busy: boolean) => void
}

/** 各 mutation の owner が失敗を表示し、別の古いエラーを重ねない。 */
function retainStatusFeedback(): void {}

/** フィルタや一覧再読込から独立し、未知の書込と元草稿を維持する。 */
export function TaskScheduleDetails({ projectId, scheduleId, csrfToken, currentProject,
  schedulingEnabled, onSessionEnded, onChanged, onBusyChange }: TaskScheduleDetailsProps) {
  const messages = useMessages()
  const labels = messages.scheduleManager
  const authorized = Boolean(currentProject && sameUuid(currentProject.project_id, projectId))
  const readonly = !authorized || currentProject?.status !== 'ACTIVE'
  const state = useSchedules(projectId, onSessionEnded, authorized, scheduleId)
  const [editor, setEditor] = useState<{ schedule: ScheduleRecord; task: PublishedTaskRecord; revision: number } | null>(null)
  const [editorOpen, setEditorOpen] = useState(false)
  const [editorPending, setEditorPending] = useState(false)
  const [statusPending, setStatusPending] = useState(false)
  const editorLock = useRef(false)
  const editorPendingRef = useRef(false)
  const editorOpenRef = useRef(false)
  const statusLock = useRef(false)
  const heading = useRef<HTMLHeadingElement>(null)
  const detailReady = !state.detail.pending && !state.detail.failure && Boolean(state.record)
  useEffect(() => { if (detailReady) heading.current?.focus() }, [detailReady])
  useEffect(() => { onBusyChange(editorOpen || editorPending || statusPending) },
    [editorOpen, editorPending, statusPending, onBusyChange])

  /** 最新の精確 GET と Task を照合してから編集草稿を固定する。 */
  function edit(): void {
    if (readonly || editorLock.current || editorPendingRef.current || statusLock.current
      || !state.canEdit() || !state.record || !state.task) return
    editorLock.current = true
    onBusyChange(true)
    editorOpenRef.current = true
    setEditor((current) => ({ schedule: state.record!, task: state.task!, revision: (current?.revision ?? 0) + 1 }))
    setEditorOpen(true)
  }
  /** 成功回执は詳細/親カードの再読取を促し、他の未知操作を成功と断定しない。 */
  function changed(): void { state.refreshFacts(); onChanged() }

  return !authorized ? <EmptyState text={labels.needProject} /> : <div data-task-schedule-details>
    {readonly && <p className="scheduleNotice">{labels.readOnlyProject}</p>}
    {state.readDenied && <p className="error" role="alert" data-schedule-read-denied>{labels.failures[state.readDenied.key]}</p>}
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
                  disabled={!schedulingEnabled || readonly || !state.canWrite || statusPending || editorPending
                    || ['COMPLETED', 'ARCHIVED'].includes(state.record.status)} onClick={edit}>{labels.edit}</button>
                <ScheduleStatusActions key={state.selection.revision} schedule={state.record} projectId={projectId} csrfToken={csrfToken}
                  allowResume={schedulingEnabled}
                  disabled={readonly || !state.canManageRecord || editorOpen || editorPending}
                  isWriteAllowed={() => !editorLock.current && state.canManage()}
                  onSessionEnded={onSessionEnded} onPendingChange={(pending) => { statusLock.current = pending; if (pending) onBusyChange(true); setStatusPending(pending) }}
                  onChanged={changed} onError={retainStatusFeedback} />
              </div>
              <p className="hint">{labels.controlHint}</p>
            </>}
            <ScheduleActivityPanel data={state.activity.data} pending={state.activity.pending}
              failure={state.activity.failure} onRefresh={state.activity.refresh} />
          </>}
        </section>
      {editor && <>
        {!editorOpen && editorPending && <div className="scheduleNotice" role="status">{labels.editorPending}
          <button className="secondaryButton compactButton" type="button" data-schedule-reopen
            onClick={() => { editorLock.current = true; editorOpenRef.current = true; setEditorOpen(true) }}>{labels.reopenEditor}</button></div>}
        <ScheduleEditDialog key={`${editor.schedule.schedule_id}:${editor.revision}`} schedule={editor.schedule} task={editor.task}
          projectId={projectId} csrfToken={csrfToken} open={editorOpen} disabled={!schedulingEnabled || readonly || statusPending || !state.canWrite}
          isWriteAllowed={() => !statusLock.current && state.canEdit()}
          onSessionEnded={onSessionEnded} onPendingChange={(pending) => {
            if (pending) onBusyChange(true)
            editorPendingRef.current = pending; editorLock.current = editorOpenRef.current || pending; setEditorPending(pending)
          }}
          onClose={() => { editorOpenRef.current = false; editorLock.current = editorPendingRef.current; setEditorOpen(false) }}
          onSaved={() => {
            editorOpenRef.current = false; editorPendingRef.current = false; editorLock.current = false
            setEditorOpen(false); setEditorPending(false); changed()
          }} />
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
        ? <a className="mono" href={routeHref('history', schedule.project_id, { runId: schedule.last_run_id })}>{schedule.last_run_id}</a> : '—'}</dd></div>
    </dl>
    {schedule.last_error && <p className="hint">{schedule.last_error}</p>}
    <details><summary>{labels.inputTitle}</summary><pre>{JSON.stringify(schedule.input, null, 2)}</pre></details>
    <details><summary>{labels.sourcesTitle}</summary><pre>{JSON.stringify(schedule.sources, null, 2)}</pre></details>
  </div>
}
