import { useEffect, useState } from 'react'
import { useMessages } from '../i18n'
import { runDuration } from '../lib/runDuration'

/** 開始後の壁時計時間を共通表示し、未開始や旧記録を架空のゼロ秒にしない。 */
export function RunDuration({ run }: { run: { status: string; started_at?: string | null; finished_at?: string | null } }) {
  const labels = useMessages().runHistory
  const [now, setNow] = useState(Date.now)
  const duration = runDuration(run, now)
  const active = duration.kind === 'elapsed'
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])
  const value = duration.kind === 'waiting' ? labels.durationWaiting
    : duration.kind === 'unknown' ? '—'
      : labels.durationValue(Math.floor(duration.seconds / 3600), Math.floor(duration.seconds / 60) % 60, duration.seconds % 60)
  return <span className="runDuration" title={labels.durationHint}>
    {duration.kind === 'elapsed' ? labels.elapsedLabel : labels.durationLabel} · {value}
  </span>
}
