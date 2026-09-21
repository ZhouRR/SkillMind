import type { ReactNode } from 'react'

import { useMessages } from '../i18n'
import { reportPreviewHtml } from '../lib/documentPreview'
import { splitOverflow } from '../lib/resultOverflow'
import { displayText, isHtmlReport } from '../lib/resultPresentation'
import { MarkdownText } from './MarkdownText'

/** 通用 OutcomeEnvelope を task-specific business field に依存せず標準表示する。 */
export function OutcomeEnvelopeResult({ data, schema, showTechnicalDetails, onEvidence, onArtifact }: {
  data: Record<string, unknown>
  schema: Record<string, unknown> | null
  showTechnicalDetails: boolean
  onEvidence: (refs: string[]) => void
  onArtifact: (ref: string) => void
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
      <div className={`outcomeStatus${data.status !== 'COMPLETED' ? ' outcomePartial' : ''}`}><strong>{messages.runResult.completionStatus} · {messages.runResult.completionStates[displayText(data.status, '')] ?? displayText(data.status, messages.runResult.outcomeUnknown)}</strong>{showTechnicalDetails && <span>{displayText(data.outcome_version, '—')}</span>}</div>
      <OutcomeCollection title={messages.runResult.deliverables} empty={messages.runResult.noDeliverables} singleHeading>
        {deliverables.map((item, index) => (
          <article className="outcomeCard" key={displayText(item.key, `deliverable-${index}`)}>
            <div className="outcomeCardHeading"><h5>{displayText(item.title, displayText(item.key, messages.runResult.untitledLabel))}</h5>{showTechnicalDetails && <span>{displayText(item.kind, '—')}</span>}</div>
            <DeliverableContent item={item} />
            {(typeof item.artifact_ref === 'string' || stringItems(item.evidence_refs).length > 0) && <div className="outcomeCardActions">
            {typeof item.artifact_ref === 'string' && <button className="secondaryButton compactButton" type="button"
              onClick={() => onArtifact(displayText(item.artifact_ref, ''))}>{messages.runResult.artifacts.preview}</button>}
            {stringItems(item.evidence_refs).length > 0 && <button className="secondaryButton compactButton" type="button" onClick={() => onEvidence(stringItems(item.evidence_refs))}>{messages.runResult.viewExcerpt}</button>}
            </div>}
            {showTechnicalDetails && typeof item.artifact_ref === 'string' && <code>{item.artifact_ref}</code>}
          </article>
        ))}
      </OutcomeCollection>
      <OutcomeCollection title={messages.runResult.findingsTitle} empty={messages.runResult.noFindings}>
        {findings.map((item, index) => (
          <article className="outcomeCard" key={displayText(item.key, `finding-${index}`)}>
            <div className="outcomeCardHeading"><h5>{displayText(item.title, displayText(item.key, messages.runResult.untitledLabel))}</h5>{typeof item.severity === 'string' && <span className="outcomeSeverity">{item.severity}</span>}</div>
            <MarkdownText text={displayText(item.detail, '—')} />
            {renderOutcomeRefs(stringItems(item.evidence_refs), showTechnicalDetails, messages.runResult.evidenceTitle)}
            {stringItems(item.evidence_refs).length > 0 && <button className="secondaryButton compactButton" type="button" onClick={() => onEvidence(stringItems(item.evidence_refs))}>{messages.runResult.viewExcerpt}</button>}
          </article>
        ))}
      </OutcomeCollection>
      {(questions.length > 0 || limitations.length > 0) && (
        <div className="outcomeColumns">
          {questions.length > 0 && <OutcomeCollection title={messages.runResult.openQuestions} empty={messages.runResult.noOpenQuestions}>
            {questions.map((item, index) => <MarkdownText key={displayText(item.key, `question-${index}`)} text={displayText(item.question, '—')} />)}
          </OutcomeCollection>}
          {limitations.length > 0 && <OutcomeCollection title={messages.runResult.limitations} empty={messages.runResult.noLimitations}>
            {limitations.map((item) => <MarkdownText key={item} text={item} />)}
          </OutcomeCollection>}
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
        <details className="outcomeGroup outcomeEffects">
          <summary>{messages.runResult.changesAndEffects}<span className="eventCount">{effects.length}</span></summary>
          <p className="hint">{messages.runResult.modelEffectsHint}</p>
          {renderOutcomeRefs(proposalRefs, showTechnicalDetails, messages.runResult.evidenceTitle)}
          {effects.map((effect, index) => (
            <p key={`${displayText(effect.proposal_ref, 'effect')}-${index}`}>
              <strong>{messages.enums.effectStatus[displayText(effect.status, '')] ?? displayText(effect.status, messages.runResult.outcomeUnknown)}</strong> · {displayText(effect.summary, '—')}
            </p>
          ))}
        </details>
      )}
    </div>
  )
}

/** 成果物の長い原説明と本文を一緒に畳み、内容を削除・推測分類しない。 */
function DeliverableContent({ item }: { item: Record<string, unknown> }) {
  const messages = useMessages().runResult
  const description = displayText(item.description, '')
  const content = displayText(item.content, '')
  const html = isHtmlReport(content)
  return <>
    {description && description.length <= 240 && <MarkdownText text={description} />}
    {(content || description.length > 240) && <details className="deliverableContent" open={html || undefined}>
      <summary>{messages.deliverableContent}</summary>
      {description.length > 240 && <MarkdownText text={description} />}
      {content && (html ? <iframe className="outcomeReportFrame" title={displayText(item.title, messages.untitledLabel)}
        sandbox="" referrerPolicy="no-referrer" srcDoc={reportPreviewHtml(content)} />
        : item.kind === 'structured_data' ? <pre>{content}</pre> : <MarkdownText text={content} />)}
    </details>}
  </>
}

/** Outcome の一群へ共通見出しと空状態を付与し、長い一覧の尾部を畳む。 */
function OutcomeCollection({ title, empty, children, singleHeading = false }: {
  title: string
  empty: string
  children: ReactNode
  /** 単一交付物は自身の見出しで足りる。複数件は件数付き見出しを保つ。 */
  singleHeading?: boolean
}) {
  const items = Array.isArray(children) ? (children as ReactNode[]) : [children]
  const { visible, hidden } = splitOverflow(items)
  return (
    <section className="outcomeGroup" aria-label={title}>
      {/* 畳んだ後も総数が読めるよう、見出しに件数を残す。 */}
      {!(singleHeading && items.length === 1) && <h4>{title}{items.length > 0 && <span className="eventCount">{items.length}</span>}</h4>}
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
export function SchemaResultValue({ schema, value, path, showTechnicalDetails = false }: {
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
