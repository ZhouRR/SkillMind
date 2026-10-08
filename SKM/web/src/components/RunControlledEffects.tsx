import { useState, type ReactNode } from 'react'

import type { ChangeProposalRecord, RunDetailRecord } from '../api'
import { useProposalDecision } from '../hooks/useProposalDecision'
import type { SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import { formatLocalTimestamp } from '../lib/presentation'
import { displayText } from '../lib/resultPresentation'
import '../styles/execution-audit.css'

/** 期限内で未決の提案だけが「今 approve/reject できるもの」。 */
export function isDecidable(proposal: ChangeProposalRecord): boolean {
  return proposal.status === 'PENDING_APPROVAL'
    && new Date(proposal.expires_at).getTime() > Date.now()
}

/** 決着済みの効果を件数付きの備査区画へまとめる。 */
function CollapsibleSection({ title, count, children }: {
  title: string
  count?: number
  children: ReactNode
}) {
  return (
    <details className="resultCollapse">
      <summary>
        <span className="resultCollapseTitle">{title}</span>
        {count !== undefined && <span className="eventCount">{count}</span>}
      </summary>
      <div className="resultCollapseBody">{children}</div>
    </details>
  )
}

/** 未決の変更提案を結果より上へ集約する。普通答復は RunInteractions が別の owner を持つ。
 *
 *  Run が回答・承認待ちで停止している時、Workspace は自動でこの画面へ切り替える。
 *  利用者は「何をすれば続くのか」を探しに来るので、監査記録の下に置いてはいけない。 */
export function PendingActionsSection({ detail, proposals, csrfToken, onDecided, actorId = '', readOnly = false, available = true, onSessionExpired }: {
  detail: RunDetailRecord
  proposals: ChangeProposalRecord[]
  csrfToken: string
  onDecided?: () => void
  actorId?: string
  readOnly?: boolean
  available?: boolean
  onSessionExpired?: SessionEnded
}) {
  const messages = useMessages()
  if (proposals.length === 0) return null
  return (
    <section className="resultSection pendingActions">
      <div className="subsectionHeader">
        <h3>{messages.runResult.pendingTitle}</h3>
        <span>{proposals.length}</span>
      </div>
      <p className="hint">{messages.runResult.pendingHint}</p>
      {proposals.map((proposal) => (
        <ChangeProposalCard
          actorId={actorId}
          readOnly={readOnly}
          available={available}
          onSessionExpired={onSessionExpired}
          csrfToken={csrfToken}
          detail={detail}
          key={JSON.stringify([actorId, csrfToken, detail.project_id, detail.run_id, proposal.proposal_id])}
          onDecided={onDecided}
          proposal={proposal}
        />
      ))}
    </section>
  )
}

/** 決着済みの Proposal と EffectExecution を observe→propose→apply の順に監査表示する。
 *
 *  未決の提案は上部の「対応が必要」へ移してあるため、ここは記録としてのみ畳んで置く。 */
export function ControlledEffectsSection({ detail, proposals, csrfToken, onDecided, actorId = '', readOnly = false, available = true, onSessionExpired }: {
  detail: RunDetailRecord
  proposals: ChangeProposalRecord[]
  csrfToken: string
  onDecided?: () => void
  actorId?: string
  readOnly?: boolean
  available?: boolean
  onSessionExpired?: SessionEnded
}) {
  const messages = useMessages()
  if (
    proposals.length === 0
    && detail.approvals.length === 0
    && detail.effect_executions.length === 0
  ) return null
  return (
    <CollapsibleSection count={proposals.length} title={messages.runResult.controlledEffects}>
      <p className="hint">{messages.runResult.platformEffectsHint}</p>
      <div className="proposalList">
        {proposals.map((proposal) => (
          <ChangeProposalCard
            actorId={actorId}
            readOnly={readOnly}
            available={available}
            onSessionExpired={onSessionExpired}
            csrfToken={csrfToken}
            detail={detail}
            key={JSON.stringify([actorId, csrfToken, detail.project_id, detail.run_id, proposal.proposal_id])}
            onDecided={onDecided}
            proposal={proposal}
          />
        ))}
      </div>
      {detail.effect_executions.length > 0 && (
        <ol className="effectTimeline">
          {detail.effect_executions.map((effect) => {
            const approval = detail.approvals.find(
              (item) => item.approval_id === effect.approval_id,
            )
            return (
              <li key={effect.effect_execution_id}>
                <div className="segmentHeading">
                  <strong>{effect.provider} · {effect.error?.code === 'effect_result_unknown'
                    ? messages.runResult.effectResultUnknown
                    : (messages.enums.effectStatus[effect.status] ?? effect.status)}</strong>
                  <span>{messages.runResult.attemptNo(effect.attempt_no)}</span>
                </div>
                <p>
                  {approval ? `${approval.source} ${approval.decision}` : messages.runResult.approvalPending}
                  {' · '}
                  {messages.runResult.beforeAfterLine(effect.before_ref ?? '—', effect.after_ref ?? '—')}
                </p>
                {effect.error && <code>{JSON.stringify(effect.error)}</code>}
              </li>
            )
          })}
        </ol>
      )}
    </CollapsibleSection>
  )
}

/** Exact version/checksum と送信済み原要求を表示し、照合で write を再送しない。 */
function ChangeProposalCard({ detail, proposal, csrfToken, onDecided, actorId = '', readOnly = false, available = true, onSessionExpired }: {
  detail: RunDetailRecord
  proposal: ChangeProposalRecord
  csrfToken: string
  onDecided?: () => void
  actorId?: string
  readOnly?: boolean
  available?: boolean
  onSessionExpired?: SessionEnded
}) {
  const messages = useMessages()
  const labels = messages.uiAuditWorkspace
  const [reason, setReason] = useState(messages.runResult.defaultApprovalReason)
  const submission = useProposalDecision({
    scope: { actorId, projectId: detail.project_id, runId: detail.run_id, proposalId: proposal.proposal_id },
    proposal, csrfToken, writable: !readOnly && available, onDecided, onSessionExpired,
  })
  const pending = submission.pending

  const canDecide = isDecidable(proposal)
  return (
    <article className={`proposalCard proposal-${proposal.status.toLowerCase()}`}>
      <div className="segmentHeading">
        <strong>{proposal.proposal_ref} · {proposal.operation}</strong>
        <span>{messages.enums.proposalStatus[proposal.status] ?? proposal.status} · {messages.enums.riskLevel[proposal.risk_level] ?? proposal.risk_level}</span>
      </div>
      <h4>{proposal.summary}</h4>
      <dl className="proposalFacts">
        <div className="proposalTarget"><dt>{messages.runResult.targetLabel}</dt><dd>{displayText(proposal.target.display, displayText(proposal.target.locator, '—'))}</dd></div>
        <div><dt>{messages.runResult.capabilityLabel}</dt><dd>{proposal.capability_version}</dd></div>
        <div><dt>{messages.runResult.versionLabel}</dt><dd>{proposal.version}</dd></div>
        <div><dt>{messages.runResult.expiresLabel}</dt><dd>{formatLocalTimestamp(proposal.expires_at)}</dd></div>
      </dl>
      <ProposalChanges changes={proposal.changes} precondition={proposal.precondition} />
      <p className="hint">{messages.runResult.evidenceLine(proposal.evidence_refs, proposal.checksum)}</p>
      {readOnly && canDecide && <p className="hint">{labels.proposalReadOnly}</p>}
      {submission.storageUnavailable && <p className="error" role="alert">{labels.proposalStorageUnavailable}</p>}
      {pending && <section className="proposalOriginal" aria-label={labels.proposalOriginal}>
        <h4>{labels.proposalOriginal}</h4>
        <dl className="proposalRequestFacts">
          <div><dt>{labels.proposalRequestKey}</dt><dd><code>{pending.request.key}</code></dd></div>
          <div><dt>{labels.proposalDecision}</dt><dd>{messages.enums.proposalStatus[pending.request.decision]}</dd></div>
          <div><dt>{messages.runResult.versionLabel}</dt><dd>{pending.request.version} · <code>{pending.request.checksum}</code></dd></div>
          <div><dt>{messages.runResult.approvalReason}</dt><dd>{pending.request.reason}</dd></div>
        </dl>
        <p role="status">{submission.checking ? labels.proposalChecking
          : pending.phase === 'sending' ? labels.proposalSending
          : pending.phase === 'confirmed' ? labels.proposalConfirmed
          : pending.phase === 'observed' ? labels.proposalObserved
          : pending.phase === 'conflict' ? labels.proposalConflict
          : pending.phase === 'rejected' ? labels.proposalRejected
          : labels.proposalUnknown}</p>
        {pending.phase === 'unknown' && <p className="hint">{submission.checked ? labels.proposalPending : labels.proposalUnknownHint}</p>}
        {pending.approval && <p>{messages.enums.proposalStatus[pending.approval.decision]} · {formatLocalTimestamp(pending.approval.created_at)} · <code>{pending.approval.approval_id}</code></p>}
        {pending.error && <p className="error" role="alert">{pending.error}</p>}
        {submission.readFailed && <p className="error" role="alert">{labels.proposalReadFailed}</p>}
        {pending.phase !== 'sending' && pending.phase !== 'confirmed' && <button type="button" className="secondaryButton"
          disabled={submission.checking || submission.accessDenied} onClick={() => void submission.check()}>{labels.proposalCheck}</button>}
        {pending.editable && pending.phase === 'rejected' && <button className="secondaryButton" type="button"
          disabled={readOnly || !available || submission.checking || submission.accessDenied}
          onClick={submission.editRejected}>{labels.proposalEditRejected}</button>}
      </section>}
      {canDecide && !pending && (
        <div className="proposalDecision">
          <label>{messages.runResult.approvalReason}
            <textarea
              disabled={submission.locked}
              maxLength={1000}
              required
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <div className="formRow">
            <button
              className="primaryButton"
              disabled={submission.locked || !reason.trim()}
              onClick={() => void submission.start('APPROVED', reason)}
              type="button"
            >{messages.runResult.approveAndApply}</button>
            <button
              className="dangerButton"
              disabled={submission.locked || !reason.trim()}
              onClick={() => void submission.start('REJECTED', reason)}
              type="button"
            >{messages.runResult.rejectAndContinue}</button>
          </div>
        </div>
      )}
      {proposal.status === 'PENDING_APPROVAL' && !canDecide && (
        <p className="error">{messages.runResult.proposalExpired}</p>
      )}
    </article>
  )
}

/** 確認対象の原差分と前提条件を、承認理由の編集とは独立して表示する。 */
function ProposalChanges({
  changes,
  precondition,
}: {
  changes: readonly Record<string, unknown>[]
  precondition: Record<string, unknown>
}): ReactNode {
  const messages = useMessages()
  const files = fileChanges(changes)
  if (files.length === 0) {
    // issue field 更新のように値が小さい提案は、従来どおり構造をそのまま見せる方が速い。
    return <pre tabIndex={0} role="region" aria-label={messages.uiAuditWorkspace.proposalChanges}>{JSON.stringify({ changes, precondition }, null, 2)}</pre>
  }
  const baseRevision = typeof precondition.revision === 'string' ? precondition.revision : '—'
  return (
    <div className="proposalChanges">
      <p className="hint">{messages.runResult.changeBaseRevision(baseRevision, files.length)}</p>
      {files.map((file) => (
        <details key={file.path} className="proposalFile">
          <summary>
            <code>{file.path}</code>
            <span className={`changeAction change-${file.action.toLowerCase()}`}>
              {messages.runResult.changeAction[file.action] ?? file.action}
            </span>
            {file.action === 'SET' && (
              <span className="hint">{messages.runResult.changeSize(file.lines, file.bytes)}</span>
            )}
          </summary>
          {file.action === 'SET'
            ? <pre className="proposalFileBody" tabIndex={0} role="region" aria-label={file.path}>{file.content}</pre>
            : <p className="hint">{messages.runResult.changeRemoved}</p>}
        </details>
      ))}
    </div>
  )
}

/** repository 変更 (`/files/<path>`) だけを描画単位へ落とす。他 capability は空配列。 */
function fileChanges(changes: readonly Record<string, unknown>[]): {
  path: string
  action: string
  content: string
  lines: number
  bytes: number
}[] {
  return changes.flatMap((change) => {
    const path = change.path
    const action = change.action
    if (typeof path !== 'string' || !path.startsWith('/files/')) return []
    const content = typeof change.value === 'string' ? change.value : ''
    return [{
      path: path.slice('/files/'.length),
      action: typeof action === 'string' ? action : 'SET',
      content,
      lines: content === '' ? 0 : content.split('\n').length,
      bytes: new TextEncoder().encode(content).length,
    }]
  })
}
