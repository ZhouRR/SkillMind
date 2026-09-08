import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'

import {
  changeScheduleStatus,
  createSchedule,
  previewSchedule,
  type PublishedTaskRecord,
  type ScheduleDefinitionInput,
  type ScheduleKind,
  type ScheduleRecord,
  type ScheduleStatus,
} from '../api'
import { useMessages } from '../i18n'
import { formatLocalTimestamp } from '../lib/presentation'
import { buildTaskDraft, defaultSourceProviders } from '../lib/taskDraft'
import { ModalDialog } from './PageElements'
import { TaskLaunchFields } from './TaskLaunchFields'

/** 時間規則の草稿。タスク入力と資源選択は共通 TaskDraft に分離する。 */
interface ScheduleDraft {
  name: string
  kind: ScheduleKind
  timezone: string
  cronExpression: string
  runAt: string
  endAt: string
  maxRuns: string
}

/** browser の timezone を初期値にする。利用者の体感時刻と保存値を最初から一致させる。 */
function defaultTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

/** 保存対象のない新しい時間草稿を作る。 */
function emptyDraft(): ScheduleDraft {
  return {
    name: '',
    kind: 'CRON',
    timezone: defaultTimezone(),
    cronExpression: '0 3 * * *',
    runAt: '',
    endAt: '',
    maxRuns: '',
  }
}

/** 即時実行と同じ入力欄で配置を確認し、時間規則と一緒に保存する。内容凍結は各 Run 作成時。 */
export function ScheduleDialog({ open, projectId, csrfToken, task, onClose, onSaved }: {
  open: boolean
  projectId: string
  csrfToken: string
  task: PublishedTaskRecord
  onClose: () => void
  onSaved: (schedule: ScheduleRecord) => void
}) {
  const messages = useMessages()
  const [draft, setDraft] = useState<ScheduleDraft>(emptyDraft)
  const [occurrences, setOccurrences] = useState<string[]>([])
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [inputText, setInputText] = useState('{}')
  const [sourceProviders, setSourceProviders] = useState(() => defaultSourceProviders(task))
  const saveController = useRef<AbortController | null>(null)
  const previewController = useRef<AbortController | null>(null)
  const taskDraft = buildTaskDraft(task, inputText, sourceProviders)

  const definition = useCallback((): ScheduleDefinitionInput => ({
    kind: draft.kind,
    timezone: draft.timezone.trim(),
    cron_expression: draft.kind === 'CRON' ? draft.cronExpression.trim() : null,
    run_at: draft.kind === 'ONCE' ? localInputToIso(draft.runAt) : null,
    end_at: draft.endAt ? localInputToIso(draft.endAt) : null,
    max_runs: draft.maxRuns ? Number(draft.maxRuns) : null,
  }), [draft])

  useEffect(() => () => {
    saveController.current?.abort()
    previewController.current?.abort()
  }, [])

  // 編集前の時間候補を現在の配置の確認結果として見せない。
  useEffect(() => {
    previewController.current?.abort()
    setOccurrences([])
  }, [definition])

  /** 保存前に次回発火時刻を確認する。不正な定義はここで Problem として返る。 */
  async function handlePreview(): Promise<void> {
    previewController.current?.abort()
    const controller = new AbortController()
    previewController.current = controller
    setFormError(null)
    try {
      const preview = await previewSchedule(projectId, definition(), csrfToken, controller.signal)
      if (!controller.signal.aborted) setOccurrences(preview)
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        setOccurrences([])
        setFormError(errorMessage(error))
      }
    }
  }

  /** 送信内容を一度固定し、二重クリックと画面破棄後の応答を無効にする。 */
  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    if (saveController.current) return
    if (!taskDraft) {
      setFormError(messages.workspace.documentSelection.incompleteDraft)
      return
    }
    const controller = new AbortController()
    saveController.current = controller
    setFormError(null)
    setSubmitting(true)
    try {
      const saved = await createSchedule(
        projectId,
        {
          name: draft.name.trim() || taskDraft.taskTitle,
          definition: definition(),
          skill_version_id: taskDraft.skillVersionId,
          task_key: taskDraft.taskKey,
          input: taskDraft.input,
          sources: taskDraft.sources,
        },
        csrfToken,
        controller.signal,
      )
      if (!controller.signal.aborted) onSaved(saved)
    } catch (error: unknown) {
      if (!controller.signal.aborted) setFormError(errorMessage(error))
    } finally {
      if (!controller.signal.aborted) {
        saveController.current = null
        setSubmitting(false)
      }
    }
  }

  return (
    <ModalDialog open={open} title={messages.schedules.create} wide onClose={onClose}>
      <form className="runForm" onSubmit={(event) => void handleSubmit(event)}>
        {/* 保存するのは精確版・入力・選択規則。全集の内容は各 occurrence の Run 作成で固定する。 */}
        <p className="hint">
          {messages.schedules.frozenTask(task.title)}
        </p>
        <fieldset disabled={submitting} className="scheduleConfiguration">
        <TaskLaunchFields
          task={task}
          inputText={inputText}
          sourceProviders={sourceProviders}
          onInputTextChange={setInputText}
          onSourceChange={(key, value) => setSourceProviders((current) => ({ ...current, [key]: value }))}
        />
        <label>{messages.schedules.nameLabel}
          <input
            maxLength={200}
            onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            placeholder={task.title}
            type="text"
            value={draft.name}
          />
        </label>
        <label>{messages.schedules.kindLabel}
          <select
            onChange={(event) => setDraft({ ...draft, kind: event.target.value as ScheduleKind })}
            value={draft.kind}
          >
            <option value="CRON">{messages.enums.scheduleKind.CRON}</option>
            <option value="ONCE">{messages.enums.scheduleKind.ONCE}</option>
          </select>
        </label>
        <label>{messages.schedules.timezoneLabel}
          <input
            maxLength={64}
            onChange={(event) => setDraft({ ...draft, timezone: event.target.value })}
            type="text"
            value={draft.timezone}
          />
        </label>
        {draft.kind === 'CRON' && (
          <label>{messages.schedules.cronLabel}
            <input
              maxLength={128}
              onChange={(event) => setDraft({ ...draft, cronExpression: event.target.value })}
              type="text"
              value={draft.cronExpression}
            />
          </label>
        )}
        {draft.kind === 'ONCE' && (
          <label>{messages.schedules.runAtLabel}
            <input
              onChange={(event) => setDraft({ ...draft, runAt: event.target.value })}
              type="datetime-local"
              value={draft.runAt}
            />
          </label>
        )}
        <label>{messages.schedules.endAtLabel}
          <input
            onChange={(event) => setDraft({ ...draft, endAt: event.target.value })}
            type="datetime-local"
            value={draft.endAt}
          />
        </label>
        <label>{messages.schedules.maxRunsLabel}
          <input
            min={1}
            onChange={(event) => setDraft({ ...draft, maxRuns: event.target.value })}
            type="number"
            value={draft.maxRuns}
          />
        </label>
        <div className="formRow">
          <button className="secondaryButton" type="button" onClick={() => void handlePreview()}>
            {messages.schedules.preview}
          </button>
        </div>
        {occurrences.length > 0 && (
          <>
            <p className="hint">{messages.schedules.previewHint}</p>
            <ol className="schedulePreview">
              {occurrences.map((item) => <li key={item} className="mono">{formatLocalTimestamp(item)}</li>)}
            </ol>
          </>
        )}
        <p className="hint">{messages.schedules.overlapHint}</p>
        </fieldset>
        {formError && <p className="error" role="alert">{formError}</p>}
        <button className="primaryButton" disabled={submitting || !taskDraft} type="submit">
          {submitting ? messages.schedules.saving : messages.schedules.save}
        </button>
      </form>
    </ModalDialog>
  )
}

/** 一つの定时执行に対する暂停・恢复・归档。遷移の可否は server の状態機が最終判定する。 */
export function ScheduleStatusActions({ schedule, projectId, csrfToken, onChanged, onError }: {
  schedule: ScheduleRecord
  projectId: string
  csrfToken: string
  onChanged: (schedule: ScheduleRecord) => void
  onError: (message: string) => void
}) {
  const messages = useMessages()
  const [busy, setBusy] = useState(false)

  async function apply(status: ScheduleStatus): Promise<void> {
    setBusy(true)
    try {
      onChanged(await changeScheduleStatus(projectId, schedule.schedule_id, status, csrfToken))
    } catch (error: unknown) {
      onError(errorMessage(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <span className="scheduleActions">
      {schedule.status === 'ACTIVE' && (
        <button className="secondaryButton compactButton" disabled={busy} type="button" onClick={() => void apply('PAUSED')}>
          {messages.schedules.pause}
        </button>
      )}
      {(schedule.status === 'PAUSED' || schedule.status === 'ERROR') && (
        <button className="secondaryButton compactButton" disabled={busy} type="button" onClick={() => void apply('ACTIVE')}>
          {messages.schedules.resume}
        </button>
      )}
      {schedule.status !== 'ARCHIVED' && (
        <button className="secondaryButton compactButton" disabled={busy} type="button" onClick={() => void apply('ARCHIVED')}>
          {messages.schedules.archive}
        </button>
      )}
    </span>
  )
}

/** 発火形態を一行で要約する。 */
export function summarizeTiming(schedule: ScheduleRecord): string {
  if (schedule.kind === 'CRON') return `${schedule.cron_expression ?? ''} · ${schedule.timezone}`
  return `${formatLocalTimestamp(schedule.run_at ?? '')} · ${schedule.timezone}`
}

/** `datetime-local` の現地時刻入力を UTC の ISO 文字列へ変換する。 */
function localInputToIso(value: string): string | null {
  if (!value) return null
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toISOString()
}

/** Problem Details 由来の message を優先し、未知例外でも表示可能な文字列にする。 */
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
