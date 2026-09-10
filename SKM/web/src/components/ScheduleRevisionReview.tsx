import { useEffect, useRef } from 'react'

import type { ScheduleRecord } from '../api'
import type { ScheduleIntent, ScheduleRevision } from '../hooks/useScheduleRevision'
import { useMessages } from '../i18n'
import { formatScheduleTimestamp } from '../lib/scheduleTime'

/** 原値・凍結した送信値・現在値を別々に表示し、GET を成功回执に見せない。 */
export function ScheduleRevisionReview({ revision, onAdopt, visible = true }: {
  revision: ScheduleRevision; onAdopt: () => void; visible?: boolean
}) {
  const messages = useMessages().scheduleEditor
  const heading = useRef<HTMLDivElement>(null)
  useEffect(() => { if (visible && revision.facts) heading.current?.focus() }, [visible, revision.facts])
  const review = revision.phase === 'conflict' || revision.phase === 'unknown' || revision.phase === 'denied'
  return <>
    {revision.previousUnknown && <details className="rawResult" data-schedule-previous-unknown>
      <summary>{messages.previousUnknown}</summary><p>{messages.factLimit}</p>
      <pre>{JSON.stringify(revision.previousUnknown.payload, null, 2)}</pre>
    </details>}
    {revision.failure && <p role="alert" data-schedule-save-failure>{messages.failures[revision.failure.key]}</p>}
    {review && revision.intent && <section className="submissionNotice" data-schedule-review={revision.phase}>
      <h3>{revision.phase === 'conflict' ? messages.conflictTitle : messages.unknownTitle}</h3>
      <p>{messages.factLimit}</p>
      <div data-schedule-comparison>
        <ScheduleFacts title={messages.original} marker="original" value={revision.intent.original} />
        <ScheduleFacts title={messages.submitted} marker="submitted" value={revision.intent.payload} />
        {revision.facts && <div ref={heading} tabIndex={-1}>
          <ScheduleFacts title={messages.current} marker="current" value={revision.facts} />
        </div>}
      </div>
      {revision.readFailure && <p role="alert">{messages.failures[revision.readFailure.key]}</p>}
      {revision.readPending && <p role="status">{messages.reading}</p>}
      <div className="formRow">
        <button className="secondaryButton" type="button" data-schedule-reconcile
          disabled={revision.accessDenied || revision.readPending || !visible}
          onClick={() => { if (visible) revision.reconcile() }}>{messages.reconcile}</button>
        <button className="secondaryButton" type="button" data-schedule-adopt
          disabled={!revision.canAdopt || !visible} onClick={onAdopt}>{messages.adopt}</button>
      </div>
      {revision.facts && !revision.canAdopt && !revision.accessDenied && <p>{messages.unavailable}</p>}
    </section>}
  </>
}

/** 名前の付いた元 JSON は現在の読み取り事実だけを使い、任意 HTML として解釈しない。 */
function ScheduleFacts({ title, marker, value }: {
  title: string; marker: string; value: ScheduleRecord | ScheduleIntent['payload']
}) {
  const messages = useMessages()
  const definition = 'definition' in value ? value.definition : 'kind' in value ? value : null
  const version = 'row_version' in value ? value.row_version : value.expected_row_version
  return <section {...{ ['data-schedule-' + marker]: '' }}>
    <h4>{title}</h4>
    <dl className="eventIdentity">
      {'name' in value && <div><dt>{messages.schedules.nameLabel}</dt><dd>{value.name}</dd></div>}
      {'status' in value && <div><dt>{messages.scheduleManager.statusLabel}</dt><dd>{messages.enums.scheduleStatus[value.status]}</dd></div>}
      <div><dt>{messages.scheduleManager.fields.rowVersion}</dt><dd>{version}</dd></div>
      {definition && <>
        <div><dt>{messages.schedules.kindLabel}</dt><dd>{messages.enums.scheduleKind[definition.kind]}</dd></div>
        <div><dt>{messages.schedules.timezoneLabel}</dt><dd>{definition.timezone}</dd></div>
        <div><dt>{definition.kind === 'CRON' ? messages.schedules.cronLabel : messages.schedules.runAtLabel}</dt>
          <dd>{definition.kind === 'CRON' ? definition.cron_expression
            : formatScheduleTimestamp(definition.run_at ?? '', definition.timezone)}</dd></div>
      </>}
    </dl>
    <details className="rawResult"><summary>{messages.elements.detail}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre></details>
  </section>
}
