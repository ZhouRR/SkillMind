import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent } from 'react'

import { loadRunDetail, type RespondedInteractionRecord, type RunDetailRecord, type UserInteractionDetail } from '../api'
import { useInteractionResponse } from '../hooks/useInteractionResponse'
import { useResourceQuery, type SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { canConfirmInteraction, hasDuplicateChoiceOptionKeys, interactionAnswer, interactionPayload, INTERACTION_REQUEST_POLICY, sameInteractionIdentity,
  type FrozenInteractionResponse, type InteractionAccessFailure, type InteractionFailure, type InteractionScope, type PendingInteractionResponse } from '../lib/interactionResponse'
import { formatLocalTimestamp } from '../lib/presentation'

/** 公開 prompt と回答の文字列だけを表示し、任意値を HTML として解釈しない。 */
function publicText(value: unknown): string { return typeof value === 'string' ? value : '' }

/** 人間が読む回答の一覧。内容一致は原送信の成功判定に使わない。 */
export function interactionAnswerText(value: { text?: unknown; selected_option_keys?: unknown }): string {
  const keys = Array.isArray(value.selected_option_keys)
    ? value.selected_option_keys.filter((key): key is string => typeof key === 'string') : []
  return [keys.join(', '), publicText(value.text)].filter(Boolean).join(' · ') || '—'
}

/** actor/Project/Run owner の中で一 Interaction を保持し、状態が変わっても別区画へ移さない。 */
export function InteractionCard({ scope, interaction, available, writable, accessFailure, csrfToken, onResponded, onFacts, onSessionExpired }: {
  scope: InteractionScope
  interaction: UserInteractionDetail
  available: boolean
  writable: boolean
  accessFailure?: InteractionAccessFailure
  csrfToken: string
  onResponded?: (response: RespondedInteractionRecord) => void
  onFacts?: (detail: RunDetailRecord) => void
  onSessionExpired: SessionEnded
}) {
  const messages = useMessages()
  const labels = messages.interactionResponse
  const [text, setText] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [expanded, setExpanded] = useState(false)
  const notice = useRef<HTMLHeadingElement>(null)
  const response = useInteractionResponse({ scope, csrfToken, interaction, available, writable, accessFailure, onResponded, onSessionExpired })
  const pending = response.pending
  const isOpen = interaction.status === 'OPEN'
  const isOrdinary = interaction.interaction_type !== 'EFFECT_APPROVAL'
  const duplicateChoices = hasDuplicateChoiceOptionKeys(interaction)
  const answer = interactionAnswer(interaction, text, selected)
  const active = isOpen || pending !== null

  // 操作結果は可視カード内の見出しへ返し、hidden tab の焦点を奪わない。
  useEffect(() => {
    if (pending && pending.phase !== 'sending' && notice.current?.getClientRects().length) notice.current.focus()
  }, [pending?.phase])

  /** 草稿を検証した明示 submit だけが原要求を固定する。 */
  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    if (answer) response.start(answer)
  }

  return (
    <article className={`interactionCard interaction-${interaction.status.toLowerCase()}`} data-interaction-card={interaction.interaction_id}>
      <div className="segmentHeading">
        <strong>{messages.enums.interactionType[interaction.interaction_type] ?? interaction.interaction_type}</strong>
        <span>{messages.enums.interactionStatus[interaction.status]} · v{interaction.version}</span>
      </div>
      <h4>{publicText(interaction.prompt.prompt) || messages.runResult.interactionNeedInput}</h4>
      {!active && <button className="secondaryButton compactButton" type="button" aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}>{expanded ? labels.hideRecord : labels.showRecord}</button>}
      <div className="interactionBody" hidden={!active && !expanded}>
        <p>{publicText(interaction.prompt.rationale)}</p>
        {publicText(interaction.prompt.impact) && <p className="hint">{messages.runResult.impactLine(publicText(interaction.prompt.impact))}</p>}
        <p className="hint">{messages.runResult.deadlineLine(formatLocalTimestamp(interaction.expires_at), messages.enums.continuationMode[interaction.continuation_mode] ?? interaction.continuation_mode)}</p>
        {!isOrdinary && <p className="hint" data-interaction-effect-readonly="">{labels.effectReadOnly}</p>}
        {interaction.response && <p className="interactionAnswer">{messages.runResult.answeredLine(interactionAnswerText(interaction.response.response))}</p>}
        {duplicateChoices && <>
          <p className="hint" data-interaction-duplicate-options="">{labels.duplicateOptions}</p>
          <ul data-interaction-readonly-options="">{interaction.options.map((option, index) => <li key={`${index}:${publicText(option.key)}`}>
            <strong>{publicText(option.key)} · {publicText(option.label)}</strong>{option.recommended === true ? messages.runResult.recommendedSuffix : ''}
            {publicText(option.description) && <p>{publicText(option.description)}</p>}
          </li>)}</ul>
        </>}
        {isOrdinary && !duplicateChoices && (isOpen || pending !== null) && <form className="interactionForm" data-interaction-form="" onSubmit={submit}>
          {interaction.interaction_type === 'CHOICE' && interaction.options.map((option, index) => {
            const key = publicText(option.key)
            return <label className="interactionOption" key={`${index}:${key}`}><input data-interaction-option={key}
              checked={selected.includes(key)} name={`interaction-${interaction.interaction_id}`}
              type={interaction.prompt.allow_multiple === true ? 'checkbox' : 'radio'} value={key}
              onChange={(event) => setSelected((prior) => event.target.checked
                ? interaction.prompt.allow_multiple === true ? [...prior.filter((item) => item !== key), key] : [key]
                : prior.filter((item) => item !== key))} />
              <span><strong>{publicText(option.label) || key}{option.recommended === true ? messages.runResult.recommendedSuffix : ''}</strong>{publicText(option.description)}</span>
            </label>
          })}
          <label>{interaction.interaction_type === 'CHOICE' ? messages.runResult.choiceNote : messages.runResult.yourAnswer}
            <textarea data-interaction-text="" required={interaction.interaction_type !== 'CHOICE'}
              value={text} onChange={(event) => setText(event.target.value)} />
          </label>
          {[...text.trim()].length > 10_000 && <p className="error" role="alert">{labels.answerTooLong}</p>}
          <button className="primaryButton" type="submit" data-interaction-submit=""
            disabled={!answer || !writable || !available || response.accessFailure !== null || !scope.actorId || !csrfToken || pending !== null || !isOpen}>
            {response.busy ? messages.runResult.submitting : messages.runResult.submitAndContinue}
          </button>
          {pending && <p className="hint">{labels.draftSeparate}</p>}
        </form>}
        {!writable && isOpen && <p className="hint">{labels.staleDetail}</p>}
        {!pending && response.accessFailure && <p className="error" role="alert">{labels.failures[response.accessFailure.key]}</p>}
        {pending && <section className="interactionIntent" data-interaction-intent="" data-interaction-phase={pending.phase}>
          <h4 ref={notice} tabIndex={-1} role="status">{labels.phase[pending.phase]}</h4>
          <p>{labels.originalVersion(pending.request.version)}</p>
          <blockquote>{interactionAnswerText(interactionPayload(pending.request))}</blockquote>
          <dl className="interactionFacts"><div><dt>{labels.originalActor}</dt><dd>{pending.request.actorId}</dd></div>
            <div><dt>{labels.originalTarget}</dt><dd>{pending.request.projectId} / {pending.request.runId} / {pending.request.interactionId}</dd></div></dl>
          {pending.failure && <p className="error" role="alert">{labels.failures[pending.failure.key]}</p>}
          {pending.uncertain && pending.phase !== 'confirmed' && <p className="hint">{labels.unknownHint}</p>}
          {pending.receipt && <dl className="interactionFacts" data-interaction-receipt="">
            <div><dt>{labels.responseId}</dt><dd>{pending.receipt.response_id}</dd></div>
            <div><dt>{labels.continuationId}</dt><dd>{pending.receipt.run_segment_id}</dd></div>
          </dl>}
          {canConfirmInteraction(pending) && !response.accessFailure && <>
            <p className="hint">{labels.confirmHint}</p>
            <button className="primaryButton" type="button" data-interaction-confirm-original=""
              disabled={response.busy || !available} onClick={response.confirmOriginal}>{labels.confirmOriginal}</button>
          </>}
          {pending.phase === 'rejected' && !pending.uncertain && pending.failure?.key === 'invalidAnswer' &&
            <button className="secondaryButton" data-interaction-edit="" type="button" onClick={response.edit}>{labels.editAnswer}</button>}
          {pending.phase !== 'sending' && <InteractionFacts request={pending.request} pending={pending}
            onSessionExpired={response.onReadSessionExpired} onFacts={onFacts} onAccessRejected={response.rejectAccess} />}
        </section>}
      </div>
    </article>
  )
}

/** 原 Run の GET は保存状態の参照であり、答復 key の受理回执には昇格させない。 */
function InteractionFacts({ request, pending, onSessionExpired, onFacts, onAccessRejected }: {
  request: FrozenInteractionResponse
  pending: PendingInteractionResponse
  onSessionExpired: SessionEnded
  onFacts?: (detail: RunDetailRecord) => void
  onAccessRejected: (failure: InteractionFailure) => void
}) {
  const messages = useMessages()
  const labels = messages.interactionResponse
  const [enabled, setEnabled] = useState(false)
  const loader = useCallback((signal: AbortSignal) => loadRunDetail(request.projectId, request.runId, signal), [request])
  const query = useResourceQuery(request.idempotencyKey, loader, onSessionExpired, INTERACTION_REQUEST_POLICY, enabled)
  const ready = enabled && !query.pending && !query.failure && query.data !== null
  // 新しい GET の拒否は paint 前に同期 gate へ反映し、button 表示だけを防壁にしない。
  useLayoutEffect(() => { if (enabled && query.failure) onAccessRejected(query.failure) }, [enabled, query.failure, onAccessRejected])
  useEffect(() => { if (ready && query.data) onFacts?.(query.data) }, [ready, query.data, onFacts])
  /** 同じ画面の二回目の照合も新しい読取世代を明示する。 */
  function read(): void { if (enabled) query.refresh(); else setEnabled(true) }
  const current = ready ? query.data?.interactions.find((item) => sameInteractionIdentity(item.interaction_id, request.interactionId)) : undefined
  return <div className="interactionReadback">
    <button type="button" className="secondaryButton" data-interaction-read-original="" disabled={enabled && query.pending} onClick={read}>{labels.readOriginal}</button>
    {enabled && query.pending && <p role="status">{labels.reading}</p>}
    {enabled && query.failure && <p className="error" role="alert" data-interaction-read-failure="">{labels.failures[query.failure.key]}</p>}
    {ready && <div data-interaction-facts="">
      <p>{labels.factsOnly}</p>
      <p>{labels.currentState}: {messages.enums.runStatus[query.data!.status]} · {current ? messages.enums.interactionStatus[current.status] : labels.notInDetail}</p>
      {current?.response && <><p>{messages.runResult.answeredLine(interactionAnswerText(current.response.response))}</p>
        <dl className="interactionFacts"><div><dt>{labels.responseId}</dt><dd>{current.response.response_id}</dd></div>
          <div><dt>{labels.originalActor}</dt><dd>{current.response.actor_id}</dd></div></dl></>}
      <ul className="interactionSegments">{query.data!.segments.map((segment) => <li key={segment.run_segment_id ?? segment.segment_no}>
        {messages.runResult.segmentTitle(segment.segment_no)} · {messages.enums.segmentTrigger[segment.trigger_type] ?? segment.trigger_type} · <code>{segment.run_segment_id ?? '—'}</code>
      </li>)}</ul>
      {pending.phase !== 'confirmed' && <p className="hint">{labels.notConfirmation}</p>}
    </div>}
  </div>
}
