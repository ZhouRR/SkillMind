import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'

import {
  createEvaluation,
  decideChangeProposal,
  loadEvaluations,
  respondToInteraction,
  type AgentSessionDetail,
  type ChangeProposalRecord,
  type CreateEvaluationInput,
  type EvaluationRecord,
  type EvidenceDetail,
  type RespondedInteractionRecord,
  type RunDetailRecord,
  type UserInteractionDetail,
} from '../api'
import { useMessages } from '../i18n'
import { createIdempotencyKey } from '../lib/idempotency'
import { formatLocalTimestamp } from '../lib/presentation'
import { splitOverflow } from '../lib/resultOverflow'
import { EmptyState } from './PageElements'
import { RunDocumentSnapshots } from './RunDocumentSnapshots'

/** Run detail 非同期読み込みの排他的 UI state。 */
export type RunDetailState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; detail: RunDetailRecord }
  | { status: 'error'; message: string }

/** Segment/Interaction 監査と検証済み Result、ToolCall、Evidence を標準表示する。 */
export function RunResultPanel({ state, csrfToken, onInteractionResponded, onProposalDecided }: {
  state: RunDetailState
  csrfToken: string
  onInteractionResponded?: (response: RespondedInteractionRecord) => void
  onProposalDecided?: () => void
}) {
  const messages = useMessages()
  const [showTechnicalDetails, setShowTechnicalDetails] = useState(false)
  if (state.status === 'idle') {
    return <EmptyState text={messages.runResult.idleEmpty} />
  }
  if (state.status === 'loading') return <EmptyState text={messages.runResult.loadingEmpty} />
  if (state.status === 'error') return <p className="error" role="alert">{state.message}</p>
  const { detail } = state
  const result = detail.result
  // 「今あなたが動く必要があるもの」と「後から辿る記録」を分ける。前者は結果より上へ、
  // 後者は既定で畳む。同じ縦一列に全部広げると、待ち事項も結果も監査の中に埋もれる。
  const openInteractions = detail.interactions.filter((item) => item.status === 'OPEN')
  const settledInteractions = detail.interactions.filter((item) => item.status !== 'OPEN')
  const decidableProposals = detail.change_proposals.filter(isDecidable)
  const settledProposals = detail.change_proposals.filter((item) => !isDecidable(item))
  const versionId = detail.skill_snapshots[0]?.skill_version_id
  const dispatches = collectSubagentDispatches(detail.evidence)
  return (
    <div className="resultView">
      <PendingActionsSection
        csrfToken={csrfToken}
        detail={detail}
        interactions={openInteractions}
        onDecided={onProposalDecided}
        onInteractionResponded={onInteractionResponded}
        proposals={decidableProposals}
      />
      <div className="technicalToggleRow">
        <button
          aria-pressed={showTechnicalDetails}
          className="secondaryButton compactButton"
          type="button"
          onClick={() => setShowTechnicalDetails((current) => !current)}
        >
          {showTechnicalDetails ? messages.runResult.hideTechnicalDetails : messages.runResult.technicalDetails}
        </button>
      </div>
      {result === null ? (
        <EmptyState text={messages.runResult.noValidatedResult(messages.enums.runStatus[detail.status] ?? detail.status)} />
      ) : <>
      {/* 結果区画の先頭に固定される行。畳まれた要約の全文は title から読める。 */}
      <section className="resultSummary">
        <div>
          <span>{messages.runResult.summaryLabel}</span>
          <h3 title={result.summary}>{result.summary}</h3>
        </div>
        <dl>
          <div><dt>{messages.runResult.confidenceLabel}</dt><dd>{formatConfidence(result.confidence)}</dd></div>
          <div><dt>{messages.runResult.reviewLabel}</dt><dd>{result.needs_review ? messages.runResult.needsReview : messages.runResult.noExtraReview}</dd></div>
          <div><dt>{messages.runResult.schemaCheckLabel}</dt><dd>{result.validation.schema_valid === true ? messages.runResult.schemaValidText : messages.runResult.schemaCheckRequiredText}</dd></div>
          {/* UUID 全文は判断材料にならないうえ右列を押し広げて要約を潰す。全文は title へ退避する。 */}
          <div><dt>{messages.runResult.taskVersionLabel}</dt><dd title={versionId}>{versionId ? shortId(versionId) : '—'}</dd></div>
        </dl>
      </section>

      <section className="resultSection">
        <div className="subsectionHeader">
          <h3>{result.result_kind === 'OUTCOME_ENVELOPE' ? messages.runResult.genericOutcome : messages.runResult.structuredResult}</h3>
          <span>{result.result_kind === 'OUTCOME_ENVELOPE' ? messages.runResult.formatBadgeOutcome : (detail.output_schema_checksum ? messages.runResult.formatBadgeSchema : messages.runResult.formatBadgeLegacy)}</span>
        </div>
        {result.result_kind === 'OUTCOME_ENVELOPE'
          ? <OutcomeEnvelopeResult data={result.data} schema={detail.output_schema} showTechnicalDetails={showTechnicalDetails} />
          : <SchemaResultValue schema={detail.output_schema} value={result.data} path="$" showTechnicalDetails={showTechnicalDetails} />}
      </section>
      </>}

      {/* 扇出は「結論の根拠がどこまで揃っているか」を左右するので備査へ畳まない。
          一路が失敗していても要約は普通に返るため、畳むと部分的な網羅を全面的な確認と読む。 */}
      {dispatches.length > 0 && <SubagentDispatchSection dispatches={dispatches} />}

      <RunDocumentSnapshots snapshots={detail.document_snapshots} />

      {/* 以下は備査情報。既定で畳み、件数だけ見出しに残す。 */}
      <CollapsibleSection count={detail.tool_calls.length} title={messages.runResult.toolCalls}>
        {detail.tool_calls.length === 0 ? <p className="compactEmpty">{messages.runResult.noToolCalls}</p> : (
          <ul className="toolSummaryList">{detail.tool_calls.map((tool) => <li key={tool.tool_call_id}><div><strong>{tool.capability}</strong><span>{tool.status}</span></div><p>{tool.provider} · {tool.duration_ms === null ? '—' : `${tool.duration_ms} ms`}</p><code>{JSON.stringify(tool.arguments_summary)}</code></li>)}</ul>
        )}
      </CollapsibleSection>

      <CollapsibleSection count={detail.evidence.length} title={messages.runResult.evidenceTitle}>
        {detail.evidence.length === 0 ? <p className="compactEmpty">{messages.runResult.noEvidence}</p> : (
          <div className="evidenceList">{detail.evidence.map((evidence) => <EvidenceCard evidence={evidence} key={evidence.evidence_ref} />)}</div>
        )}
      </CollapsibleSection>

      <CollapsibleSection count={detail.segments.length} title={messages.runResult.conversationAudit}>
        <RunConversationAudit
          csrfToken={csrfToken}
          detail={detail}
          interactions={settledInteractions}
          onInteractionResponded={onInteractionResponded}
        />
      </CollapsibleSection>

      <ControlledEffectsSection
        csrfToken={csrfToken}
        detail={detail}
        onDecided={onProposalDecided}
        proposals={settledProposals}
      />

      {result !== null && (
        <CollapsibleSection title={messages.runResult.viewRawResult}>
          <pre className="rawResultBody">{JSON.stringify(result.data, null, 2)}</pre>
        </CollapsibleSection>
      )}

      {result !== null && <EvaluationSection csrfToken={csrfToken} detail={detail} />}
    </div>
  )
}

/** 期限内で未決の提案だけが「今 approve/reject できるもの」。 */
function isDecidable(proposal: ChangeProposalRecord): boolean {
  return proposal.status === 'PENDING_APPROVAL'
    && new Date(proposal.expires_at).getTime() > Date.now()
}

/** 備査情報を既定で畳み、見出しに件数だけ残す共通区画。
 *
 *  `<details>` を使うのは、畳んでいても子要素が DOM に残り、頁面測試(renderToStaticMarkup)の
 *  内容断言を壊さないため。外側に既に観測 tab があるので、ここで二段目の tab は増やさない。 */
/** 一回の扇出の投影。証拠 metadata が唯一の出所で、画面側では再計算しない。 */
interface SubagentDispatch {
  evidenceRef: string
  objective: string
  branches: Array<{ key: string; outcome: string; session: string }>
  turnsPerBranch: number | null
  outputBytesPerBranch: number | null
}

/** `subagent-dispatch` 証拠から扇出の投影を取り出す。 */
export function collectSubagentDispatches(evidence: readonly EvidenceDetail[]): SubagentDispatch[] {
  return evidence
    .filter((item) => item.evidence_type === 'subagent-dispatch')
    .map((item) => {
      const meta = item.metadata
      const rawBranches = Array.isArray(meta.branches) ? meta.branches : []
      return {
        evidenceRef: item.evidence_ref,
        objective: typeof meta.objective === 'string' ? meta.objective : '',
        branches: rawBranches.flatMap((branch) => {
          if (typeof branch !== 'object' || branch === null) return []
          const record = branch as Record<string, unknown>
          return [{
            key: String(record.key ?? ''),
            outcome: String(record.outcome ?? ''),
            session: String(record.session ?? ''),
          }]
        }),
        turnsPerBranch: typeof meta.turns_per_branch === 'number' ? meta.turns_per_branch : null,
        outputBytesPerBranch:
          typeof meta.output_bytes_per_branch === 'number' ? meta.output_bytes_per_branch : null,
      }
    })
}

/** 扇出で「実際には調べられていない面」があるかを判定する。 */
export function hasIncompleteBranch(dispatches: readonly SubagentDispatch[]): boolean {
  return dispatches.some((item) => item.branches.some((branch) => branch.outcome !== 'COMPLETED'))
}

/** 一回の扇出を、各面が実際に調べられたかどうかが読める形で描画する。
 *
 *  扇出の固有の危うさは見た目ではなく**部分的な網羅を全面的な確認と読んでしまう**こと。
 *  一路が FAILED/TIMED_OUT でも要約は普通に返るため、失敗した面は結論の根拠から抜けている。
 *  そこで未完了が一つでもあれば節の見出し自体を警告色にし、件数を出す。
 */
function SubagentDispatchSection({ dispatches }: { dispatches: readonly SubagentDispatch[] }) {
  const messages = useMessages()
  const incomplete = dispatches.flatMap((item) =>
    item.branches.filter((branch) => branch.outcome !== 'COMPLETED'),
  )
  return (
    <section
      className={`resultSection subagentSection${incomplete.length > 0 ? ' subagentIncomplete' : ''}`}
    >
      <div className="subsectionHeader">
        <h3>{messages.runResult.subagentTitle}</h3>
        <span>{messages.runResult.subagentBranchCount(
          dispatches.reduce((total, item) => total + item.branches.length, 0),
        )}</span>
      </div>
      {incomplete.length > 0 && (
        <p className="error" role="alert">
          {messages.runResult.subagentIncompleteWarning(incomplete.map((item) => item.key))}
        </p>
      )}
      {dispatches.map((dispatch) => (
        <div className="subagentDispatch" key={dispatch.evidenceRef}>
          {dispatch.objective && <p className="hint">{dispatch.objective}</p>}
          <ul className="subagentBranches">
            {dispatch.branches.map((branch) => (
              <li key={`${dispatch.evidenceRef}:${branch.key}`}>
                <span className={`statusBadge branch-${branch.outcome.toLowerCase()}`}>
                  {messages.enums.subagentOutcome[branch.outcome] ?? branch.outcome}
                </span>
                <strong>{branch.key}</strong>
                {/* 会話 ID は全文だと行を潰すだけ。追跡用に title へ退避する。 */}
                <code className="mono" title={branch.session}>{shortId(branch.session)}</code>
              </li>
            ))}
          </ul>
          {dispatch.turnsPerBranch !== null && (
            <p className="hint">
              {messages.runResult.subagentBudget(
                dispatch.turnsPerBranch,
                dispatch.outputBytesPerBranch ?? 0,
              )}
            </p>
          )}
        </div>
      ))}
    </section>
  )
}


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

/** 未応答の質問と未決の変更提案を結果より上へ集約する。
 *
 *  Run が回答・承認待ちで停止している時、Workspace は自動でこの画面へ切り替える。
 *  利用者は「何をすれば続くのか」を探しに来るので、監査記録の下に置いてはいけない。 */
function PendingActionsSection({ detail, interactions, proposals, csrfToken, onInteractionResponded, onDecided }: {
  detail: RunDetailRecord
  interactions: UserInteractionDetail[]
  proposals: ChangeProposalRecord[]
  csrfToken: string
  onInteractionResponded?: (response: RespondedInteractionRecord) => void
  onDecided?: () => void
}) {
  const messages = useMessages()
  if (interactions.length === 0 && proposals.length === 0) return null
  return (
    <section className="resultSection pendingActions">
      <div className="subsectionHeader">
        <h3>{messages.runResult.pendingTitle}</h3>
        <span>{interactions.length + proposals.length}</span>
      </div>
      <p className="hint">{messages.runResult.pendingHint}</p>
      {interactions.map((interaction) => (
        <InteractionCard
          csrfToken={csrfToken}
          detail={detail}
          interaction={interaction}
          key={interaction.interaction_id}
          onResponded={onInteractionResponded}
        />
      ))}
      {proposals.map((proposal) => (
        <ChangeProposalCard
          csrfToken={csrfToken}
          detail={detail}
          key={proposal.proposal_id}
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
function ControlledEffectsSection({ detail, proposals, csrfToken, onDecided }: {
  detail: RunDetailRecord
  proposals: ChangeProposalRecord[]
  csrfToken: string
  onDecided?: () => void
}) {
  const messages = useMessages()
  if (
    proposals.length === 0
    && detail.approvals.length === 0
    && detail.effect_executions.length === 0
  ) return null
  return (
    <CollapsibleSection count={proposals.length} title={messages.runResult.controlledEffects}>
      <div className="proposalList">
        {proposals.map((proposal) => (
          <ChangeProposalCard
            csrfToken={csrfToken}
            detail={detail}
            key={proposal.proposal_id}
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
                  <strong>{effect.provider} · {messages.enums.effectStatus[effect.status] ?? effect.status}</strong>
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

/** Exact version/checksum を表示したまま一度だけ approve/reject request を送る。 */
function ChangeProposalCard({ detail, proposal, csrfToken, onDecided }: {
  detail: RunDetailRecord
  proposal: ChangeProposalRecord
  csrfToken: string
  onDecided?: () => void
}) {
  const messages = useMessages()
  const [reason, setReason] = useState(messages.runResult.defaultApprovalReason)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])

  /** 表示中 Proposal の version/checksum と decision を idempotent に送る。 */
  async function submitDecision(decision: 'APPROVED' | 'REJECTED'): Promise<void> {
    controller.current?.abort()
    const requestController = new AbortController()
    controller.current = requestController
    setSubmitting(true)
    setError(null)
    try {
      await decideChangeProposal(
        detail.project_id,
        detail.run_id,
        proposal,
        decision,
        reason.trim(),
        createIdempotencyKey(),
        csrfToken,
        requestController.signal,
      )
      onDecided?.()
    } catch (caught: unknown) {
      if (!requestController.signal.aborted) {
        setError(caught instanceof Error ? caught.message : 'Unknown Proposal API error')
      }
    } finally {
      if (!requestController.signal.aborted) setSubmitting(false)
    }
  }

/** 提案 1 件の変更内容を描画する。承認画面は唯一の闸門なので、読めることが安全性そのもの。 */
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
    return <pre>{JSON.stringify({ changes, precondition }, null, 2)}</pre>
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
            ? <pre className="proposalFileBody">{file.content}</pre>
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

  const canDecide = proposal.status === 'PENDING_APPROVAL'
    && new Date(proposal.expires_at).getTime() > Date.now()
  return (
    <article className={`proposalCard proposal-${proposal.status.toLowerCase()}`}>
      <div className="segmentHeading">
        <strong>{proposal.proposal_ref} · {proposal.operation}</strong>
        <span>{messages.enums.proposalStatus[proposal.status] ?? proposal.status} · {messages.enums.riskLevel[proposal.risk_level] ?? proposal.risk_level}</span>
      </div>
      <h4>{proposal.summary}</h4>
      <dl className="proposalFacts">
        <div><dt>{messages.runResult.targetLabel}</dt><dd>{displayText(proposal.target.display, displayText(proposal.target.locator, '—'))}</dd></div>
        <div><dt>{messages.runResult.capabilityLabel}</dt><dd>{proposal.capability_version}</dd></div>
        <div><dt>{messages.runResult.versionLabel}</dt><dd>{proposal.version}</dd></div>
        <div><dt>{messages.runResult.expiresLabel}</dt><dd>{formatLocalTimestamp(proposal.expires_at)}</dd></div>
      </dl>
      <ProposalChanges changes={proposal.changes} precondition={proposal.precondition} />
      <p className="hint">{messages.runResult.evidenceLine(proposal.evidence_refs, proposal.checksum)}</p>
      {canDecide && (
        <div className="proposalDecision">
          <label>{messages.runResult.approvalReason}
            <textarea
              maxLength={1000}
              required
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <div className="formRow">
            <button
              className="primaryButton"
              disabled={submitting || !reason.trim()}
              onClick={() => void submitDecision('APPROVED')}
              type="button"
            >{messages.runResult.approveAndApply}</button>
            <button
              className="dangerButton"
              disabled={submitting || !reason.trim()}
              onClick={() => void submitDecision('REJECTED')}
              type="button"
            >{messages.runResult.rejectAndContinue}</button>
          </div>
        </div>
      )}
      {proposal.status === 'PENDING_APPROVAL' && !canDecide && (
        <p className="error">{messages.runResult.proposalExpired}</p>
      )}
      {error && <p className="error" role="alert">{error}</p>}
    </article>
  )
}

/** 一つの主分析と、その下で併走した扇出の子分析。 */
export interface SessionLineage {
  primary: AgentSessionDetail
  branches: AgentSessionDetail[]
}

/** Segment 内の Session を主分析ごとにまとめ、子分析をその下へ畳む。
 *
 *  平坦に並べると「順に 5 回会話した」と読めてしまうが、実際は主分析 1 本と併走した子分析 4 本
 *  かもしれない。件数の意味が変わるので、主分析だけを数え、子はぶら下げる。
 *  親が見つからない子は**捨てずに**独立した行として残す——監査記録から黙って消える方が悪い。 */
export function groupSessionsByLineage(
  sessions: readonly AgentSessionDetail[],
): SessionLineage[] {
  const lineages: SessionLineage[] = []
  const byId = new Map<string, SessionLineage>()
  for (const session of sessions) {
    if (session.session_kind !== 'SUBAGENT') {
      const lineage: SessionLineage = { primary: session, branches: [] }
      lineages.push(lineage)
      byId.set(session.agent_session_id, lineage)
    }
  }
  for (const session of sessions) {
    if (session.session_kind !== 'SUBAGENT') continue
    const parent = session.parent_session_id === null ? undefined : byId.get(session.parent_session_id)
    if (parent === undefined) {
      lineages.push({ primary: session, branches: [] })
      continue
    }
    parent.branches.push(session)
  }
  return lineages
}

/** 子分析が結論に寄与できたか。CLOSED 以外は途中で終わっている。 */
function isSettledBranch(session: AgentSessionDetail): boolean {
  return session.status === 'CLOSED'
}

/** 主分析 1 行と、その下の子分析行を描画する。
 *
 *  P3a と同じ理由で、完走しなかった子分析は完走したものと**視覚的に区別**する。扇出は一路が
 *  落ちても要約が普通に返るため、同じ見た目で並べると「その面も見た」と読まれる。 */
function SessionLineageLines({ lineage }: { lineage: SessionLineage }) {
  const messages = useMessages()
  const { primary, branches } = lineage
  return (
    <>
      <p className="sessionLine">
        {messages.runResult.sessionLabel} {shortId(primary.agent_session_id)} · {messages.enums.sessionStatus[primary.status] ?? primary.status} · {primary.model}
        {primary.parent_session_id ? ` · ${messages.runResult.sessionParentPrefix}${shortId(primary.parent_session_id)}` : ''}
      </p>
      {branches.map((branch) => (
        <p
          className={`sessionLine sessionBranchLine${isSettledBranch(branch) ? '' : ' sessionBranchIncomplete'}`}
          key={branch.agent_session_id}
        >
          {messages.enums.sessionKind[branch.session_kind] ?? branch.session_kind} {shortId(branch.agent_session_id)} · {messages.enums.sessionStatus[branch.status] ?? branch.status}
          {typeof branch.usage.branch_key === 'string' ? ` · ${branch.usage.branch_key}` : ''}
        </p>
      ))}
    </>
  )
}

/** Segment、Attempt、Session lineage と決着済み Interaction を記録として表示する。
 *
 *  未応答の質問は上部の「対応が必要」へ移してあるため、ここには回答済み・期限切れだけが並ぶ。 */
function RunConversationAudit({ detail, interactions, csrfToken, onInteractionResponded }: {
  detail: RunDetailRecord
  interactions: UserInteractionDetail[]
  csrfToken: string
  onInteractionResponded?: (response: RespondedInteractionRecord) => void
}) {
  const messages = useMessages()
  return (
    <>
      <ol className="segmentTimeline">
        {detail.segments.map((segment) => {
          const attempts = detail.attempts.filter((attempt) => attempt.run_segment_id === segment.run_segment_id)
          const lineages = groupSessionsByLineage(
            detail.sessions.filter((session) => session.run_segment_id === segment.run_segment_id),
          )
          return (
            <li key={segment.run_segment_id ?? `legacy-${segment.segment_no}`}>
              <div className="segmentHeading">
                <strong>{messages.runResult.segmentTitle(segment.segment_no)}</strong>
                <span>{messages.enums.segmentTrigger[segment.trigger_type] ?? segment.trigger_type} · {messages.enums.sessionStatus[segment.status] ?? segment.status}</span>
              </div>
              <p>{displayText(segment.objective.text, messages.runResult.segmentObjectiveFallback)}</p>
              <div className="segmentRelations">
                <span>{messages.runResult.attemptsCount(attempts.length)}</span>
                <span>{messages.runResult.sessionsCount(lineages.length)}</span>
                <span>{messages.enums.continuationMode[segment.continuation_mode] ?? segment.continuation_mode}</span>
              </div>
              {lineages.map((lineage) => (
                <SessionLineageLines key={lineage.primary.agent_session_id} lineage={lineage} />
              ))}
            </li>
          )
        })}
      </ol>
      {interactions.length > 0 && (
        <div className="interactionList">
          {interactions.map((interaction) => (
            <InteractionCard
              csrfToken={csrfToken}
              detail={detail}
              interaction={interaction}
              key={interaction.interaction_id}
              onResponded={onInteractionResponded}
            />
          ))}
        </div>
      )}
    </>
  )
}

/** OPEN Interaction を型別 form で一度だけ回答し、受理 snapshot を親へ通知する。 */
function InteractionCard({ detail, interaction, csrfToken, onResponded }: {
  detail: RunDetailRecord
  interaction: UserInteractionDetail
  csrfToken: string
  onResponded?: (response: RespondedInteractionRecord) => void
}) {
  const messages = useMessages()
  const [text, setText] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])

  /** 表示中 interaction の version と選択肢を固定して response を送る。 */
  async function handleRespond(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    controller.current?.abort()
    const requestController = new AbortController()
    controller.current = requestController
    setSubmitting(true)
    setError(null)
    const answer = interaction.interaction_type === 'CHOICE'
      ? { selected_option_keys: selected, ...(text.trim() ? { text: text.trim() } : {}) }
      : { text: text.trim() }
    try {
      const response = await respondToInteraction(
        detail.project_id,
        detail.run_id,
        interaction.interaction_id,
        interaction.version,
        answer,
        createIdempotencyKey(),
        csrfToken,
        requestController.signal,
      )
      onResponded?.(response)
    } catch (caught: unknown) {
      if (!requestController.signal.aborted) {
        setError(caught instanceof Error ? caught.message : 'Unknown Interaction API error')
      }
    } finally {
      if (!requestController.signal.aborted) setSubmitting(false)
    }
  }

  const allowMultiple = interaction.prompt.allow_multiple === true
  const isOpen = interaction.status === 'OPEN'
  return (
    <article className={`interactionCard interaction-${interaction.status.toLowerCase()}`}>
      <div className="segmentHeading">
        <strong>{messages.enums.interactionType[interaction.interaction_type] ?? interaction.interaction_type}</strong>
        <span>{messages.enums.interactionStatus[interaction.status] ?? interaction.status} · v{interaction.version}</span>
      </div>
      <h4>{displayText(interaction.prompt.prompt, messages.runResult.interactionNeedInput)}</h4>
      <p>{displayText(interaction.prompt.rationale, '')}</p>
      {displayText(interaction.prompt.impact, '') && <p className="hint">{messages.runResult.impactLine(displayText(interaction.prompt.impact, ''))}</p>}
      <p className="hint">{messages.runResult.deadlineLine(formatLocalTimestamp(interaction.expires_at), messages.enums.continuationMode[interaction.continuation_mode] ?? interaction.continuation_mode)}</p>
      {interaction.response && <p className="interactionAnswer">{messages.runResult.answeredLine(displayResponse(interaction.response.response))}</p>}
      {isOpen && interaction.interaction_type === 'EFFECT_APPROVAL' && (
        <p className="hint">{messages.runResult.effectApprovalHint}</p>
      )}
      {isOpen && interaction.interaction_type !== 'EFFECT_APPROVAL' && (
        <form className="interactionForm" onSubmit={(event) => void handleRespond(event)}>
          {interaction.interaction_type === 'CHOICE' && interaction.options.map((option) => {
            const key = displayText(option.key, '')
            return (
              <label className="interactionOption" key={key}>
                <input
                  checked={selected.includes(key)}
                  name={`interaction-${interaction.interaction_id}`}
                  onChange={(event) => setSelected((current) => event.target.checked
                    ? (allowMultiple ? [...current, key] : [key])
                    : current.filter((item) => item !== key))}
                  type={allowMultiple ? 'checkbox' : 'radio'}
                  value={key}
                />
                <span>
                  <strong>{displayText(option.label, key)}{option.recommended === true ? messages.runResult.recommendedSuffix : ''}</strong>
                  {displayText(option.description, '')}
                </span>
              </label>
            )
          })}
          {interaction.interaction_type !== 'CHOICE' && (
            <label>{messages.runResult.yourAnswer}<textarea maxLength={10000} required value={text} onChange={(event) => setText(event.target.value)} /></label>
          )}
          {interaction.interaction_type === 'CHOICE' && (
            <label>{messages.runResult.choiceNote}<textarea maxLength={10000} value={text} onChange={(event) => setText(event.target.value)} /></label>
          )}
          <button className="primaryButton" disabled={submitting || (interaction.interaction_type === 'CHOICE' && selected.length === 0)} type="submit">
            {submitting ? messages.runResult.submitting : messages.runResult.submitAndContinue}
          </button>
          {error && <p className="error" role="alert">{error}</p>}
        </form>
      )}
    </article>
  )
}

/** Unknown 公開値を安全な表示文字列へ絞る。 */
function displayText(value: unknown, fallback: string): string {
  return typeof value === 'string' && value ? value : fallback
}

/** UUID 全文を重ねず timeline 用短縮表示を返す。 */
function shortId(value: string): string {
  return value.slice(0, 8)
}

/** 保存済み InteractionResponse を公開 field だけで簡潔表示する。 */
function displayResponse(value: Record<string, unknown>): string {
  const selected = stringItems(value.selected_option_keys)
  const text = displayText(value.text, '')
  return [selected.join(', '), text].filter(Boolean).join(' · ') || '—'
}

/** 通用 OutcomeEnvelope を task-specific business field に依存せず標準表示する。 */
function OutcomeEnvelopeResult({ data, schema, showTechnicalDetails }: {
  data: Record<string, unknown>
  schema: Record<string, unknown> | null
  showTechnicalDetails: boolean
}) {
  const messages = useMessages()
  const deliverables = recordItems(data.deliverables)
  const findings = recordItems(data.findings)
  const questions = recordItems(data.open_questions)
  const limitations = stringItems(data.limitations)
  const proposalRefs = stringItems(data.change_proposal_refs)
  const effects = recordItems(data.effects)
  const schemaProperties = isPlainRecord(schema?.properties) ? schema.properties : {}
  const structuredSchema = isPlainRecord(schemaProperties.structured_data)
    ? schemaProperties.structured_data
    : null
  return (
    <div className="outcomeEnvelope">
      <div className="outcomeStatus"><strong>{textValue(data.status, messages.runResult.outcomeUnknown)}</strong>{showTechnicalDetails && <span>{textValue(data.outcome_version, '—')}</span>}</div>
      <OutcomeCollection title={messages.runResult.deliverables} empty={messages.runResult.noDeliverables}>
        {deliverables.map((item, index) => (
          <article className="outcomeCard" key={textValue(item.key, `deliverable-${index}`)}>
            <div><strong>{textValue(item.title, textValue(item.key, messages.runResult.untitledLabel))}</strong>{showTechnicalDetails && <span>{textValue(item.kind, '—')}</span>}</div>
            {typeof item.description === 'string' && <p>{item.description}</p>}
            {typeof item.content === 'string' && <pre>{item.content}</pre>}
            {showTechnicalDetails && typeof item.artifact_ref === 'string' && <code>{item.artifact_ref}</code>}
          </article>
        ))}
      </OutcomeCollection>
      <OutcomeCollection title={messages.runResult.findingsTitle} empty={messages.runResult.noFindings}>
        {findings.map((item, index) => (
          <article className="outcomeCard" key={textValue(item.key, `finding-${index}`)}>
            <div><strong>{textValue(item.title, textValue(item.key, messages.runResult.untitledLabel))}</strong>{typeof item.severity === 'string' && <span>{item.severity}</span>}</div>
            <p>{textValue(item.detail, '—')}</p>
            {renderOutcomeRefs(stringItems(item.evidence_refs), showTechnicalDetails, messages.runResult.evidenceTitle)}
          </article>
        ))}
      </OutcomeCollection>
      {(questions.length > 0 || limitations.length > 0) && (
        <div className="outcomeColumns">
          <OutcomeCollection title={messages.runResult.openQuestions} empty={messages.runResult.noOpenQuestions}>
            {questions.map((item, index) => <p key={textValue(item.key, `question-${index}`)}>{textValue(item.question, '—')}</p>)}
          </OutcomeCollection>
          <OutcomeCollection title={messages.runResult.limitations} empty={messages.runResult.noLimitations}>
            {limitations.map((item) => <p key={item}>{item}</p>)}
          </OutcomeCollection>
        </div>
      )}
      {isPlainRecord(data.structured_data) && (
        <section className="outcomeGroup">
          <details className="technicalResultDetails">
            <summary>{messages.runResult.businessStructured}</summary>
            <SchemaResultValue schema={structuredSchema} value={data.structured_data} path="$/structured_data" showTechnicalDetails={showTechnicalDetails} />
          </details>
        </section>
      )}
      {(proposalRefs.length > 0 || effects.length > 0) && (
        <section className="outcomeGroup">
          <h4>{messages.runResult.changesAndEffects}</h4>
          {renderOutcomeRefs(proposalRefs, showTechnicalDetails, messages.runResult.evidenceTitle)}
          {effects.map((effect, index) => (
            <p key={`${textValue(effect.proposal_ref, 'effect')}-${index}`}>
              <strong>{messages.enums.effectStatus[textValue(effect.status, '')] ?? textValue(effect.status, messages.runResult.outcomeUnknown)}</strong> · {textValue(effect.summary, '—')}
            </p>
          ))}
        </section>
      )}
    </div>
  )
}

/** Outcome の一群へ共通見出しと空状態を付与し、長い一覧の尾部を畳む。 */
function OutcomeCollection({ title, empty, children }: {
  title: string
  empty: string
  children: ReactNode
}) {
  const items = Array.isArray(children) ? (children as ReactNode[]) : [children]
  const { visible, hidden } = splitOverflow(items)
  return (
    <section className="outcomeGroup">
      {/* 畳んだ後も総数が読めるよう、見出しに件数を残す。 */}
      <h4>{title}{items.length > 0 && <span className="eventCount">{items.length}</span>}</h4>
      {items.length === 0 ? <p className="compactEmpty">{empty}</p> : <>
        {visible}
        <OverflowFold count={hidden.length}>{hidden}</OverflowFold>
      </>}
    </section>
  )
}

/** 一覧の尾部を「残り N 件」として畳む。畳む対象が無ければ何も描画しない。
 *
 *  `<details>` を使うのは CollapsibleSection と同じ理由で、閉じていても子が DOM に残り、
 *  頁面測試(renderToStaticMarkup)の内容断言を壊さないため。 */
function OverflowFold({ count, children }: { count: number; children: ReactNode }) {
  const messages = useMessages()
  if (count === 0) return null
  return (
    <details className="resultOverflow">
      <summary>{messages.runResult.showRemaining(count)}</summary>
      <div className="resultOverflowBody">{children}</div>
    </details>
  )
}

/** Unknown 値から object item だけを抽出する。 */
function recordItems(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter(isPlainRecord) : []
}

/** Unknown 値から string item だけを抽出する。 */
function stringItems(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

/** Evidence/proposal reference は既定で件数だけ示し、全文は技術項目の切替後に表示する。 */
function renderOutcomeRefs(refs: string[], showTechnicalDetails: boolean, label: string): ReactNode {
  if (refs.length === 0) return null
  if (!showTechnicalDetails) return <span className="outcomeRefSummary">{label} · {refs.length}</span>
  return <div className="outcomeRefs">{refs.map((reference) => <code key={reference}>{reference}</code>)}</div>
}

/** Unknown 値を表示用 string へ絞り、欠損時は既定値を返す。 */
function textValue(value: unknown, fallback: string): string {
  return typeof value === 'string' && value ? value : fallback
}

/** Evaluation 履歴の非同期取得状態。 */
type EvaluationState =
  | { status: 'loading' }
  | { status: 'ready'; items: EvaluationRecord[] }
  | { status: 'error'; message: string }

/** Result の人工評価履歴と追加式入力 form を表示する。 */
function EvaluationSection({ detail, csrfToken }: { detail: RunDetailRecord; csrfToken: string }) {
  const messages = useMessages()
  const [state, setState] = useState<EvaluationState>({ status: 'loading' })
  const [rating, setRating] = useState(3)
  const [verdict, setVerdict] = useState<CreateEvaluationInput['verdict']>('uncertain')
  const [comment, setComment] = useState('')
  const [pointer, setPointer] = useState('')
  const [suggestedValue, setSuggestedValue] = useState('')
  const [reason, setReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const submitController = useRef<AbortController | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setState({ status: 'loading' })
    void loadEvaluations(detail.project_id, detail.run_id, controller.signal)
      .then((items) => setState({ status: 'ready', items }))
      .catch((caught: unknown) => {
        if (!controller.signal.aborted) {
          setState({
            status: 'error',
            message: caught instanceof Error ? caught.message : 'Unknown Evaluation API error',
          })
        }
      })
    return () => controller.abort()
  }, [detail.project_id, detail.run_id])

  useEffect(() => () => submitController.current?.abort(), [])

  /** Form 値を一件の追加式 Evaluation として保存する。 */
  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    submitController.current?.abort()
    const controller = new AbortController()
    submitController.current = controller
    setSubmitting(true)
    setSubmitError(null)
    const revisions = pointer.trim()
      ? [{
          pointer: pointer.trim(),
          suggested_value: parseSuggestedValue(suggestedValue),
          reason: reason.trim(),
        }]
      : []
    try {
      const saved = await createEvaluation(
        detail.project_id,
        detail.run_id,
        { rating, verdict, comment: comment.trim(), revisions },
        csrfToken,
        controller.signal,
      )
      setState((current) => current.status === 'ready'
        ? { status: 'ready', items: [...current.items, saved] }
        : { status: 'ready', items: [saved] })
      setComment('')
      setPointer('')
      setSuggestedValue('')
      setReason('')
    } catch (caught: unknown) {
      if (!controller.signal.aborted) {
        setSubmitError(caught instanceof Error ? caught.message : 'Unknown Evaluation API error')
      }
    } finally {
      if (!controller.signal.aborted) setSubmitting(false)
    }
  }

  return (
    <CollapsibleSection
      count={state.status === 'ready' ? state.items.length : undefined}
      title={messages.runResult.manualEvaluation}
    >
      {state.status === 'loading' && <p className="compactEmpty">{messages.runResult.loadingEvaluations}</p>}
      {state.status === 'error' && <p className="error" role="alert">{state.message}</p>}
      {state.status === 'ready' && state.items.length === 0 && <p className="compactEmpty">{messages.runResult.noEvaluations}</p>}
      {state.status === 'ready' && state.items.length > 0 && (
        <ol className="evaluationList">{state.items.map((item) => <EvaluationItem item={item} key={item.evaluation_id} />)}</ol>
      )}
      <form className="evaluationForm" onSubmit={(event) => void handleSubmit(event)}>
        <div className="formRow">
          <label>{messages.runResult.ratingLabel}<select value={rating} onChange={(event) => setRating(Number(event.target.value))}>{[1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
          <label>{messages.runResult.verdictLabel}<select value={verdict} onChange={(event) => setVerdict(event.target.value as CreateEvaluationInput['verdict'])}><option value="accurate">{messages.runResult.verdictAccurate}</option><option value="partially_accurate">{messages.runResult.verdictPartial}</option><option value="inaccurate">{messages.runResult.verdictInaccurate}</option><option value="uncertain">{messages.runResult.verdictUncertain}</option></select></label>
        </div>
        <label>{messages.runResult.commentLabel}<textarea maxLength={4000} value={comment} onChange={(event) => setComment(event.target.value)} /></label>
        <details className="revisionEditor">
          <summary>{messages.runResult.addRevision}</summary>
          <label>{messages.runResult.jsonPointerLabel}<input maxLength={512} pattern="^/.*" placeholder="/items/0/value" value={pointer} onChange={(event) => setPointer(event.target.value)} /></label>
          <label>{messages.runResult.suggestedValueLabel}<input disabled={!pointer.trim()} value={suggestedValue} onChange={(event) => setSuggestedValue(event.target.value)} /></label>
          <label>{messages.runResult.revisionReasonLabel}<input disabled={!pointer.trim()} maxLength={1000} required={Boolean(pointer.trim())} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
        </details>
        <button className="secondaryButton" disabled={submitting} type="submit">{submitting ? messages.runResult.saving : messages.runResult.addEvaluation}</button>
        {submitError && <p className="error" role="alert">{submitError}</p>}
      </form>
    </CollapsibleSection>
  )
}

/** 一件の Evaluation と AI 原値・人工提案値の並列 revision を表示する。 */
function EvaluationItem({ item }: { item: EvaluationRecord }) {
  const messages = useMessages()
  return (
    <li className="evaluationItem">
      <div><strong>{item.rating}/5 · {messages.enums.verdict[item.verdict] ?? item.verdict}</strong><time>{formatLocalTimestamp(item.created_at)}</time></div>
      {item.comment && <p>{item.comment}</p>}
      {item.revisions.map((revision) => (
        <div className="evaluationRevision" key={revision.pointer}>
          <code>{revision.pointer}</code>
          <div><p><span>{messages.runResult.aiOriginal}</span>{displayJsonValue(revision.original_value)}</p><p><span>{messages.runResult.humanSuggestion}</span>{displayJsonValue(revision.suggested_value)}</p></div>
          <small>{revision.reason}</small>
        </div>
      ))}
    </li>
  )
}

/** Text input を JSON として解釈できる場合だけ型付き suggested value にする。 */
function parseSuggestedValue(value: string): unknown {
  const trimmed = value.trim()
  if (!trimmed) return ''
  try {
    return JSON.parse(trimmed) as unknown
  } catch {
    return trimmed
  }
}

/** 任意 JSON value を比較表示用の短い text へ変換する。 */
function displayJsonValue(value: unknown): string {
  if (typeof value === 'string') return value || '""'
  return JSON.stringify(value) ?? '—'
}

/** Frozen output Schema に沿って object、array、scalar を同じ再帰 renderer で表示する。
 *
 * 業務データの形は Skill ごとに違い、深さも値の長さも事前に決まらない。Run 事実行のような
 * 固定列の card 表に載せると、入れ子のたびに列が細って値が切れるため、ここは専用の
 * 「複合値は全幅・scalar は並列・値は折り返し」規則で描く。
 */
function SchemaResultValue({ schema, value, path, showTechnicalDetails = false }: {
  schema: Record<string, unknown> | null
  value: unknown
  path: string
  showTechnicalDetails?: boolean
}) {
  const messages = useMessages()
  if (Array.isArray(value)) {
    if (value.length === 0) return <p className="compactEmpty">{messages.runResult.emptyArray}</p>
    const itemSchema = isPlainRecord(schema?.items) ? schema.items : null
    // Evidence 参照のような scalar だけの配列は、番号付き card にすると余白ばかりになる。
    if (value.every((item) => !isPlainRecord(item) && !Array.isArray(item))) {
      return (
        <div className="schemaScalarList">
          {value.map((item, index) => <code key={`${path}/${index}`}>{displayJsonValue(item)}</code>)}
        </div>
      )
    }
    // 配列は件数がそのまま縦の長さになる。番号は要素側が持つので、切っても連番はずれない。
    const { visible, hidden } = splitOverflow(value.map((item, index) => (
      <li key={`${path}/${index}`}>
        <span className="schemaItemIndex">{index + 1}</span>
        <div className="schemaItemBody">
          <SchemaResultValue schema={itemSchema} value={item} path={`${path}/${index}`} showTechnicalDetails={showTechnicalDetails} />
        </div>
      </li>
    )))
    return (
      <>
        <ol className="schemaItemList">{visible}</ol>
        <OverflowFold count={hidden.length}>
          <ol className="schemaItemList">{hidden}</ol>
        </OverflowFold>
      </>
    )
  }
  if (isPlainRecord(value)) {
    const properties = isPlainRecord(schema?.properties) ? schema.properties : {}
    return (
      <dl className="schemaFacts">
        {Object.entries(value).map(([key, nested]) => {
          const label = schemaFieldLabel(properties[key], key)
          // 配列・object は中身が縦に伸びるため常に全幅を取り、scalar だけを並列に置く。
          const wide = Array.isArray(nested) || isPlainRecord(nested)
          return (
            <div className={wide ? 'schemaFactWide' : undefined} key={`${path}/${key}`}>
              <dt>
                <span>{label}</span>
                {showTechnicalDetails && <code className="mono">{key}</code>}
              </dt>
              <dd><SchemaResultValue schema={isPlainRecord(properties[key]) ? properties[key] : null} value={nested} path={`${path}/${key}`} showTechnicalDetails={showTechnicalDetails} /></dd>
            </div>
          )
        })}
      </dl>
    )
  }
  if (typeof value === 'string' && (path.endsWith('/source_ref') || path.includes('/evidence_refs/'))) {
    return <code>{value}</code>
  }
  return <span>{displayJsonValue(value)}</span>
}

/** Schema の title/description を表示ラベルにし、無い場合だけ field key を使う。
 *
 * title/description は Skill 原文の言語で生成されるため、これを主表示にすると画面の
 * 言語混在が減る。key と連結すると英語 key と説明文が同じ行に並んで読めなくなるので、
 * ラベルと key は呼び出し側で別要素として描く。
 */
function schemaFieldLabel(value: unknown, fallback: string): string {
  if (!isPlainRecord(value)) return fallback
  if (typeof value.title === 'string' && value.title) return value.title
  if (typeof value.description === 'string' && value.description) return value.description
  return fallback
}

/** Array 以外の JSON object を型付きへ絞り込む。 */
function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Evidence locator を常時表示し、本文 excerpt は利用者操作まで折り畳む。 */
function EvidenceCard({ evidence }: { evidence: EvidenceDetail }) {
  const messages = useMessages()
  return (
    <details className="evidenceCard">
      <summary><span><strong>{evidence.evidence_ref}</strong><small>{evidence.evidence_type}</small></span><span>{messages.runResult.viewExcerpt}</span></summary>
      <dl><div><dt>{messages.runResult.evidenceSourceLabel}</dt><dd>{evidence.source_uri}</dd></div><div><dt>{messages.runResult.evidenceLocatorLabel}</dt><dd>{JSON.stringify(evidence.source_locator)}</dd></div><div><dt>{messages.runResult.evidenceHashLabel}</dt><dd>{evidence.content_hash}</dd></div></dl>
      <pre>{evidence.excerpt ?? messages.runResult.noExcerpt}</pre>
    </details>
  )
}

/** Optional confidence を百分率または未提供表示へ変換する。 */
function formatConfidence(value: number | null): string {
  return value === null ? '—' : `${Math.round(value * 100)}%`
}
