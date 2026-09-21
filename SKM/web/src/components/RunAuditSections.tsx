import { useState } from 'react'

import type { AgentSessionDetail, EvidenceDetail, RunDetailRecord } from '../api'
import { useMessages } from '../i18n'
import { formatLocalTimestamp } from '../lib/presentation'
import { groupSessionsByLineage, type SessionLineage, type SubagentDispatch } from '../lib/runAudit'
import { displayText, evidenceTitle } from '../lib/resultPresentation'
import { MarkdownText } from './MarkdownText'

/** 一回の扇出を、各面が実際に調べられたかどうかが読める形で描画する。
 *
 *  扇出の固有の危うさは見た目ではなく**部分的な網羅を全面的な確認と読んでしまう**こと。
 *  一路が FAILED/TIMED_OUT でも要約は普通に返るため、失敗した面は結論の根拠から抜けている。
 *  そこで未完了が一つでもあれば節の見出し自体を警告色にし、件数を出す。
 */
export function SubagentDispatchSection({ dispatches }: { dispatches: readonly SubagentDispatch[] }) {
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

/** Segment、Attempt、Session lineage を記録として表示し、Interaction の owner は移さない。 */
export function RunConversationAudit({ detail }: {
  detail: RunDetailRecord
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
    </>
  )
}

/** UUID 全文を重ねず timeline 用短縮表示を返す。 */
function shortId(value: string): string {
  return value.slice(0, 8)
}

/** 可読名を先に示し、原 locator と hash は独立した明細として保持する。 */
export function EvidenceCard({ evidence, snapshots, expanded = false }: { evidence: EvidenceDetail; snapshots: RunDetailRecord['document_snapshots']; expanded?: boolean }) {
  const messages = useMessages()
  const [opened, setOpened] = useState(expanded)
  const [source, setSource] = useState(false)
  return (
    <details className="evidenceCard" open={opened} onToggle={(event) => setOpened(event.currentTarget.open)}>
      <summary><span><strong>{evidenceTitle(evidence, snapshots)}</strong><small>{messages.runResult.evidenceTypes[evidence.evidence_type] ?? evidence.evidence_type} · {formatLocalTimestamp(evidence.created_at)}{typeof evidence.source_locator.phase === 'string' && messages.runResult.evidencePhases[evidence.source_locator.phase] ? ` · ${messages.runResult.evidencePhases[evidence.source_locator.phase]}` : ''}</small></span><span>{evidence.excerpt ? messages.runResult.viewExcerpt : messages.runResult.viewLocator}</span></summary>
      {opened && <details className="evidenceMetadata" open={!evidence.excerpt || undefined}><summary>{messages.runResult.technicalDetails}</summary>
      <code>{evidence.evidence_ref}</code><dl><div><dt>{messages.runResult.evidenceSourceLabel}</dt><dd>{evidence.source_uri}</dd></div><div><dt>{messages.runResult.evidenceLocatorLabel}</dt><dd>{JSON.stringify(evidence.source_locator)}</dd></div><div><dt>{messages.runResult.evidenceHashLabel}</dt><dd>{evidence.content_hash}</dd></div></dl></details>}
      {opened && (evidence.excerpt === null ? <p className="compactEmpty">{messages.runResult.noExcerpt}</p> : <>
        <div className="excerptToolbar" role="group" aria-label={messages.runResult.excerptDisplay}>
          <button className="secondaryButton compactButton" type="button" aria-pressed={!source} onClick={() => setSource(false)}>{messages.runResult.artifacts.preview}</button>
          <button className="secondaryButton compactButton" type="button" aria-pressed={source} onClick={() => setSource(true)}>{messages.runResult.excerptSource}</button>
        </div>
        {source ? <pre className="excerptSource">{evidence.excerpt}</pre> : <MarkdownText text={evidence.excerpt} />}
      </>)}
    </details>
  )
}
