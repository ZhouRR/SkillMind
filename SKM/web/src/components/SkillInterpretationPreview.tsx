import { useEffect, useRef, useState } from 'react'
import type { InterpretationExecutionRecord, BlueprintNote, CapabilityBlueprintView, SourceTrace } from '../api'
import type { AdjustState } from '../hooks/useSkillInterpretation'
import { useMessages } from '../i18n'
import type { UiMessages } from '../lib/i18n/messages'
import { isNearBottom } from '../lib/scroll'
import { SourceExecutionPreview } from './SourceExecutionPreview'
import { SkillTabButton } from './SkillTabButton'

/** Worker 実行中の model 解釈を、実際の prompt と流式出力として表示する。 */
export function InterpretStreamView({ prompt, output, attempt = 1 }: {
  prompt: string
  output: string
  attempt?: number
}) {
  const messages = useMessages()
  const outputRef = useRef<HTMLPreElement>(null)
  const pinnedToBottom = useRef(true)
  // 出力の伸長に追随して末尾へスクロールする。ただしユーザーが上へ離れている間は追随しない。
  useEffect(() => {
    const element = outputRef.current
    if (element && pinnedToBottom.current) element.scrollTop = element.scrollHeight
  }, [output])
  return (
    <div className="interpretStream" aria-live="polite">
      <div className="interpretStreamHead">
        <span className="streamDot" aria-hidden="true" />
        <strong>{messages.skills.interpretRunning}</strong>
        <span className="interpretStreamHint">{messages.skills.interpretRunningHint}</span>
      </div>
      {attempt > 1 && (
        <p className="hint" role="status">
          {messages.skills.attemptLine(attempt)}
        </p>
      )}
      {prompt !== '' && (
        <details className="interpretPrompt">
          <summary>{messages.skills.promptSent}</summary>
          <pre>{prompt}</pre>
        </details>
      )}
      <div className="interpretOutput">
        <span>{messages.skills.modelOutput}</span>
        {output === ''
          ? <p className="interpretOutputWaiting">{messages.skills.waitingModelOutput}</p>
          : (
            <pre ref={outputRef} onScroll={(event) => { pinnedToBottom.current = isNearBottom(event.currentTarget) }}>
              {output}<span className="streamCursor" />
            </pre>
          )}
      </div>
    </div>
  )
}

/** 解釈詳細内の観測区分。要約と操作は tab の外に常置し、詳細だけを切り替える。 */
type InterpretationDetailTab = 'report' | 'blueprint' | 'contracts' | 'diff'

/** Model interpretation の report、source trace、confidence、diff、追加調整を表示する。
 *
 *  詳細(報告・蓝图・契約・差分)は縦へ全部積むと発行判断に要る要約と操作が埋もれるため、
 *  tab で同時に一つだけ見せる。非活性 tab も hidden で mount したままにする(測試断言と状態保持)。 */
export function InterpretationExecutionView({ execution, instruction, onInstructionChange, onAdjust, onRegenerate, onCreateDraft, adjustState, versionBusy }: {
  execution: InterpretationExecutionRecord
  instruction: string
  onInstructionChange: (value: string) => void
  onAdjust: () => void
  onRegenerate: () => void
  onCreateDraft: () => void
  adjustState: AdjustState
  versionBusy: boolean
}) {
  const messages = useMessages()
  const [detailTab, setDetailTab] = useState<InterpretationDetailTab>('report')
  const report = execution.report
  const failed = execution.status !== 'PREVIEW_READY'
  const validationAttempts = execution.status === 'FAILED' ? execution.validation_attempts : undefined
  const parentInstruction = readAdjustmentInstruction(execution.adjustment)
  const blueprint = execution.preview.capability_blueprint
  const hasBlueprint = blueprint !== null && (blueprint.capabilities.length > 0 || blueprint.tasks.length > 0)
  const sourceExecution = execution.preview.source_execution
  const manifest = execution.preview.runtime_manifest_draft
  const hasContracts = Array.isArray(manifest.tasks) && manifest.tasks.some(isPlainRecord)
  return (
    <section className="interpretationPanel">
      <div className="subsectionHeader">
        <h3>{messages.skills.interpretationTitle}</h3>
        <span className="scopeBadge">{execution.compatibility_level}</span>
      </div>
      <dl className="runFacts">
        <div><dt>{messages.skills.interpretationIdLabel}</dt><dd className="mono">{execution.interpretation_id}</dd></div>
        <div><dt>{messages.skills.interpretationStatusLabel}</dt><dd>{execution.status}{execution.reused ? messages.skills.reusedSuffix : ''}</dd></div>
        <div><dt>{messages.skills.interpretationModelLabel}</dt><dd className="mono">{execution.model ?? '—'}</dd></div>
        {!sourceExecution && <div><dt>{messages.skills.interpretationConfidenceLabel}</dt><dd>{execution.confidence.toFixed(2)}</dd></div>}
      </dl>
      {failed && <p className="error" role="alert">{messages.skills.interpretationFailedLine(execution.error_code ?? 'unknown')}</p>}
      {validationAttempts && validationAttempts.length > 0 && (
        <details className="rawResult">
          <summary>{messages.skills.validationErrorDetails}</summary>
          <ol>
            {validationAttempts.slice(0, 2).map((attempt, index) => <li key={index}><pre>{attempt}</pre></li>)}
          </ol>
        </details>
      )}
      {execution.parent_interpretation_id && (
        <p className="hint">{messages.skills.parentPrefix}<code className="mono">{execution.parent_interpretation_id}</code>{parentInstruction ? messages.skills.adjustQuote(parentInstruction) : ''}</p>
      )}
      {report && !sourceExecution && <p className="interpretationSummary">{report.summary}</p>}
      {sourceExecution && <SourceExecutionPreview preview={sourceExecution} />}
      {!sourceExecution && <div className="tabBar" role="tablist" aria-label={messages.skills.detailTabsAria}>
        <SkillTabButton current={detailTab} tab="report" onSelect={setDetailTab}>{messages.skills.tabReport}</SkillTabButton>
        <SkillTabButton current={detailTab} tab="blueprint" onSelect={setDetailTab}>{messages.skills.blueprintTitle}</SkillTabButton>
        <SkillTabButton current={detailTab} tab="contracts" onSelect={setDetailTab}>{messages.skills.generatedContractsTitle}</SkillTabButton>
        <SkillTabButton current={detailTab} tab="diff" onSelect={setDetailTab}>
          {messages.skills.revisionDiffTitle}
          {/* 構造差分がある時だけ点を出し、他 tab からも「見るべき差分がある」ことを示す。 */}
          {execution.diff.has_changes === true && <i className="tabAlert" aria-hidden="true" />}
        </SkillTabButton>
      </div>}
      <div className="tabPanel" role="tabpanel" hidden={!sourceExecution && detailTab !== 'report'}>
        {report ? (
          <div className="interpretationDetailStack">
            {!sourceExecution && <ConfidenceGrid confidence={report.confidence} />}
            {!sourceExecution && <NoteBlock title={messages.skills.assumptionsTitle} items={report.assumptions} />}
            <NoteBlock title={messages.skills.questionsTitle} items={report.questions.map((q) => ({ key: q.key, text: q.required ? `${q.text}${messages.skills.requiredAnswerSuffix}` : q.text }))} />
            {!sourceExecution && report.source_traces.length > 0 && (
              <div className="noteBlock">
                <h4>{messages.skills.sourceTracesTitle}</h4>
                <ul className="sourceTraceList">
                  {report.source_traces.map((trace, index) => <SourceTraceItem key={`${trace.target}-${index}`} trace={trace} />)}
                </ul>
              </div>
            )}
            {report.diagnostics.length > 0 && (
              <ul className="diagnostics">
                {report.diagnostics.map((diagnostic, index) => (
                  <li key={`${diagnostic.code}-${index}`}>
                    <strong>{diagnostic.severity} · {diagnostic.code}</strong>
                    <span>{diagnostic.message}{diagnostic.path ? ` (${diagnostic.path}${diagnostic.line ? `:${diagnostic.line}` : ''})` : ''}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : <p className="hint">{messages.skills.reportEmpty}</p>}
      </div>
      <div className="tabPanel" role="tabpanel" hidden={!!sourceExecution || detailTab !== 'blueprint'}>
        {hasBlueprint
          ? <CapabilityBlueprintPreview blueprint={blueprint} />
          : <p className="hint">{messages.skills.blueprintEmpty}</p>}
      </div>
      <div className="tabPanel" role="tabpanel" hidden={!!sourceExecution || detailTab !== 'contracts'}>
        {hasContracts
          ? <GeneratedContractPreview manifest={manifest} />
          : <p className="hint">{messages.skills.contractsEmpty}</p>}
      </div>
      <div className="tabPanel" role="tabpanel" hidden={!!sourceExecution || detailTab !== 'diff'}>
        <RevisionDiffView diff={execution.diff} hasParent={execution.parent_interpretation_id !== null} />
      </div>
      {sourceExecution && execution.parent_interpretation_id && <details className="rawResult"><summary>{messages.skills.revisionDiffTitle}</summary><RevisionDiffView diff={execution.diff} hasParent /></details>}
      <form className="adjustForm" onSubmit={(event) => { event.preventDefault(); onAdjust() }}>
        <label>{messages.skills.adjustLabel}<textarea className="compactTextarea" value={instruction} onChange={(event) => onInstructionChange(event.target.value)} placeholder={messages.skills.adjustPlaceholder} spellCheck={false} /></label>
        {adjustState.status === 'error' && <p className="error" role="alert">{adjustState.message}</p>}
        <div className="skillActions">
          <button className="secondaryButton" type="submit" disabled={adjustState.status === 'adjusting' || !instruction.trim()}>{adjustState.status === 'adjusting' ? messages.skills.adjusting : messages.skills.adjustAndReinterpret}</button>
          <button className="secondaryButton" type="button" disabled={versionBusy} onClick={onRegenerate}>{messages.skills.forceRegenerate}</button>
          <button className="primaryButton" type="button" disabled={failed || versionBusy} onClick={onCreateDraft}>{versionBusy ? messages.skills.processing : messages.skills.createDraftFromThis}</button>
        </div>
      </form>
    </section>
  )
}

/** Skill の能力・目標・資源・規則・交付物・効果を業務固有分岐なしで公開前に示す。
 *
 * 業務 Schema と gate finding だけでは「この Skill が何をできるのか」が読めない。蓝图は
 * 資源前提と効果意図を明示し、効果が意図であって権限ではないことを利用者へ示す。
 */
function CapabilityBlueprintPreview({ blueprint }: { blueprint: CapabilityBlueprintView | null }) {
  // 蓝图は Interpreter の産物であり、解釈前は存在しない。空の枠を出すより、まだ無いことを
  // 示さないほうが「解釈したのに能力を抽出できなかった」との誤読を避けられる。
  const messages = useMessages()
  if (blueprint === null) return null
  const { capabilities, tasks, resource_requirements: resources, guidance } = blueprint
  if (capabilities.length === 0 && tasks.length === 0) return null
  return (
    <div className="noteBlock">
      {/* 見出しは親の詳細 tab(能力蓝图)が担うため、ここでは重複させない。 */}
      {capabilities.map((capability) => (
        <section key={capability.key}>
          <strong>{capability.title}</strong>
          <span className="mono">{capability.key}</span>
          {capability.summary && <p className="hint">{capability.summary}</p>}
        </section>
      ))}
      {tasks.map((task) => (
        <section key={task.key}>
          <strong>{messages.skills.objectivePrefix(task.key)}</strong>
          <p className="hint">{task.objective}</p>
          {(task.document_prerequisites ?? []).length > 0 && (
            <p className="hint">
              {messages.skills.documentPrerequisites}: {task.document_prerequisites?.join(', ')}
            </p>
          )}
          <BlueprintNoteList title={messages.skills.successCriteria} notes={task.success_criteria ?? []} />
          {(task.deliverables ?? []).length > 0 && (
            <ul className="noteList">
              {(task.deliverables ?? []).map((deliverable) => (
                <li key={deliverable.key}>
                  <strong>{messages.skills.deliverablePrefix(deliverable.kind)}</strong>
                  <span>{deliverable.description}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      ))}
      {resources.length > 0 && (
        <section>
          <strong>{messages.skills.resourcePrereq}</strong>
          <ul className="noteList">
            {resources.map((resource) => (
              <li key={resource.key}>
                <strong>{resource.key} · {resource.kind}</strong>
                <span>
                  {resource.required ? messages.skills.requiredLabel : messages.skills.optionalLabel} · {resource.access}
                  {(resource.capabilities ?? []).length > 0
                    ? ` · ${(resource.capabilities ?? []).join(', ')}`
                    : ''}
                  {resource.selection_guidance ? ` — ${resource.selection_guidance}` : ''}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      <BlueprintNoteList title={messages.skills.requiredRules} notes={guidance.required_rules} />
      <BlueprintNoteList title={messages.skills.recommendedSteps} notes={guidance.recommended_steps} />
      <BlueprintNoteList title={messages.skills.qualityCriteria} notes={guidance.quality_criteria} />
      <BlueprintNoteList title={messages.skills.prohibited} notes={guidance.prohibited_actions} />
      {blueprint.interaction_points.length > 0 && (
        <section>
          <strong>{messages.skills.interactionPoints}</strong>
          <ul className="noteList">
            {blueprint.interaction_points.map((point) => (
              <li key={point.key}>
                <strong>{point.type}</strong>
                <span>{point.condition}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {blueprint.effect_intents.length > 0 && (
        <section>
          <strong>{messages.skills.effectIntents}</strong>
          <ul className="noteList">
            {blueprint.effect_intents.map((intent) => (
              <li key={intent.key}>
                <strong>{intent.mode} · risk {intent.risk}</strong>
                <span>
                  {intent.operation}
                  {intent.resource_key ? ` · ${intent.resource_key}` : ''}
                </span>
              </li>
            ))}
          </ul>
          <p className="hint">{messages.skills.effectIntentHint}</p>
        </section>
      )}
    </div>
  )
}

/** 蓝图の note 群を空なら描画せずに一覧化する。 */
function BlueprintNoteList({ title, notes }: { title: string; notes: BlueprintNote[] }) {
  if (notes.length === 0) return null
  return (
    <div>
      <span>{title}</span>
      <ul className="noteList">
        {notes.map((note) => (
          <li key={note.key}>
            <strong>{note.key}</strong>
            <span>{note.text}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Interpreter が生成し platform がコンパイルした task field を公開前に確認可能にする。 */
function GeneratedContractPreview({ manifest }: { manifest: Record<string, unknown> }) {
  const messages = useMessages()
  const tasks = Array.isArray(manifest.tasks)
    ? manifest.tasks.filter(isPlainRecord)
    : []
  if (tasks.length === 0) return null
  return (
    <div className="noteBlock">
      {/* 見出しは親の詳細 tab(生成的任务契约)が担うため、ここでは重複させない。 */}
      {tasks.map((task, index) => (
        <section key={typeof task.key === 'string' ? task.key : index}>
          <strong>{typeof task.key === 'string' ? task.key : `task-${index + 1}`}</strong>
          <ContractFieldList contract={task.input_contract} title={messages.skills.contractInputTitle} />
          <ContractFieldList contract={task.output_contract} title={messages.skills.contractOutputTitle} />
        </section>
      ))}
    </div>
  )
}

/** TaskContractDraft の field/type/required を business 固有分岐なしで一覧化する。 */
function ContractFieldList({ contract, title }: { contract: unknown; title: string }) {
  const messages = useMessages()
  if (!isPlainRecord(contract)) return null
  const fields = Array.isArray(contract.fields) ? contract.fields.filter(isPlainRecord) : []
  return (
    <div>
      <span>{title} · {String(contract.type ?? 'unknown')}</span>
      {fields.length > 0 && (
        <ul className="noteList">
          {fields.map((field, index) => (
            <li key={typeof field.key === 'string' ? field.key : index}>
              <strong>{String(field.key ?? index)}</strong>
              <span>{String(field.type ?? 'unknown')} · {field.required === true ? messages.skills.contractFieldRequired : messages.skills.contractFieldOptional}{typeof field.description === 'string' ? ` — ${field.description}` : ''}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** 分項 confidence を小さな metric grid で表示する。 */
function ConfidenceGrid({ confidence }: { confidence: Record<string, number> }) {
  const entries = Object.entries(confidence)
  if (entries.length === 0) return null
  return (
    <div className="resourceGrid">
      {entries.map(([area, value]) => (
        <div className="metric" key={area}><span>{area}</span><strong>{value.toFixed(2)}</strong></div>
      ))}
    </div>
  )
}

/** key/text の note list（assumptions・questions）を表示する。 */
function NoteBlock({ title, items }: { title: string; items: Array<{ key: string; text: string }> }) {
  if (items.length === 0) return null
  return (
    <div className="noteBlock">
      <h4>{title}</h4>
      <ul className="noteList">
        {items.map((item, index) => (
          <li key={`${item.key}-${index}`}><strong>{item.key}</strong><span>{item.text}</span></li>
        ))}
      </ul>
    </div>
  )
}

/** 一つの source trace を target と位置付きで表示する。 */
function SourceTraceItem({ trace }: { trace: SourceTrace }) {
  return (
    <li>
      <code className="mono">{trace.target}</code>
      <span>{trace.path}{trace.line ? `:${trace.line}` : ''} — {trace.reason}</span>
    </li>
  )
}

/** 親との revision diff を dimension 別に要約し、原始 JSON も併記する。 */
function RevisionDiffView({ diff, hasParent }: { diff: Record<string, unknown>; hasParent: boolean }) {
  const messages = useMessages()
  const lines = summarizeDiff(messages, diff)
  const changed = diff.has_changes === true && lines.length > 0
  return (
    <div className="revisionDiff">
      {/* 見出しは親の詳細 tab(修订差异)が担う。差分有無の badge だけをここに残す。 */}
      {changed && <span className="scopeBadge">{messages.skills.hasDiff}</span>}
      {!hasParent && <p className="hint">{messages.skills.firstInterpretation}</p>}
      {hasParent && !changed && <p className="hint">{messages.skills.noStructuralDiff}</p>}
      {changed && <ul className="diffLines">{lines.map((line, index) => <li key={index}>{line}</li>)}</ul>}
      {hasParent && (
        <details className="rawResult"><summary>{messages.skills.viewRawDiff}</summary><pre>{JSON.stringify(diff, null, 2)}</pre></details>
      )}
    </div>
  )
}

/** Revision diff を dimension 別の短い行へ要約する。 */
function summarizeDiff(messages: UiMessages, diff: Record<string, unknown>): string[] {
  const lines: string[] = []
  for (const dimension of ['capabilities', 'tasks', 'data_sources', 'tools', 'workflows']) {
    const value = diff[dimension]
    if (!isPlainRecord(value)) continue
    const added = countArray(value.added)
    const removed = countArray(value.removed)
    const changed = countArray(value.changed)
    if (added + removed + changed > 0) lines.push(`${dimension}: +${added} / -${removed} / ~${changed}`)
  }
  const level = diff.compatibility_level
  if (isPlainRecord(level)) lines.push(`compatibility_level: ${String(level.from)} → ${String(level.to)}`)
  for (const dimension of ['identity', 'permissions', 'ui', 'confidence', 'skill_execution']) {
    const value = diff[dimension]
    if (isPlainRecord(value) && isPlainRecord(value.changed)) {
      const count = Object.keys(value.changed).length
      if (count > 0) lines.push(`${dimension}: ${messages.skills.changedCount(count)}`)
    }
  }
  const diagnostics = diff.diagnostics
  if (isPlainRecord(diagnostics)) {
    const added = countArray(diagnostics.added)
    const removed = countArray(diagnostics.removed)
    if (added + removed > 0) lines.push(`diagnostics: +${added} / -${removed}`)
  }
  return lines
}

/** Adjustment record から人が読める instruction を安全に取り出す。 */
function readAdjustmentInstruction(adjustment: Record<string, unknown> | null): string | null {
  if (adjustment === null) return null
  const value = adjustment.instruction
  return typeof value === 'string' ? value : null
}

/** Object を厳密に判定する（配列や null を除く）。 */
function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 配列なら長さを、そうでなければ 0 を返す。 */
function countArray(value: unknown): number {
  return Array.isArray(value) ? value.length : 0
}
