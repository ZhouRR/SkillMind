import { RunDuration } from '../components/RunDuration'
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'

import {
  isValidTaskFlowTarget,
  loadProjectModules,
  loadProjectSchedules,
  loadProjectTasks,
  type ProjectModuleRecord,
  type ProjectRecord,
  type PublishedTaskRecord,
  type ScheduleRecord,
} from '../api'
import { EmptyState, LoadingSkeleton, PageHeader, StatusBadge } from '../components/PageElements'
import { ScheduleDialog, summarizeTiming } from '../components/ScheduleDialog'
import { TaskScheduleDetails } from '../components/TaskScheduleDetails'
import { matchesTaskScheduleFilter, schedulesForTask, unavailableScheduledTasks, type TaskScheduleStatusFilter } from '../lib/taskScheduleFilter'
import { TaskFlowPreview } from '../components/TaskFlowPreview'
import { ROUTE_ICONS } from '../components/routeIcons'
import { useTaskFlowPreview } from '../hooks/useTaskFlowPreview'
import type { SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { formatLocalTimestamp } from '../lib/presentation'
import { formatScheduleTimestamp } from '../lib/scheduleTime'
import { routeHref } from '../lib/routing'
import { filterTasksByModule, sourceRequirements, taskCatalogId } from '../lib/taskDraft'

/** 一覧に要る取得結果。上次执行は task descriptor に同梱されて返る。 */
interface TaskCenterState {
  tasks: PublishedTaskRecord[]
  schedules: ScheduleRecord[]
}

type LoadState =
  | { status: 'loading' }
  | { status: 'ready'; data: TaskCenterState }
  | { status: 'error'; message: string }

/** 現在 Project で「何を走らせられるか」を一覧する任务中心（docs/07 §8）。
 *
 * 工作空间との役割分担は明確にする——ここは**選ぶ場所**（就緒度・資源・定时・上次执行を横並びで
 * 比較する）、工作空间は**今走っている一つを観る場所**。以前は task が工作空间の弹窗内の
 * `<select>` にしか存在せず、就緒度も定时も上次执行も別々の場所に散っていた。
 */
export interface TasksPageProps {
  projectId: string
  csrfToken: string
  moduleId: string
  currentProject?: ProjectRecord | null
  projectReadOnly?: boolean
  schedulingEnabled?: boolean
  actorId?: string
  onSessionEnded?: SessionEnded
}

/** App の現会話を持たない単独表示では session を変更しない。 */
function retainSession(): void {}

/** 同 actor でも会話/Project が替われば、旧 Task の照会と選択を持ち越さない。 */
export function TasksPage(props: TasksPageProps) {
  return <TaskCenter key={`${props.actorId ?? ''}:${props.csrfToken}:${props.projectId}`} {...props} />
}

/** Task 一覧と独立した read-only preview を同じ精確 Project の中へ置く。 */
function TaskCenter({ projectId, csrfToken, moduleId, currentProject = null, projectReadOnly = false, schedulingEnabled = true, actorId = '', onSessionEnded = retainSession }: TasksPageProps) {
  const messages = useMessages()
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [modules, setModules] = useState<ProjectModuleRecord[]>([])
  const [revision, setRevision] = useState(0)
  const [scheduleFor, setScheduleFor] = useState<PublishedTaskRecord | null>(null)
  const [q, setQ] = useState('')
  const [status, setStatus] = useState<TaskScheduleStatusFilter>('')
  const [filter, setFilter] = useState({ q: '', status: '' as TaskScheduleStatusFilter })
  const [scheduleId, setScheduleId] = useState<string | null>(null)
  const [scheduleBusy, setScheduleBusy] = useState(false)
  const scheduleLock = useRef(false)
  const setScheduleLocked = useCallback((busy: boolean) => { scheduleLock.current = busy; setScheduleBusy(busy) }, [])
  const controller = useRef<AbortController | null>(null)
  const previewTrigger = useRef<HTMLButtonElement | null>(null)
  const previewHeading = useRef<HTMLHeadingElement | null>(null)
  const flow = useTaskFlowPreview({ projectId, actorId, sessionKey: csrfToken, onSessionEnded })

  useEffect(() => { if (flow.target) previewHeading.current?.focus() }, [flow.selectionRevision])

  useEffect(() => {
    controller.current?.abort()
    if (!projectId) {
      setState({ status: 'ready', data: { tasks: [], schedules: [] } })
      return
    }
    const active = new AbortController()
    controller.current = active
    setState({ status: 'loading' })
    void Promise.all([
      loadProjectTasks(projectId, active.signal),
      loadProjectSchedules(projectId, active.signal),
      loadProjectModules(projectId, active.signal).catch(() => [] as ProjectModuleRecord[]),
    ])
      .then(([tasks, schedules, projectModules]) => {
        if (active.signal.aborted) return
        setModules(projectModules)
        setState({ status: 'ready', data: { tasks: tasks.tasks, schedules } })
      })
      .catch((error: unknown) => {
        if (active.signal.aborted) return
        setState({
          status: 'error',
          message: error instanceof Error ? error.message : messages.tasks.loadFailed,
        })
      })
    return () => active.abort()
  }, [projectId, revision, schedulingEnabled])

  useEffect(() => () => controller.current?.abort(), [])

  const activeModule = useMemo(
    () => modules.find((module) => module.module_id === moduleId) ?? null,
    [modules, moduleId],
  )
  const allRows = useMemo(() => {
    if (state.status !== 'ready') return []
    return buildRows(filterTasksByModule(state.data.tasks, activeModule), state.data)
  }, [state, activeModule])
  const rows = allRows.filter((row) => matchesTaskScheduleFilter(row.task, row.schedules, filter.q, filter.status))
  const unavailable = state.status === 'ready' ? unavailableScheduledTasks(state.data.tasks, state.data.schedules)
    .filter((schedules) => matchesTaskScheduleFilter(null, schedules, filter.q, filter.status)) : []
  const queryInvalid = [...q.trim()].length > 200 || q.includes('\u0000')

  /** 検索と状態を同時に適用し、予定の編集 owner は一覧から独立して保つ。 */
  function search(event: FormEvent): void {
    event.preventDefault()
    if (!queryInvalid) setFilter({ q, status })
  }
  /** 未決編集がある時は別の予定に対象を切り替えない。 */
  function manageSchedule(id: string): void { if (!scheduleLock.current) setScheduleId(id) }


  useEffect(() => {
    if (flow.target && state.status === 'ready' && !allRows.some((row) => taskCatalogId(row.task) === `${flow.target!.skill_version_id}::${flow.target!.task_key}`)) flow.close()
  }, [allRows, state.status, flow.target, flow.close])

  /** 閉じた瞬間に旧 query を失効させ、元の選択 button へ keyboard focus を戻す。 */
  function closePreview(): void {
    flow.close()
    if (previewTrigger.current?.isConnected) previewTrigger.current.focus()
  }

  if (!projectId) {
    return (
      <>
        <PageHeader title={messages.routes.tasks.label} />
        <section className="panel">
          <EmptyState text={messages.tasks.selectProjectFirst} />
        </section>
      </>
    )
  }

  return (
    <>
      <PageHeader
        title={activeModule ? messages.tasks.titleWithModule(activeModule.name) : messages.routes.tasks.label}
        description={activeModule?.description || undefined}
        aside={<span className="scopeBadge">{messages.tasks.countBadge(rows.length + unavailable.length)}</span>}
      />
      <section className="panel taskCatalog" aria-label={messages.routes.tasks.label}>
        <form className="taskFilters" data-task-filters onSubmit={search}>
          <label>{messages.scheduleManager.searchLabel}<input data-task-search value={q} maxLength={200}
            onChange={(event) => setQ(event.target.value)} /></label>
          <label>{messages.scheduleManager.statusLabel}<select data-task-status value={status}
            onChange={(event) => setStatus(event.target.value as TaskScheduleStatusFilter)}>
            <option value="">{messages.scheduleManager.allStates}</option>
            <option value="UNCONFIGURED">{messages.tasks.noSchedule}</option>
            {(['ACTIVE', 'PAUSED', 'COMPLETED', 'ERROR', 'ARCHIVED'] as const).map((value) =>
              <option key={value} value={value}>{messages.enums.scheduleStatus[value]}</option>)}
          </select></label>
          <button className="primaryButton compactButton" type="submit" disabled={queryInvalid}>{messages.scheduleManager.search}</button>
          <button className="secondaryButton compactButton" type="button" data-task-refresh disabled={state.status === 'loading'}
            onClick={() => setRevision((current) => current + 1)}>{messages.scheduleManager.refresh}</button>
        </form>
        {queryInvalid && <p className="error" role="alert">{messages.scheduleManager.invalidSearch}</p>}
        {state.status === 'loading' && <LoadingSkeleton label={messages.tasks.loading} rows={4} />}
        {state.status === 'error' && <p className="error" role="alert">{state.message}</p>}
        {state.status === 'ready' && rows.length + unavailable.length === 0 && (
          <EmptyState
            text={filter.q || filter.status ? messages.tasks.noMatches : messages.tasks.empty}
            action={!(filter.q || filter.status) && <a className="secondaryButton compactButton" href={routeHref('skills')}>{messages.projects.goSkills}</a>}
          />
        )}
        {state.status === 'ready' && rows.length + unavailable.length > 0 && (
          <ul className="taskCards">
            {rows.map((row) => (
              <TaskCard
                key={taskCatalogId(row.task)}
                onSchedule={() => setScheduleFor(row.task)}
                onManageSchedule={manageSchedule}
                scheduleBusy={scheduleBusy}
                onPreview={(trigger) => { if (flow.select(row.task)) previewTrigger.current = trigger }}
                previewAllowed={flow.allowed && isValidTaskFlowTarget(row.task)}
                previewSelected={flow.target?.skill_version_id === row.task.skill_version_id && flow.target?.task_key === row.task.task_key}
                projectId={projectId}
                projectReadOnly={projectReadOnly}
                schedulingEnabled={schedulingEnabled}
                row={row}
              />
            ))}
            {unavailable.map((schedules) => <li className="taskCard" data-unavailable-task key={`${schedules[0]!.skill_version_id}::${schedules[0]!.task_key}`}>
              <div className="taskCardTitle"><strong>{schedules[0]!.task_key}</strong><small>{messages.tasks.unavailableTask}</small></div>
              <TaskSchedules schedules={schedules} onManage={manageSchedule} disabled={scheduleBusy} />
            </li>)}
          </ul>
        )}
      </section>
      {scheduleId && <section data-task-schedule-panel>
        <div className="formRow"><button className="secondaryButton compactButton" type="button" data-task-schedule-close
          disabled={scheduleBusy} onClick={() => { if (!scheduleLock.current) setScheduleId(null) }}>{messages.elements.close}</button></div>
        <TaskScheduleDetails key={`${projectId}:${scheduleId}`} projectId={projectId} scheduleId={scheduleId}
          csrfToken={csrfToken} currentProject={currentProject} schedulingEnabled={schedulingEnabled}
          onSessionEnded={onSessionEnded} onBusyChange={setScheduleLocked} onChanged={() => setRevision((current) => current + 1)} />
      </section>}
      {flow.target && <section className="panel taskFlowPanel" id="task-flow-preview" data-task-flow-panel
        aria-labelledby="task-flow-heading" onKeyDown={(event) => { if (event.key === 'Escape') { event.preventDefault(); closePreview() } }}>
        <div className="panelHeader"><div><h2 ref={previewHeading} id="task-flow-heading" tabIndex={-1}>{messages.taskFlow.title}</h2>
          <p className="hint"><code>{flow.target.skill_key}</code> · <code>{flow.target.task_key}</code> · {flow.target.version}</p></div>
          <div className="formRow"><button type="button" className="secondaryButton compactButton" data-flow-refresh disabled={flow.pending} onClick={flow.refresh}>{messages.taskFlow.refresh}</button>
            <button type="button" className="secondaryButton compactButton" data-flow-close onClick={closePreview}>{messages.taskFlow.close}</button></div>
        </div>
        {flow.pending && <LoadingSkeleton label={messages.taskFlow.loading} rows={3} />}
        {flow.failure && <p role="alert" className="error" data-flow-error>{messages.taskFlow.failures[flow.failure.key]}</p>}
        {flow.data && <TaskFlowPreview preview={flow.data} />}
      </section>}
      {/* 定时执行は task に属する設定なので、設定入口も一覧の行に置く。工作空间の左 rail に
          置いていたときは「今の下書き」に紐づいていて、どの task の予定なのかが読めなかった。 */}
      {scheduleFor !== null && !projectReadOnly && schedulingEnabled && <ScheduleDialog
        key={`${projectId}:${taskCatalogId(scheduleFor)}`}
        csrfToken={csrfToken}
        onClose={() => setScheduleFor(null)}
        onSaved={() => { setScheduleFor(null); setRevision((current) => current + 1) }}
        open
        projectId={projectId}
        task={scheduleFor}
      />}
    </>
  )
}

/** 一覧一行分の投影。task 本体と、それに紐づく定时・上次执行。 */
interface TaskRow {
  task: PublishedTaskRecord
  schedules: ScheduleRecord[]
  requirementCount: number | null
}

/** task 一覧に定时を突き合わせる。上次执行は server が descriptor に同梱して返す。
 *
 * 以前はここで Run 履歴の先頭 N 件を突き合わせていたが、N 件より古い task が「未実行」と
 * 表示された——欠落ではなく誤った値だった。件数上限に依存しない形は server 側の投影しかない。
 */
export function buildRows(tasks: PublishedTaskRecord[], data: TaskCenterState): TaskRow[] {
  return tasks.map((task) => ({
    task,
    schedules: schedulesForTask(task, data.schedules),
    requirementCount: task.readiness === null ? null : sourceRequirements(task).length,
  }))
}

/** 一つの task を、就緒度・資源・定时・操作の四点で示す card。 */
function TaskCard({ row, projectId, projectReadOnly, schedulingEnabled, onSchedule, onManageSchedule, scheduleBusy, onPreview, previewAllowed, previewSelected }: {
  row: TaskRow
  projectId: string
  projectReadOnly: boolean
  schedulingEnabled: boolean
  onSchedule: () => void
  onManageSchedule: (id: string) => void
  scheduleBusy: boolean
  onPreview: (trigger: HTMLButtonElement) => void
  previewAllowed: boolean
  previewSelected: boolean
}) {
  const messages = useMessages()
  const readiness = row.task.readiness
  const level = readiness?.level ?? null
  const activeSchedules = row.schedules.filter((schedule) => schedule.status === 'ACTIVE')
  const nextSchedule = activeSchedules.filter((schedule) => schedule.next_run_at !== null)
    .sort((left, right) => Date.parse(left.next_run_at!) - Date.parse(right.next_run_at!))[0]
  return (
    <li className="taskCard" data-task-card={taskCatalogId(row.task)}>
      <div className="taskCardHead">
        <div className="taskCardIdentity">
          <span className="taskCardIcon" aria-hidden="true">{ROUTE_ICONS.tasks}</span>
          <div className="taskCardTitle">
            <strong>{row.task.title}</strong>
            <small>{row.task.skill_name} v{row.task.version}</small>
          </div>
        </div>
        {level && (
          <span className={`statusBadge readiness-${level.toLowerCase()}`}>
            {messages.workspace.readinessLevels[level] ?? level}
          </span>
        )}
      </div>
      <dl className="taskCardFacts">
        <div>
          <dt>{messages.tasks.resourcesLabel}</dt>
          <dd>{row.requirementCount === null ? messages.taskFlow.unassessed : row.requirementCount === 0
            ? messages.workspace.noResourceNeeded
            : messages.tasks.requirementCount(row.requirementCount)}</dd>
        </div>
        <div>
          <dt>{messages.tasks.scheduleLabel}</dt>
          <dd>{row.schedules.length === 0
            ? messages.tasks.noSchedule
            : messages.tasks.scheduleCount(row.schedules.length)}</dd>
        </div>
        {row.schedules.length > 0 && <div>
          <dt>{messages.tasks.nextRunLabel}</dt>
          <dd>{nextSchedule ? formatScheduleTimestamp(nextSchedule.next_run_at!, nextSchedule.timezone) : messages.schedules.noNextRun}</dd>
        </div>}
      </dl>
      {/* 未実行を空欄にすると読み込み中と区別が付かない。「実行履歴なし」と明示する。 */}
      <p className="hint taskCardLastRun">
        {messages.tasks.lastRunLabel}:{' '}
        {row.task.last_run
          ? <>{formatLocalTimestamp(row.task.last_run.started_at ?? row.task.last_run.created_at)} <StatusBadge status={row.task.last_run.status} /> <RunDuration run={row.task.last_run} /></>
          : messages.tasks.neverRun}
      </p>
      {/* 主操作(すぐ実行)を先頭に置き、確認・設定の操作を後ろへ並べる。 */}
      <div className="taskCardActions">
        {/* 「立即执行」は工作空间へ渡す。実行中の観測・応答・承認はすべて向こうの責務で、
            ここに二つ目の実行 lifecycle を作らない。 */}
        <a className="primaryButton compactButton" href={routeHref('workspace', projectId, { taskId: taskCatalogId(row.task) })}>
          {messages.tasks.runNow}
        </a>
        <button className="secondaryButton compactButton" type="button" data-flow-open={taskCatalogId(row.task)}
          aria-expanded={previewSelected} aria-controls="task-flow-preview" disabled={!previewAllowed}
          onClick={(event) => onPreview(event.currentTarget)}>{messages.taskFlow.open}</button>
        {schedulingEnabled && <button
          className="secondaryButton compactButton"
          disabled={projectReadOnly || level === null || level === 'GUIDANCE_ONLY'}
          type="button"
          onClick={onSchedule}
        >
          {messages.tasks.addSchedule}
        </button>}
      </div>
      {!isValidTaskFlowTarget(row.task) && <p className="hint" data-flow-invalid-target>{messages.taskFlow.failures.invalid}</p>}
      <TaskSchedules schedules={row.schedules} onManage={onManageSchedule} disabled={scheduleBusy} />
    </li>
  )
}

/** カードには各予定の状態と次回時刻を出し、詳細を開いて元版の操作を行う。 */
function TaskSchedules({ schedules, onManage, disabled }: {
  schedules: ScheduleRecord[]; onManage: (id: string) => void; disabled: boolean
}) {
  const messages = useMessages()
  if (!schedules.length) return null
  return <details className="taskSchedules"><summary>{messages.tasks.manageSchedule} ({schedules.length})</summary><ul className="taskScheduleList">
    {schedules.map((schedule) => <li key={schedule.schedule_id}>
      <span className={`statusBadge scheduleStatus-${schedule.status.toLowerCase()}`}>{messages.enums.scheduleStatus[schedule.status]}</span>
      <strong>{schedule.name}</strong><span className="mono">{summarizeTiming(schedule)}</span>
      <button className="secondaryButton compactButton" type="button" data-task-schedule-manage={schedule.schedule_id}
        disabled={disabled} onClick={() => onManage(schedule.schedule_id)}>{messages.tasks.manageSchedule}</button>
    </li>)}
  </ul></details>
}
