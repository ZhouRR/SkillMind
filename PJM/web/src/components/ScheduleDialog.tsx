import { useCallback, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from 'react'

import {
  ApiProblemError, createSchedule, previewSchedule,
  type PublishedTaskRecord, type ScheduleDefinitionInput, type ScheduleRecord, type ScheduleStatus,
} from '../api'
import { useResourceMutation, useResourceQuery, type ResourceRequestPolicy, type SessionEnded } from '../hooks/useResourceRequest'
import { useScheduleRevision } from '../hooks/useScheduleRevision'
import { useMessages } from '../i18n'
import {
  browserScheduleTimezone, formatScheduleTimestamp, localScheduleInstants, preservedScheduleDefinition,
  scheduleDefinition, scheduleTimeDraft,
  type LocalScheduleInstant, type ScheduleTimeDraft,
} from '../lib/scheduleTime'
import { buildTaskDraft, defaultSourceProviders, taskCatalogId } from '../lib/taskDraft'
import { classifyScheduleReadFailure } from '../lib/scheduleManager'
import { sameUuid } from '../lib/validation'
import { ModalDialog } from './PageElements'
import { TaskLaunchFields } from './TaskLaunchFields'
import { ScheduleRevisionReview } from './ScheduleRevisionReview'

/** 同じ精確 task の作成だけを扱い、編集や別の実行 API を増やさない。 */
interface ScheduleDialogProps {
  open: boolean
  projectId: string
  csrfToken: string
  task: PublishedTaskRecord
  onClose: () => void
  onSaved: (schedule: ScheduleRecord) => void
}

/** 元対象の編集 owner は閉じても保持でき、別 row_version の再読取では作り直さない。 */
interface ScheduleEditDialogProps extends ScheduleDialogProps {
  schedule: ScheduleRecord
  disabled?: boolean
  onPendingChange?: (pending: boolean) => void
  onSessionEnded?: SessionEnded
  isWriteAllowed?: () => boolean
}

/** 時間 preview と保存では同じ HTTP 失敗でも意味が異なる。 */
interface ScheduleFailure { key: 'previewFailed' | 'saveRejected' | 'saveDenied' | 'saveUnknown' }
const SCHEDULE_POLICY: ResourceRequestPolicy<ScheduleFailure> = {
  classify: (error, mutation) => ({
    key: !mutation ? 'previewFailed'
      : error instanceof ApiProblemError && [400, 422].includes(error.status) ? 'saveRejected'
        : error instanceof ApiProblemError && [401, 403, 404].includes(error.status) ? 'saveDenied' : 'saveUnknown',
  }),
  readTimeout: { key: 'previewFailed' },
  writeTimeout: { key: 'saveUnknown' },
  blocks: ({ key }) => key === 'saveUnknown' || key === 'saveDenied',
}
/** ここでは認証状態を書き換えず、現在の form に拒否を明示する。 */
function retainSession(): void {}

/** 同じ文字列へ戻っても別 request identity。以前の候補や確認を再利用しない。 */
interface PreviewRequest { revision: number; definition: ScheduleDefinitionInput }

/** actor は親の key、Project/CSRF/task/open はこの所有者で分離する。token は DOM/storage に出さない。 */
export function ScheduleDialog(props: ScheduleDialogProps) {
  return <ScheduleDialogOwner {...props} />
}

/** 呼出側は正確な catalog task と元 record を渡し、hidden 中も pending owner を保持する。 */
export function ScheduleEditDialog(props: ScheduleEditDialogProps) {
  return <ScheduleDialogOwner {...props} />
}

/** 作成/編集は一つの form。CSRF/対象 ABA と、単なる表示 open の違いを明示する。 */
function ScheduleDialogOwner(props: ScheduleDialogProps & Partial<Pick<ScheduleEditDialogProps, 'schedule' | 'disabled' | 'onPendingChange' | 'onSessionEnded' | 'isWriteAllowed'>>) {
  const taskId = taskCatalogId(props.task)
  const lifecycle = props.schedule ? true : props.open
  const context = useMemo(() => ({}), [props.projectId, props.csrfToken, taskId, props.schedule?.schedule_id, lifecycle])
  const current = useRef(context)
  current.current = context
  const isCurrent = useCallback(() => current.current === context && lifecycle, [context, lifecycle])
  return <ScheduleCreationForm key={JSON.stringify([props.projectId, props.csrfToken, taskId, props.schedule?.schedule_id, lifecycle])}
    {...props} isCurrent={isCurrent} />
}

/** 共有 request 境界で防重/期限を守り、現在の時間 preview を人が確認した後だけ保存する。 */
function ScheduleCreationForm({ open, projectId, csrfToken, task, onClose, onSaved, isCurrent,
  schedule, disabled = false, onPendingChange, onSessionEnded, isWriteAllowed }:
  ScheduleDialogProps & Partial<Pick<ScheduleEditDialogProps, 'schedule' | 'disabled' | 'onPendingChange' | 'onSessionEnded' | 'isWriteAllowed'>>
  & { isCurrent: () => boolean }) {
  const messages = useMessages()
  const [inputTimezone] = useState(browserScheduleTimezone)
  const [original] = useState(() => schedule ? structuredClone(schedule) : null)
  const [initialTiming] = useState<ScheduleTimeDraft>(() => original ? scheduleTimeDraft(original, inputTimezone) : ({
    kind: 'CRON', timezone: inputTimezone, cronExpression: '0 3 * * *',
    runAt: '', runAtChoice: '', endAt: '', endAtChoice: '', maxRuns: '',
  }))
  const [timing, setTiming] = useState(initialTiming)
  const timingRef = useRef(timing)
  const [name, setName] = useState(original?.name ?? '')
  const nameRef = useRef(name)
  const [inputText, setInputText] = useState(() => original ? JSON.stringify(original.input, null, 2) : '{}')
  const inputRef = useRef(inputText)
  const [sources, setSources] = useState(() => original ? { ...original.sources } : defaultSourceProviders(task))
  const sourcesRef = useRef(sources)
  const runCandidates = useMemo(() => localScheduleInstants(timing.runAt, inputTimezone), [timing.runAt, inputTimezone])
  const endCandidates = useMemo(() => localScheduleInstants(timing.endAt, inputTimezone), [timing.endAt, inputTimezone])
  const definition = useMemo(() => {
    const resolved = scheduleDefinition(timing, runCandidates, endCandidates)
    return original ? preservedScheduleDefinition(timing, initialTiming, original, resolved) : resolved
  }, [timing, runCandidates, endCandidates, original, initialTiming])
  const [request, setRequest] = useState<PreviewRequest | null>(null)
  const currentRequest = useRef<PreviewRequest | null>(null)
  const serial = useRef(0)
  const previewStarted = useRef(false)
  const [confirmed, setConfirmed] = useState<PreviewRequest | null>(null)
  const confirmedRef = useRef<PreviewRequest | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const mutation = useResourceMutation(retainSession, SCHEDULE_POLICY)
  const unavailable = disabled || !!original && (!sameUuid(original.skill_version_id, task.skill_version_id)
    || original.task_key !== task.task_key || !sameUuid(original.project_id, projectId)
    || task.readiness?.level === 'GUIDANCE_ONLY')
  const revision = useScheduleRevision({ schedule: original, projectId, csrfToken, isCurrent, disabled: unavailable, onSessionEnded, isWriteAllowed })
  const editingTerminal = !!original && !!revision.base && ['COMPLETED', 'ARCHIVED'].includes(revision.base.status)
  const pendingCallback = useRef(onPendingChange)
  pendingCallback.current = onPendingChange
  useLayoutEffect(() => { if (original && isCurrent()) pendingCallback.current?.(revision.pending) }, [original, revision.pending, isCurrent])

  const loader = useCallback(async (signal: AbortSignal): Promise<string[]> => {
    if (!request || currentRequest.current !== request || !isCurrent()) throw new DOMException('Obsolete preview', 'AbortError')
    try { return await previewSchedule(projectId, request.definition, csrfToken, signal) }
    catch (error: unknown) {
      if (original && currentRequest.current === request && !signal.aborted && isCurrent()
        && classifyScheduleReadFailure(error).key !== 'loadFailed') {
        revision.denyAccess(classifyScheduleReadFailure(error).key === 'sessionExpired')
      }
      throw error
    }
    finally { if (currentRequest.current === request) previewStarted.current = false }
  }, [request, projectId, csrfToken, isCurrent, original, revision.denyAccess])
  const preview = useResourceQuery(String(request?.revision ?? 0), loader, retainSession, SCHEDULE_POLICY, !!request)
  const currentPreview = !!request && currentRequest.current === request && !preview.pending
    && !preview.failure && !!preview.data?.length
  if (request && !preview.pending) previewStarted.current = false
  const locked = unavailable || editingTerminal || (original ? revision.locked : mutation.busy
    || mutation.failure?.key === 'saveUnknown' || mutation.failure?.key === 'saveDenied')
  const taskDraft = buildTaskDraft(task, inputText, sources)

  /** 定義を変えた同じ tick で古い確認を閉じる。A→B→A でも復活させない。 */
  function invalidatePreview(): void {
    currentRequest.current = null
    confirmedRef.current = null
    previewStarted.current = false
    preview.refresh()
    setRequest(null)
    setConfirmed(null)
    setFormError(null)
  }

  /** 原版の採用後も同じ草稿を維持し、旧版の preview/確認だけは必ず捨てる。 */
  function changeTiming(change: Partial<ScheduleTimeDraft>): void {
    if (locked || !open) return
    invalidatePreview()
    timingRef.current = { ...timingRef.current, ...change }
    setTiming(timingRef.current)
  }

  /** 明示 click だけで preview を開始する。名称/タスク入力編集では時間候補を再送しない。 */
  function handlePreview(): void {
    if (!open || !isCurrent() || locked || timingRef.current !== timing || previewStarted.current || (request && preview.pending)) return
    if (!definition) { setFormError(messages.schedules.invalidDefinition); return }
    const next = { revision: ++serial.current, definition: { ...definition } }
    currentRequest.current = next
    confirmedRef.current = null
    previewStarted.current = true
    setConfirmed(null)
    setFormError(null)
    setRequest(next)
  }

  /** 同期 ref でも現在の候補/確認を照合し、button の disabled だけに依存しない。 */
  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    if (!open || !isCurrent() || locked) return
    const original = currentRequest.current
    if (!original || original !== request || !currentPreview || confirmedRef.current !== original) {
      setFormError(messages.schedules.previewRequired)
      return
    }
    const draft = buildTaskDraft(task, inputRef.current, sourcesRef.current)
    if (!draft) { setFormError(messages.workspace.documentSelection.incompleteDraft); return }
    const payload = {
      name: schedule ? nameRef.current : nameRef.current.trim() || draft.taskTitle, definition: original.definition,
      skill_version_id: draft.skillVersionId, task_key: draft.taskKey, input: draft.input, sources: draft.sources,
    }
    const accepted = schedule ? revision.submit({ kind: 'edit', input: {
      name: payload.name, definition: payload.definition, input: payload.input, sources: payload.sources,
    } }, onSaved) : mutation.submit((signal) => {
      if (!isCurrent()) throw new DOMException('Obsolete schedule', 'AbortError')
      return createSchedule(projectId, payload, csrfToken, signal)
    }, (saved) => { if (isCurrent()) onSaved(saved) })
    if (accepted) {
      if (schedule) pendingCallback.current?.(true)
      confirmedRef.current = null
      setConfirmed(null)
      setFormError(null)
    }
  }

  return <ModalDialog open={open} title={original ? messages.scheduleEditor.title : messages.schedules.create} wide onClose={onClose}>
    <form className="runForm" data-schedule-form data-schedule-editor={original?.schedule_id} onSubmit={handleSubmit}>
      <p className="hint">{messages.schedules.frozenTask(task.title)}</p>
      {original && <p className="hint">{messages.scheduleEditor.identity(original.schedule_id, revision.base!.row_version)}</p>}
      {(unavailable || editingTerminal) && <p role="alert">{messages.scheduleEditor.unavailable}</p>}
      <fieldset disabled={locked} className="scheduleConfiguration">
        <TaskLaunchFields task={task} inputText={inputText} sourceProviders={sources}
          onInputTextChange={(value) => { inputRef.current = value; setInputText(value) }}
          onSourceChange={(key, value) => {
            sourcesRef.current = { ...sourcesRef.current, [key]: value }; setSources(sourcesRef.current)
          }} />
        <label>{messages.schedules.nameLabel}<input name="name" maxLength={200} type="text" value={name}
          placeholder={task.title} onChange={(event) => { nameRef.current = event.target.value; setName(event.target.value) }} /></label>
        <label>{messages.schedules.kindLabel}<select name="kind" value={timing.kind}
          onChange={(event) => changeTiming({ kind: event.target.value as ScheduleTimeDraft['kind'] })}>
          <option value="CRON">{messages.enums.scheduleKind.CRON}</option>
          <option value="ONCE">{messages.enums.scheduleKind.ONCE}</option>
        </select></label>
        <label>{messages.schedules.timezoneLabel}<input name="timezone" type="text" maxLength={64} value={timing.timezone}
          onChange={(event) => changeTiming({ timezone: event.target.value })} /></label>
        <p className="hint">{messages.schedules.ruleTimezoneHint}</p>
        {timing.kind === 'CRON' && <label>{messages.schedules.cronLabel}<input name="cron_expression" type="text"
          maxLength={128} value={timing.cronExpression} onChange={(event) => changeTiming({ cronExpression: event.target.value })} /></label>}
        <p className="hint" data-schedule-input-timezone>{messages.schedules.inputTimezone(inputTimezone)}</p>
        {timing.kind === 'ONCE' && <ScheduleDateField name="run_at" label={messages.schedules.runAtLabel}
          original={original && timing.runAt === initialTiming.runAt && timing.runAtChoice === initialTiming.runAtChoice ? original.run_at : null}
          value={timing.runAt} choice={timing.runAtChoice} candidates={runCandidates} timezone={inputTimezone}
          onChange={(value) => changeTiming({ runAt: value, runAtChoice: '' })}
          onChoice={(value) => changeTiming({ runAtChoice: value })} />}
        <ScheduleDateField name="end_at" label={messages.schedules.endAtLabel}
          original={original && timing.endAt === initialTiming.endAt && timing.endAtChoice === initialTiming.endAtChoice ? original.end_at : null}
          value={timing.endAt} choice={timing.endAtChoice} candidates={endCandidates} timezone={inputTimezone}
          onChange={(value) => changeTiming({ endAt: value, endAtChoice: '' })}
          onChoice={(value) => changeTiming({ endAtChoice: value })} />
        <label>{messages.schedules.maxRunsLabel}<input name="max_runs" type="number" min={1} max={100000} step={1}
          value={timing.maxRuns} onChange={(event) => changeTiming({ maxRuns: event.target.value })} /></label>
        <div className="formRow"><button className="secondaryButton" data-schedule-preview type="button"
          disabled={!definition || (!!request && preview.pending)} onClick={handlePreview}>
          {request && preview.pending ? messages.schedules.previewLoading : messages.schedules.preview}
        </button></div>
        {currentPreview && <div data-schedule-preview-result>
          <p className="hint">{messages.schedules.previewHint(request!.definition.timezone)}</p>
          <ol className="schedulePreview">{preview.data!.map((instant) =>
            <li key={instant} className="mono">{formatScheduleTimestamp(instant, request!.definition.timezone)}</li>)}</ol>
          <label className="submissionAcknowledgement"><input type="checkbox" data-schedule-confirm checked={confirmed === request}
            onChange={(event) => {
              const next = event.target.checked && currentPreview && currentRequest.current === request ? request : null
              confirmedRef.current = next; setConfirmed(next)
              if (next) setFormError(null)
            }} /><span>{messages.schedules.previewConfirm}</span></label>
        </div>}
        <p className="hint">{messages.schedules.overlapHint}</p>
      </fieldset>
      {request && preview.failure && <p className="error" role="alert">{messages.schedules[preview.failure.key]}</p>}
      {formError && <p className="error" role="alert">{formError}</p>}
      {original ? <ScheduleRevisionReview revision={revision} visible={open} onAdopt={() => {
        if (open && revision.adopt()) invalidatePreview()
      }} /> : mutation.failure && <p className="error" role="alert" data-schedule-save-failure>{messages.schedules[mutation.failure.key]}</p>}
      <p className="hint">{original ? messages.scheduleEditor.closingHint : messages.schedules.closingHint}</p>
      <button className="primaryButton" type="submit"
        disabled={locked || !taskDraft || !currentPreview || confirmed !== request}>
        {mutation.busy || revision.phase === 'sending' ? messages.schedules.saving : messages.schedules.save}
      </button>
    </form>
  </ModalDialog>
}

/** 同じ browser 時区の入力でも DST fold は offset の選択を別操作にする。 */
function ScheduleDateField({ name, label, value, choice, candidates, timezone, original, onChange, onChoice }: {
  name: string; label: string; value: string; choice: string; candidates: LocalScheduleInstant[]
  timezone: string; onChange: (value: string) => void; onChoice: (value: string) => void
  original?: string | null
}) {
  const messages = useMessages()
  return <div data-schedule-date={name}>
    <label>{label}<input type="datetime-local" name={name} value={value} step={60}
      onChange={(event) => onChange(event.target.value)} /></label>
    {value && candidates.length === 0 && <p className="error" role="alert">{messages.schedules.invalidLocalTime}</p>}
    {candidates.length === 1 && <p className="hint">{formatScheduleTimestamp(candidates[0]!.instant, timezone)}</p>}
    {original && <p className="hint" data-schedule-original-instant>{messages.scheduleEditor.originalInstant(original)}</p>}
    {candidates.length > 1 && <label>{messages.schedules.ambiguousLocalTime}
      <select data-schedule-offset value={choice} onChange={(event) => onChoice(event.target.value)}>
        <option value="">{messages.schedules.chooseOffset}</option>
        {candidates.map((candidate) => <option key={candidate.instant} value={candidate.instant}>
          {formatScheduleTimestamp(candidate.instant, timezone)}
        </option>)}
      </select>
    </label>}
  </div>
}

/** 状態変更の元 owner。actor は親 key、CSRF/Project/Schedule はここで分離する。 */
interface ScheduleStatusProps {
  schedule: ScheduleRecord
  projectId: string
  csrfToken: string
  onChanged: (schedule: ScheduleRecord) => void
  onError: (message: string) => void
  disabled?: boolean
  onPendingChange?: (pending: boolean) => void
  onSessionEnded?: SessionEnded
  isWriteAllowed?: () => boolean
}

/** row_version 更新では未確認 write を消さず、元 ID の GET と人工採用を要求する。 */
export function ScheduleStatusActions(props: ScheduleStatusProps) {
  const context = useMemo(() => ({}), [props.projectId, props.csrfToken, props.schedule.schedule_id])
  const current = useRef(context)
  current.current = context
  const isCurrent = useCallback(() => current.current === context, [context])
  return <ScheduleStatusOwner key={JSON.stringify([props.projectId, props.csrfToken, props.schedule.schedule_id])}
    {...props} isCurrent={isCurrent} />
}

/** 同じ同期 mutation/期限/比較境界を編集と共用し、未知結果から自動で次の状態へ進まない。 */
function ScheduleStatusOwner({ schedule, projectId, csrfToken, onChanged, onError,
  disabled = false, onPendingChange, onSessionEnded, isWriteAllowed, isCurrent }: ScheduleStatusProps & { isCurrent: () => boolean }) {
  const messages = useMessages()
  const revision = useScheduleRevision({ schedule, projectId, csrfToken, isCurrent, disabled, syncLatestWhenIdle: true, onSessionEnded, isWriteAllowed })
  const callback = useRef(onPendingChange)
  callback.current = onPendingChange
  useLayoutEffect(() => { if (isCurrent()) callback.current?.(revision.pending) }, [revision.pending, isCurrent])
  const current = revision.base!
  /** 元版を伴う一回の明示操作。採用後は改めてボタンを選ぶまで送信しない。 */
  function apply(status: ScheduleStatus): void {
    if (revision.submit({ kind: 'status', status }, onChanged,
      (failure) => onError(messages.scheduleEditor.failures[failure.key]))) callback.current?.(true)
  }
  return <div data-schedule-status-owner={schedule.schedule_id}>
    <span className="scheduleActions" aria-busy={revision.phase === 'sending'}>
      {current.status === 'ACTIVE' && <button className="secondaryButton compactButton" data-schedule-status="PAUSED"
        disabled={revision.locked} type="button" onClick={() => apply('PAUSED')}>{messages.schedules.pause}</button>}
      {(current.status === 'PAUSED' || current.status === 'ERROR') && <button className="secondaryButton compactButton" data-schedule-status="ACTIVE"
        disabled={revision.locked} type="button" onClick={() => apply('ACTIVE')}>{messages.schedules.resume}</button>}
      {current.status !== 'ARCHIVED' && <button className="secondaryButton compactButton" data-schedule-status="ARCHIVED"
        disabled={revision.locked} type="button" onClick={() => apply('ARCHIVED')}>{messages.schedules.archive}</button>}
    </span>
    <ScheduleRevisionReview revision={revision} onAdopt={() => { revision.adopt() }} />
  </div>
}

/** 発火形態を一行で要約し、ONCE に browser 時刻と別 zone のラベルを混ぜない。 */
export function summarizeTiming(schedule: ScheduleRecord): string {
  if (schedule.kind === 'CRON') return (schedule.cron_expression ?? '') + ' · ' + schedule.timezone
  return formatScheduleTimestamp(schedule.run_at ?? '', schedule.timezone)
}
