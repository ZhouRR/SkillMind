import type { ToolCallDetail } from '../api'
import { useMessages } from '../i18n'

/** 公開 tool status の既知値だけを翻訳し、未知値は成功へ推測しない。 */
export function toolStatusTone(status: string): 'succeeded' | 'failed' | 'running' | 'pending' | 'cancelled' | 'unknown' {
  switch (status.trim().toUpperCase()) {
    case 'SUCCEEDED': case 'SUCCESS': case 'COMPLETED': return 'succeeded'
    case 'FAILED': case 'ERROR': case 'DENIED': case 'TIMED_OUT': return 'failed'
    case 'RUNNING': case 'STARTED': case 'APPLYING': return 'running'
    case 'PENDING': case 'REQUESTED': case 'QUEUED': case 'LEASED': return 'pending'
    case 'CANCELLED': case 'CANCELED': return 'cancelled'
    default: return 'unknown'
  }
}

/** 省略された引数にも keyboard で開ける全文を残す監査行。 */
export function ToolCallSummary({ tool }: { tool: ToolCallDetail }) {
  const labels = useMessages().uiAuditWorkspace
  const tone = toolStatusTone(tool.status)
  const names = { succeeded: labels.toolStatusSucceeded, failed: labels.toolStatusFailed,
    running: labels.toolStatusRunning, pending: labels.toolStatusPending, cancelled: labels.toolStatusCancelled,
    unknown: tool.status || labels.toolStatusUnknown }
  return <li>
    <div><strong>{tool.capability}</strong><span className={`toolStatus toolStatus-${tone}`}>{names[tone]}</span></div>
    <p>{tool.provider} · {tool.duration_ms === null ? '—' : `${tool.duration_ms} ms`}</p>
    <code>{JSON.stringify(tool.arguments_summary)}</code>
    <details className="toolArguments">
      <summary>{labels.toolArguments}</summary>
      <pre className="rawResultBody" tabIndex={0} role="region" aria-label={`${tool.capability} · ${labels.toolArguments}`}>{JSON.stringify(tool.arguments_summary, null, 2)}</pre>
    </details>
  </li>
}
