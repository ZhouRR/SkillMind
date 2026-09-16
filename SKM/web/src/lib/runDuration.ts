/** Run の開始〜終了（進行中は現在）の壁時計秒数。承認待ちは含み、開始前の排隊は除く。 */
export function runDuration(run: { status: string; started_at?: string | null; finished_at?: string | null }, now: number):
  { kind: 'waiting' } | { kind: 'unknown' } | { kind: 'finished' | 'elapsed'; seconds: number } {
  const terminal = ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(run.status)
  if (!run.started_at) return { kind: !terminal && ['QUEUED', 'PREPARING'].includes(run.status) ? 'waiting' : 'unknown' }
  const start = Date.parse(run.started_at)
  const end = run.finished_at ? Date.parse(run.finished_at) : terminal ? NaN : now
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return { kind: 'unknown' }
  return { kind: run.finished_at ? 'finished' : 'elapsed', seconds: Math.floor((end - start) / 1000) }
}
