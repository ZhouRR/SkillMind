import type { RunDetailRecord } from '../api'
import { useMessages } from '../i18n'
import { MarkdownText } from './MarkdownText'

/** Workspace と実行詳細で同じ原要約を表示し、付随する操作・証拠は mount しない。 */
export function RunSummaryReport({ detail, showValidation = false }: {
  detail: Pick<RunDetailRecord, 'result' | 'status'>
  showValidation?: boolean
}) {
  const messages = useMessages()
  const result = detail.result
  if (!result) return null
  return <section className="resultSummary reportSummary">
    <div>
      <h3>{messages.runResult.reportTitle}</h3>
      <MarkdownText text={result.summary} />
    </div>
    <dl>
      <div><dt>{messages.runResult.platformStatus}</dt><dd>{messages.enums.runStatus[detail.status] ?? detail.status}</dd></div>
      <div className={result.needs_review ? 'reviewRequired' : undefined}><dt>{messages.runResult.reviewLabel}</dt><dd>{result.needs_review ? messages.runResult.needsReview : messages.runResult.noExtraReview}</dd></div>
      {showValidation && <div><dt>{messages.runResult.schemaCheckLabel}</dt><dd>{result.validation.schema_valid === true ? messages.runResult.schemaValidText : messages.runResult.schemaCheckRequiredText}</dd></div>}
    </dl>
  </section>
}
