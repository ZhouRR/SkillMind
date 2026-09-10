import { API_BASE, hasStrings, isRecord } from './http'

/** SSE replay から受信する監査 event。 */
export interface RunEventRecord {
  run_id: string
  run_attempt_id: string | null
  agent_session_id: string | null
  sequence: number
  event_type: string
  occurred_at: string
  payload: Record<string, unknown>
  trace_id: string | null
}

/** Browser EventSource の lifecycle を Component から切り離す subscription。 */
export interface RunEventSubscription {
  close(): void
}

/** Backend が現在送信する named SSE event の固定集合。 */
const RUN_EVENT_NAMES = [
  'run.snapshot',
  'run.cancel.requested',
  'segment.started',
  'segment.completed',
  'interaction.requested',
  'interaction.responded',
  'interaction.expired',
  'checkpoint.created',
  'session.started',
  'session.resumed',
  'session.forked',
  'session.replaced',
  'change.proposed',
  'effect.approved',
  'effect.rejected',
  'effect.applied',
  'effect.failed',
  'step.started',
  'step.completed',
  'step.failed',
  'text.delta',
  'text.completed',
  'tool.requested',
  'tool.completed',
  'tool.failed',
  'permission.required',
  'permission.resolved',
  'evidence.created',
  'artifact.created',
  'usage.updated',
  'session.deferred',
  'session.interrupted',
  'session.store.degraded',
  'result.completed',
  'engine.failed',
] as const

/** Named SSE event を sequence replay 付きで購読する。 */
export function subscribeRunEvents(
  runId: string,
  after: number,
  onEvent: (event: RunEventRecord) => void,
  onConnectionError: () => void,
): RunEventSubscription {
  const endpoint = `${API_BASE}/runs/${encodeURIComponent(runId)}/events?after=${after}`
  const source = new EventSource(endpoint, { withCredentials: true })
  const listener = (event: Event): void => {
    if (!(event instanceof MessageEvent) || typeof event.data !== 'string') return
    try {
      onEvent(parseRunEvent(JSON.parse(event.data) as unknown))
    } catch {
      onConnectionError()
    }
  }
  for (const eventName of RUN_EVENT_NAMES) source.addEventListener(eventName, listener)
  source.onerror = onConnectionError
  return { close: () => source.close() }
}

/** Unknown SSE JSON を sequence 付き RunEvent へ制限する。 */
function parseRunEvent(value: unknown): RunEventRecord {
  if (
    !isRecord(value)
    || !hasStrings(value, ['run_id', 'event_type', 'occurred_at'])
    || typeof value.sequence !== 'number'
    || !Number.isInteger(value.sequence)
    || value.sequence < 1
    || !isRecord(value.payload)
  ) {
    throw new Error('Run event did not match its contract')
  }
  return value as unknown as RunEventRecord
}
