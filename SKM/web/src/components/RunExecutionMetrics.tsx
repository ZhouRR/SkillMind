import type { RunDetailRecord } from '../api'
import { useMessages } from '../i18n'

/** 原 ToolCall を残したまま、明示的な訂正と人手介入を短く要約する。 */
export function RunExecutionMetrics({ detail }: { detail: RunDetailRecord }) {
  const messages = useMessages().runResult
  const metrics = detail.execution_metrics
  if (!metrics) return detail.tool_calls.some((call) => call.status === 'FAILED')
    ? <p className="hint">{messages.recoveryUnknown}</p> : null
  return <details className="executionMetrics">
    <summary>{messages.executionDiagnostics}{metrics.unresolved_tool_errors > 0 && <span className="validationWarning"> · {messages.unresolvedErrors}: {metrics.unresolved_tool_errors}</span>}</summary>
    <dl className="runFacts">
      <div><dt>{messages.unresolvedErrors}</dt><dd>{metrics.unresolved_tool_errors}</dd></div>
      <div><dt>{messages.correctedReads}</dt><dd>{metrics.corrected_reads}</dd></div>
      <div><dt>{messages.manualResponses}</dt><dd>{metrics.manual_responses}</dd></div>
      <div><dt>{messages.structuralErrors}</dt><dd>{metrics.structural_errors}</dd></div>
      <div><dt>{messages.correctionAttempts}</dt><dd>{metrics.correction_attempts}</dd></div>
      <div><dt>{messages.repeatedQueries}</dt><dd>{metrics.duplicate_reads}</dd></div>
      <div><dt>{messages.schemaReads}</dt><dd>{metrics.schema_reads}</dd></div>
      <div><dt>{messages.schemaCacheHits}</dt><dd>{metrics.schema_cache_hits}</dd></div>
      <div><dt>{messages.schemaRefreshes}</dt><dd>{metrics.schema_refreshes}</dd></div>
    </dl>
  </details>
}
