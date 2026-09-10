import type { ScheduleActivity } from '../api'
import { useMessages } from '../i18n'
import type { ScheduleReadFailure } from '../lib/scheduleManager'
import { formatScheduleTimestamp } from '../lib/scheduleTime'
import { apiTimestampMicroseconds } from '../lib/validation'

/** 読取結果だけを受け取り、Server の lease から実行権や再送許可を補造しない。 */
export interface ScheduleActivityPanelProps {
  data: ScheduleActivity | null
  pending: boolean
  failure: ScheduleReadFailure | null
  onRefresh: () => void
}

/** 独立した一回の確認を表示し、更新中/失敗時の旧値を現在の空状態に見せない。 */
export function ScheduleActivityPanel({ data, pending, failure, onRefresh }: ScheduleActivityPanelProps) {
  const messages = useMessages()
  const labels = messages.scheduleActivity
  const ready = !pending && !failure && data !== null
  const occurrence = ready ? data.pending : null
  const expired = occurrence && apiTimestampMicroseconds(occurrence.lease_expires_at) <= apiTimestampMicroseconds(data!.checked_at)
  const exhausted = occurrence && occurrence.attempt_count >= data!.automatic_attempt_limit
  return <section className="scheduleFacts" data-schedule-activity aria-label={labels.title}>
    <h3>{labels.title}</h3>
    <p className="hint">{labels.scopeHint}</p>
    <button className="secondaryButton compactButton" type="button" data-schedule-activity-refresh
      disabled={pending} onClick={onRefresh}>{labels.refresh}</button>
    {pending && <p role="status" data-schedule-activity-loading>{labels.loading}</p>}
    {!pending && failure && <p className="error" role="alert" data-schedule-activity-error>{messages.scheduleManager.failures[failure.key]}</p>}
    {ready && <div data-schedule-activity-tracking={data.tracking}>
      <dl>
        <div><dt>{labels.checkedAt}</dt><dd data-schedule-activity-checked-at><ActivityTimestamp value={data.checked_at} /></dd></div>
        <div><dt>{labels.rowVersion}</dt><dd>{data.row_version}</dd></div>
        <div><dt>{labels.currentConfiguration}</dt><dd>{data.configuration_version}</dd></div>
      </dl>
      {data.tracking === 'LEGACY_UNAVAILABLE' ? <p className="scheduleNotice">{labels.legacy}</p>
        : !occurrence ? <><p data-schedule-activity-empty>{labels.empty}</p><p className="hint">{labels.emptyLimit}</p></>
          : <div data-schedule-pending>
            <h4>{labels.pendingTitle}</h4>
            <dl>
              <div><dt>{labels.occurrenceId}</dt><dd className="mono">{occurrence.occurrence_id}</dd></div>
              <div><dt>{labels.occurrenceAt}</dt><dd>{formatScheduleTimestamp(occurrence.occurrence_at, 'UTC')}
                <div><ActivityTimestamp value={occurrence.occurrence_at} /></div></dd></div>
              <div><dt>{labels.originalConfiguration}</dt><dd>{occurrence.configuration_version}</dd></div>
              <div><dt>{labels.attempts}</dt><dd>{occurrence.attempt_count} / {data.automatic_attempt_limit}</dd></div>
              <div><dt>{labels.createdAt}</dt><dd><ActivityTimestamp value={occurrence.created_at} /></dd></div>
              <div><dt>{labels.updatedAt}</dt><dd><ActivityTimestamp value={occurrence.updated_at} /></dd></div>
              <div><dt>{labels.leaseExpiresAt}</dt><dd><ActivityTimestamp value={occurrence.lease_expires_at} /></dd></div>
            </dl>
            <p className="hint">{labels.configurationHint}</p>
            <p data-schedule-lease={expired ? 'expired' : 'active'}>{expired ? labels.leaseExpired : labels.leaseActive}</p>
            <p data-schedule-attempt-limit={exhausted ? 'reached' : 'remaining'}>{exhausted ? labels.attemptsReached : labels.attemptsRemaining}</p>
            <p className="scheduleNotice">{labels.leaseLimit}</p>
          </div>}
    </div>}
  </section>
}

/** Offset と小数秒を残し、分単位の表示を lease 判定の精度に見せない。 */
function ActivityTimestamp({ value }: { value: string }) {
  return <time className="mono" dateTime={value}>{value}</time>
}
