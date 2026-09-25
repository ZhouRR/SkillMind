import { useEffect, useState } from 'react'

import type { RespondedInteractionRecord, RunDetailRecord } from '../api'
import type { SessionEnded } from '../hooks/useResourceRequest'
import { useMessages } from '../i18n'
import type { InteractionAccessFailure } from '../lib/interactionResponse'
import { formatLocalTimestamp } from '../lib/presentation'
import { collectSubagentDispatches } from '../lib/runAudit'
import { observeRunPaint } from '../lib/runPerformance'
import { EffectReconciliation } from './EffectReconciliation'
import { RunEvaluations } from './EvaluationSection'
import { LazyAuditSection } from './LazyAuditSection'
import { EmptyState, ModalDialog } from './PageElements'
import { ReportPresentation } from './ReportPresentation'
import { ResultValidationScope } from './ResultValidationScope'
import { RunArtifacts } from './RunArtifacts'
import { EvidenceCard, RunConversationAudit, SubagentDispatchSection } from './RunAuditSections'
import { ControlledEffectsSection, isDecidable, PendingActionsSection } from './RunControlledEffects'
import { RunDocumentSnapshots } from './RunDocumentSnapshots'
import { RunDuration } from './RunDuration'
import { RunExecutionMetrics } from './RunExecutionMetrics'
import { RunInteractions } from './RunInteractions'
import { OutcomeEnvelopeResult, SchemaResultValue } from './RunOutcome'
import { RunSummaryReport } from './RunSummaryReport'

/** Run detail 非同期読み込みの排他的 UI state。 */
export type RunDetailState =
  | { status: 'idle' }
  | { status: 'loading'; detail?: RunDetailRecord }
  | { status: 'ready'; detail: RunDetailRecord }
  | { status: 'error'; message: string; detail?: RunDetailRecord; accessFailure?: InteractionAccessFailure }

/** 単独 read-only 表示には認証通知先が無い。実 Workspace は Session guard を渡す。 */
const ignoreSessionExpired: SessionEnded = () => {}

/** 普通答復の owner は loading/error と表示 tab に依存させない。 */
export function RunResultPanel({ state, csrfToken, actorId = '', projectId, runId, projectReadOnly = false, reportOnly = false,
  onInteractionResponded, onInteractionFacts, onProposalDecided, onSessionExpired = ignoreSessionExpired }: {
  reportOnly?: boolean
  state: RunDetailState
  csrfToken: string
  actorId?: string
  projectId?: string
  runId?: string
  projectReadOnly?: boolean
  onInteractionResponded?: (response: RespondedInteractionRecord) => void
  onInteractionFacts?: (detail: RunDetailRecord) => void
  onProposalDecided?: () => void
  onSessionExpired?: SessionEnded
}) {
  const detail = 'detail' in state ? state.detail : undefined
  useEffect(() => detail ? observeRunPaint(detail) : undefined, [detail])
  const scope = { actorId, projectId: projectId ?? detail?.project_id ?? '', runId: runId ?? detail?.run_id ?? '' }
  const owner = JSON.stringify([actorId, csrfToken, scope.projectId.toLowerCase(), scope.runId.toLowerCase()])
  const [evaluationOwner, setEvaluationOwner] = useState<string | null>(null)
  useEffect(() => setEvaluationOwner(null), [owner])
  return <>
    <RunInteractions key={JSON.stringify([actorId, csrfToken, scope.projectId.toLowerCase(), scope.runId.toLowerCase()])}
      scope={scope} state={state} csrfToken={csrfToken} pendingOnly={reportOnly} onResponded={onInteractionResponded}
      onFacts={onInteractionFacts} onSessionExpired={onSessionExpired} />
    <RunEvaluations key={JSON.stringify(['evaluations', actorId, csrfToken, scope.projectId.toLowerCase(), scope.runId.toLowerCase()])}
      open={evaluationOwner === owner} onClose={() => setEvaluationOwner(null)} onOpen={() => setEvaluationOwner(owner)}
      scope={scope} state={state} csrfToken={csrfToken} readOnly={projectReadOnly} onSessionExpired={onSessionExpired} />
    <RunResultContent key={`content:${owner}`} reportOnly={reportOnly} state={state} csrfToken={csrfToken} onProposalDecided={onProposalDecided}
      onEvaluate={() => setEvaluationOwner(owner)}
      artifactOwner={JSON.stringify([actorId, csrfToken, scope.projectId.toLowerCase(), scope.runId.toLowerCase()])}
      artifactScope={scope}
      onSessionExpired={onSessionExpired} />
  </>
}

/** 普通答復とは独立した Result、Proposal、Segment/ToolCall/Evidence 監査を表示する。 */
function RunResultContent({ reportOnly, state, csrfToken, onProposalDecided, artifactOwner, artifactScope, onSessionExpired, onEvaluate }: {
  reportOnly: boolean
  state: RunDetailState
  csrfToken: string
  onProposalDecided?: () => void
  artifactOwner: string
  artifactScope: { actorId: string; projectId: string; runId: string }
  onSessionExpired: SessionEnded
  onEvaluate: () => void
}) {
  const messages = useMessages()
  const [showTechnicalDetails, setShowTechnicalDetails] = useState(false)
  const [drawer, setDrawer] = useState<'details' | 'checks' | 'evidence' | null>(null)
  const [evidenceSelection, setEvidenceSelection] = useState<string[] | null>(null)
  const [artifactPreview, setArtifactPreview] = useState<{ ref: string; nonce: number } | null>(null)
  if (state.status === 'idle') {
    return <EmptyState text={messages.runResult.idleEmpty} />
  }
  if (state.status === 'loading') return <EmptyState text={messages.runResult.loadingEmpty} />
  if (state.status === 'error') return <p className="error" role="alert">{state.message}</p>
  const { detail } = state
  const result = detail.result
  // 「今あなたが動く必要があるもの」と「後から辿る記録」を分ける。前者は結果より上へ、
  // 後者は既定で畳む。同じ縦一列に全部広げると、待ち事項も結果も監査の中に埋もれる。
  const decidableProposals = detail.change_proposals.filter(isDecidable)
  const settledProposals = detail.change_proposals.filter((item) => !isDecidable(item))
  const versionId = detail.skill_snapshots[0]?.skill_version_id
  const dispatches = collectSubagentDispatches(detail.evidence)
  const latestAttempt = [...detail.attempts].sort((a, b) => b.created_at.localeCompare(a.created_at))[0]
  const capacityError = latestAttempt?.error?.code === 'model_capacity_unavailable'
    && (detail.status === 'RETRY_PENDING' || detail.status === 'FAILED')
  const retryAt = latestAttempt?.error?.retry_at

  return (
    <div className="resultView">
      {capacityError && <section className="resultSection" role={detail.status === 'FAILED' ? 'alert' : 'status'}>
        <strong>{messages.runResult.capacityTitle}</strong>
        <p>{detail.status === 'RETRY_PENDING' ? messages.runResult.capacityWaiting : messages.runResult.capacityFailed}</p>
        {detail.status === 'RETRY_PENDING' && typeof retryAt === 'string' && Number.isFinite(Date.parse(retryAt))
          && <p>{messages.runResult.capacityRetryAt(formatLocalTimestamp(retryAt))}</p>}
      </section>}

      {detail.effect_executions.some((effect) => effect.error?.code === 'effect_result_unknown') && (
        <section className="resultSection" role="status">
          <strong>{messages.runResult.effectResultUnknown}</strong>
          <p>{messages.runResult.effectReconciliationHint}</p>
          {detail.effect_executions.filter((effect) => effect.error?.code === 'effect_result_unknown').map((effect) => (
            <div key={effect.effect_execution_id}>
              <strong>{effect.provider}</strong>
              <EffectReconciliation scope={{ projectId: detail.project_id, runId: detail.run_id, effectId: effect.effect_execution_id }}
                actorId={artifactScope.actorId} csrfToken={csrfToken} onSessionExpired={onSessionExpired} />
            </div>
          ))}
        </section>
      )}
      <PendingActionsSection
        csrfToken={csrfToken}
        detail={detail}
        onDecided={onProposalDecided}
        proposals={decidableProposals}
      />
      {result && <RunSummaryReport detail={detail} showValidation={!reportOnly} />}
      <div className="resultActions" aria-label={messages.workspace.tabResult}>
        {!reportOnly && <RunDuration run={detail} />}
        {result && <button className="secondaryButton compactButton" type="button" onClick={onEvaluate}>{messages.runResult.manualEvaluation}</button>}
        {!reportOnly && <><button className="secondaryButton compactButton" type="button" onClick={() => { setEvidenceSelection(null); setDrawer('evidence') }}>{messages.runResult.evidenceTitle}<span className="eventCount">{detail.evidence.length}</span></button>
        {result && <button className="secondaryButton compactButton" type="button" onClick={() => setDrawer('checks')}>{messages.runResult.reading.checks}</button>}
        <button className="secondaryButton compactButton" type="button" onClick={() => setDrawer('details')}>{messages.runResult.reading.details}</button></>}
      </div>
      {/* 同じ toolbar から開く補助 drawer(実行情報・検証の範囲・証拠・人手評価)は同じ幅に揃える。 */}
      <ModalDialog drawer wide open={drawer === 'details'} title={messages.runResult.reading.details} onClose={() => setDrawer(null)}>
        <dl className="runDetailFacts">
          <div><dt>{messages.workspace.runIdLabel}</dt><dd><code>{detail.run_id}</code></dd></div>
          <div><dt>{messages.runResult.taskVersionLabel}</dt><dd><code>{versionId ?? '—'}</code></dd></div>
          {result && <>
            <div><dt>{messages.runResult.confidenceLabel}</dt><dd>{formatConfidence(result.confidence)}<small className="confidenceHint">{messages.runResult.reading.confidenceHint}</small></dd></div>
            <div><dt>{messages.runResult.structuredResult}</dt><dd>{result.result_kind === 'OUTCOME_ENVELOPE' ? messages.runResult.formatBadgeOutcome : (detail.output_schema_checksum ? messages.runResult.formatBadgeSchema : messages.runResult.formatBadgeLegacy)}</dd></div>
          </>}
        </dl>
        <button
          aria-pressed={showTechnicalDetails}
          className="secondaryButton compactButton"
          type="button"
          onClick={() => setShowTechnicalDetails((current) => !current)}
        >
          {showTechnicalDetails ? messages.runResult.hideTechnicalDetails : messages.runResult.technicalDetails}
        </button>
      </ModalDialog>
      <ModalDialog drawer wide open={drawer === 'checks'} title={messages.runResult.reading.checks} onClose={() => setDrawer(null)}>
        {result && <ResultValidationScope result={result} />}
      </ModalDialog>
      <ModalDialog drawer wide open={drawer === 'evidence'} title={messages.runResult.evidenceTitle} onClose={() => setDrawer(null)}>
        {evidenceSelection === null ? (detail.evidence.length === 0 ? <p className="compactEmpty">{messages.runResult.noEvidence}</p>
          : <div className="evidenceList">{detail.evidence.map((evidence) => <EvidenceCard snapshots={detail.document_snapshots} evidence={evidence} key={evidence.evidence_ref} />)}</div>)
          : <div className="evidenceList">{Array.from(new Set(evidenceSelection)).map((ref) => {
            const evidence = detail.evidence.find((item) => item.evidence_ref === ref)
            // 未解決参照は文字で残す。現在の資源や model URL から証拠を補完しない。
            return evidence ? <EvidenceCard snapshots={detail.document_snapshots} evidence={evidence} expanded key={`selected:${ref}`} />
              : <p key={ref}><code>{ref}</code> · {messages.runResult.noEvidence}</p>
          })}</div>}
      </ModalDialog>
      {result === null ? (
        <EmptyState text={messages.runResult.noValidatedResult(messages.enums.runStatus[detail.status] ?? detail.status)} />
      ) : <>
      {!reportOnly && <ResultValidationScope result={result} compact />}

      <div className="resultReportGrid">
        <section className="resultSection resultReportBody" aria-label={messages.runResult.genericOutcome}>
          <ReportPresentation key={result.result_id} value={result.data}>
          {result.result_kind === 'OUTCOME_ENVELOPE'
            ? <OutcomeEnvelopeResult data={result.data} schema={detail.output_schema} showTechnicalDetails={showTechnicalDetails} onArtifact={(ref) => setArtifactPreview((previous) => ({ ref, nonce: (previous?.nonce ?? 0) + 1 }))} onEvidence={(refs) => { setEvidenceSelection(refs); setDrawer('evidence') }} />
            : <SchemaResultValue schema={detail.output_schema} value={result.data} path="$" showTechnicalDetails={showTechnicalDetails} />}
          </ReportPresentation>
        </section>
      </div>
      </>}

      {/* 扇出は「結論の根拠がどこまで揃っているか」を左右するので備査へ畳まない。
          一路が失敗していても要約は普通に返るため、畳むと部分的な網羅を全面的な確認と読む。 */}
      {dispatches.length > 0 && <SubagentDispatchSection dispatches={dispatches} />}

      {!reportOnly && <RunDocumentSnapshots snapshots={detail.document_snapshots} />}
      {detail.project_id.toLowerCase() === artifactScope.projectId.toLowerCase()
        && detail.run_id.toLowerCase() === artifactScope.runId.toLowerCase()
        && <RunArtifacts key={artifactOwner} projectId={detail.project_id} runId={detail.run_id}
          result={result} evidence={detail.evidence} snapshots={detail.document_snapshots} requestedPreview={artifactPreview} onSessionExpired={onSessionExpired} />}
      <RunExecutionMetrics detail={detail} />

      {/* 以下は備査情報。既定で畳み、件数だけ見出しに残す。 */}
      {!reportOnly && <>
      <LazyAuditSection count={detail.tool_calls.length} title={messages.runResult.toolCalls}>
        {() => detail.tool_calls.length === 0 ? <p className="compactEmpty">{messages.runResult.noToolCalls}</p> : (
          <ul className="toolSummaryList">{detail.tool_calls.map((tool) => <li key={tool.tool_call_id}><div><strong>{tool.capability}</strong><span>{tool.status}</span></div><p>{tool.provider} · {tool.duration_ms === null ? '—' : `${tool.duration_ms} ms`}</p><code>{JSON.stringify(tool.arguments_summary)}</code></li>)}</ul>
        )}
      </LazyAuditSection>

      <LazyAuditSection count={detail.segments.length} title={messages.runResult.conversationAudit}>
        {() => <RunConversationAudit detail={detail} />}
      </LazyAuditSection>

      <ControlledEffectsSection
        csrfToken={csrfToken}
        detail={detail}
        onDecided={onProposalDecided}
        proposals={settledProposals}
      />

      {result !== null && (
        <LazyAuditSection title={messages.runResult.viewRawResult}>
          {() => <pre className="rawResultBody">{JSON.stringify(result.data, null, 2)}</pre>}
        </LazyAuditSection>
      )}

      </>}
    </div>
  )
}

/** Optional confidence を百分率または未提供表示へ変換する。 */
function formatConfidence(value: number | null): string {
  return value === null ? '—' : `${Math.round(value * 100)}%`
}
