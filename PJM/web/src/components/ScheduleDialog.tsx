import { useCallback, useState, type FormEvent } from 'react'

import {
  changeScheduleStatus,
  createSchedule,
  previewSchedule,
  type ScheduleDefinitionInput,
  type ScheduleKind,
  type ScheduleRecord,
  type ScheduleStatus,
} from '../api'
import { useMessages } from '../i18n'
import { formatLocalTimestamp } from '../lib/presentation'
import type { TaskDraft } from '../lib/taskDraft'
import { ModalDialog } from './PageElements'

/** 弹窗 form の下書き。時刻に関する部分だけを持ち、task 側の設定は Run 下書きから凍結する。 */
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

/** 一つの task に定时执行を追加する弹窗。
 *
 * 凍結するのは呼び出し元が渡した Run 下書きそのもの。この弹窗が独自の task 設定 form を
 * 持たないのは、同じ意味の設定が二つの form に並存すると片方だけ直された状態が生まれ、
 * 実際に何が走るのか読めなくなるため（計画 §22 D4 は失効時に黙って別の来源へ切り替えない
 * ことを要求している）。
 */
export function ScheduleDialog({ open, projectId, csrfToken, taskDraft, onClose, onSaved }: {
  open: boolean
  projectId: string
  csrfToken: string
  /** 凍結対象。入力が JSON として読めない間は null で、その場合は保存させない。 */
  taskDraft: TaskDraft | null
  onClose: () => void
  onSaved: (schedule: ScheduleRecord) => void
}) {
  const messages = useMessages()
  const [draft, setDraft] = useState<ScheduleDraft>(emptyDraft)
  const [occurrences, setOccurrences] = useState<string[]>([])
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const definition = useCallback((): ScheduleDefinitionInput => ({
    kind: draft.kind,
    timezone: draft.timezone.trim(),
    cron_expression: draft.kind === 'CRON' ? draft.cronExpression.trim() : null,
    run_at: draft.kind === 'ONCE' ? localInputToIso(draft.runAt) : null,
    end_at: draft.endAt ? localInputToIso(draft.endAt) : null,
    max_runs: draft.maxRuns ? Number(draft.maxRuns) : null,
  }), [draft])

  /** 保存前に次回発火時刻を確認する。不正な定義はここで Problem として返る。 */
  async function handlePreview(): Promise<void> {
    setFormError(null)
    try {
      setOccurrences(await previewSchedule(projectId, definition(), csrfToken))
    } catch (error: unknown) {
      setOccurrences([])
      setFormError(errorMessage(error))
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    if (!taskDraft) return
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
      )
      setDraft(emptyDraft())
      setOccurrences([])
      onSaved(saved)
    } catch (error: unknown) {
      setFormError(errorMessage(error))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <ModalDialog open={open} title={messages.schedules.create} onClose={onClose}>
      <form className="runForm" onSubmit={(event) => void handleSubmit(event)}>
        {/* 何を凍結するのかを先に見せる。調度は保存時点の設定を固定し、後から差し替えない。 */}
        <p className="hint">
          {taskDraft ? messages.schedules.frozenTask(taskDraft.taskTitle) : messages.schedules.noTaskDraft}
        </p>
        <label>{messages.schedules.nameLabel}
          <input
            maxLength={200}
            onChange={(event) => setDraft({ ...draft, name: event.target.value })}
            placeholder={taskDraft?.taskTitle ?? ''}
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
